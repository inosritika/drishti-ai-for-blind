"""Serve the DRISHTI web UI and proxy voice input to Saaras — OWNER: ARYAN.

Stdlib only, on purpose: `make web` must work on any laptop with nothing
installed and no build step.

Why a server at all when the page is static: the mic feature sends audio to
Saaras, and the API key must never ship inside page JavaScript. This proxy
keeps the key in .env on the laptop and exposes exactly one endpoint:

    POST /api/hear   body: audio/wav  ->  {"language": "ta-IN"|null,
                                           "transcript": str, "heard": str}

The user says a language — "Hindi", "தமிழ்", "मराठी में सुनाओ" — and we
resolve it two ways, either one sufficient:

  1. the WORD they said: the transcript contains a language name in any
     script we know it by;
  2. the LANGUAGE they said it in: Saaras identifies the speech language,
     so answering in Tamil selects Tamil without naming it.

A name match wins over the speech language: "Hindi" spoken in English must
select Hindi, not English.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(WEB_ROOT.parent))

from drishti.common import SARVAM_BASE_URL, env_key, http_multipart  # noqa: E402
from drishti.config import normalize_language  # noqa: E402

PORT = 8080
MAX_BODY = 4 * 1024 * 1024  # a few seconds of 16kHz mono WAV is ~100KB

# Language names as people actually say them: English name, endonym, the
# Devanagari spelling (a Hindi speaker asking for Tamil says "तमिल"), and
# spellings Saaras produces when it mishears. One-word utterances transcribe
# unreliably — the UI nudges people toward a short phrase ("Hindi please"),
# which lands far more often.
LANGUAGE_NAMES: dict[str, tuple[str, ...]] = {
    "en-IN": ("english", "angrezi", "अंग्रेजी", "अंग्रेज़ी", "इंग्लिश"),
    "hi-IN": ("hindi", "hindee", "hindhi", "हिंदी", "हिन्दी"),
    "bn-IN": ("bengali", "bangla", "বাংলা", "बांग्ला", "बंगाली"),
    "gu-IN": ("gujarati", "ગુજરાતી", "गुजराती"),
    "kn-IN": ("kannada", "ಕನ್ನಡ", "कन्नड़", "कन्नड"),
    "ml-IN": ("malayalam", "മലയാളം", "मलयालम"),
    "mr-IN": ("marathi", "मराठी"),
    "od-IN": ("odia", "oriya", "ଓଡ଼ିଆ", "उड़िया", "ओड़िया"),
    "pa-IN": ("punjabi", "panjabi", "ਪੰਜਾਬੀ", "पंजाबी"),
    "ta-IN": ("tamil", "தமிழ்", "तमिल"),
    "te-IN": ("telugu", "తెలుగు", "तेलुगु", "तेलुगू"),
}


def match_language(transcript: str, spoken_code: str | None) -> tuple[str | None, str]:
    """Resolve (language, how) from what was said and how it was said.

    An empty transcript voids BOTH signals: on pure silence Saaras still
    reports a language_code (observed: kn-IN, confidence irrelevant), and
    trusting it would flip the user's language on a cough.
    """
    if not transcript.strip():
        return None, "heard nothing"
    lowered = transcript.lower()
    for code, names in LANGUAGE_NAMES.items():
        for name in names:
            if name in lowered:
                return code, f"you named {names[0].title()}"
    normalized = normalize_language(spoken_code)
    if normalized:
        return normalized, f"you spoke {LANGUAGE_NAMES[normalized][0].title()}"
    return None, "no language recognised"


def hear(wav_bytes: bytes) -> dict:
    """One Saaras call on the recorded utterance."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        handle.write(wav_bytes)
        path = Path(handle.name)
    try:
        response = http_multipart(
            f"{SARVAM_BASE_URL}/speech-to-text",
            {"model": "saaras:v3", "language_code": "unknown"},
            "file",
            path,
            {"api-subscription-key": env_key("SARVAM_API_KEY")},
            cache_ns=None,  # every utterance is new — caching would be wrong
        )
    finally:
        path.unlink(missing_ok=True)

    transcript = (response.get("transcript") or "").strip()
    spoken_code = response.get("language_code") or response.get("detected_language")
    language, how = match_language(transcript, spoken_code)
    return {"language": language, "transcript": transcript, "heard": how}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        name = self.path.split("?")[0].lstrip("/") or "index.html"
        target = (WEB_ROOT / name).resolve()
        if not target.is_relative_to(WEB_ROOT) or not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        types = {".html": "text/html; charset=utf-8", ".css": "text/css",
                 ".js": "text/javascript", ".svg": "image/svg+xml",
                 ".mp4": "video/mp4", ".json": "application/json"}
        self._send(200, target.read_bytes(), types.get(target.suffix, "application/octet-stream"))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/hear":
            self._send_json(404, {"error": "unknown endpoint"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            self._send_json(413, {"error": "bad audio size"})
            return
        try:
            self._send_json(200, hear(self.rfile.read(length)))
        except Exception as exc:  # surface the reason; the UI reads .error
            self._send_json(502, {"error": str(exc)})

    def log_message(self, fmt: str, *args) -> None:
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}",
              file=sys.stderr)


def main() -> None:
    env_key("SARVAM_API_KEY")  # fail at startup, not on the first click
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"DRISHTI web · http://127.0.0.1:{PORT} · Ctrl-C stops")
    server.serve_forever()


if __name__ == "__main__":
    main()
