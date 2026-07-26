"""Serve the DRISHTI web UI and proxy voice input to Saaras — OWNER: ARYAN.

Stdlib only, on purpose: `make web` must work on any laptop with nothing
installed and no build step.

Why a server at all when the page is static: both features need the Sarvam
key, and it must never ship inside page JavaScript. This keeps the key in
.env on the laptop and exposes three endpoints:

    POST /api/hear      body: audio/wav  ->  {"language": "ta-IN"|null,
                                              "transcript": str, "heard": str}
    POST /api/describe  body: video/*    ->  {"job": "<id>"}
    GET  /api/job/<id>                   ->  status.json + narration + segments

`describe` runs the real pipeline as a SUBPROCESS, never in-process: a stage
that dies must not take the server with it, and the child's stdout is the log
we show the user. Progress needs no invention either — the runner already
writes stage and pct into status.json, so polling just reads it.

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

import json
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent
REPO = WEB_ROOT.parent
sys.path.insert(0, str(REPO))

from drishti.common import SARVAM_BASE_URL, env_key, http_multipart, read_json  # noqa: E402
from drishti.config import get_profile, new_job, normalize_language  # noqa: E402

PORT = 8080
MAX_AUDIO = 4 * 1024 * 1024    # a few seconds of 16kHz mono WAV is ~100KB
MAX_VIDEO = 120 * 1024 * 1024  # 29s of 720p is ~12MB; leave generous room

JOB_ID = re.compile(r"^[0-9]{8}_[0-9]{6}_[a-z0-9-]+$")
# Job id -> tail of the child's stdout, so a failed run can explain itself.
LOGS: dict[str, list[str]] = {}

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


def start_job(video: bytes, filename: str, language: str, cast: str = "") -> str:
    """Create a job directory, then run the pipeline on it in the background."""
    profile = get_profile("dev")
    suffix = Path(filename).suffix.lower() or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(video)
        staged = Path(handle.name)
    try:
        # new_job copies the clip in as input.mp4 and names the directory.
        job = new_job(profile, staged, label=Path(filename).stem or "web")
    finally:
        staged.unlink(missing_ok=True)

    job_id = job.name
    LOGS[job_id] = []
    command = [sys.executable, "-m", "drishti.pipeline", "--job", str(job),
               "--profile", profile.name]
    if language and language != "auto":
        command += ["--language", language]
    if cast.strip():
        command += ["--cast", cast.strip()]

    def run() -> None:
        child = subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in child.stdout:
            LOGS[job_id].append(line.rstrip())
            del LOGS[job_id][:-40]  # keep the tail bounded
        child.wait()

    threading.Thread(target=run, daemon=True).start()
    return job_id


def job_state(job_id: str) -> dict | None:
    """status.json plus everything the results view needs, in one payload."""
    job = (get_profile("dev").jobs_root / job_id).resolve()
    if not JOB_ID.match(job_id) or not job.is_dir():
        return None
    status = read_json(job / "status.json", default={})
    return {
        "job": job_id,
        "stage": status.get("stage", "queued"),
        "pct": status.get("pct", 0),
        "error": status.get("error"),
        "problems": status.get("problems") or [],
        "output_language": status.get("output_language"),
        "source_language": status.get("source_language"),
        "language_confidence": status.get("language_confidence"),
        "api": status.get("api") or {},
        "stage_timings": status.get("stage_timings") or {},
        "chunks": len(read_json(job / "chunks.json", default=[]) or []),
        "gaps": read_json(job / "gaps.json", default=[]) or [],
        "segments": read_json(job / "segments.json", default=[]) or [],
        "narration": read_json(job / "narration.json", default=[]) or [],
        "cast": read_json(job / "cast.json", default={}) or {},
        "video": f"/jobs/{job_id}/output.mp4" if (job / "output.mp4").is_file() else None,
        "log": LOGS.get(job_id, [])[-14:],
    }


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
        path = self.path.split("?")[0]

        if path.startswith("/api/job/"):
            state = job_state(path[len("/api/job/"):])
            self._send_json(200 if state else 404, state or {"error": "no such job"})
            return

        # Rendered videos live outside WEB_ROOT, under runs/dev/<id>/.
        if path.startswith("/jobs/"):
            parts = path[len("/jobs/"):].split("/")
            if len(parts) == 2 and JOB_ID.match(parts[0]) and parts[1] == "output.mp4":
                video = (get_profile("dev").jobs_root / parts[0] / "output.mp4").resolve()
                if video.is_file():
                    self._send(200, video.read_bytes(), "video/mp4")
                    return
            self._send(404, b"not found", "text/plain")
            return

        name = path.lstrip("/") or "index.html"
        target = (WEB_ROOT / name).resolve()
        if not target.is_relative_to(WEB_ROOT) or not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        types = {".html": "text/html; charset=utf-8", ".css": "text/css",
                 ".js": "text/javascript", ".svg": "image/svg+xml",
                 ".mp4": "video/mp4", ".json": "application/json"}
        self._send(200, target.read_bytes(), types.get(target.suffix, "application/octet-stream"))

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)

        if path == "/api/hear":
            if not 0 < length <= MAX_AUDIO:
                self._send_json(413, {"error": "bad audio size"})
                return
            try:
                self._send_json(200, hear(self.rfile.read(length)))
            except Exception as exc:  # surface the reason; the UI reads .error
                self._send_json(502, {"error": str(exc)})
            return

        if path == "/api/describe":
            if not 0 < length <= MAX_VIDEO:
                self._send_json(413, {"error": "video too large — trim it to 29s"})
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                job_id = start_job(
                    self.rfile.read(length),
                    query.get("name", ["clip.mp4"])[0],
                    query.get("language", ["auto"])[0],
                    query.get("cast", [""])[0],
                )
                self._send_json(200, {"job": job_id})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        self._send_json(404, {"error": "unknown endpoint"})

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
