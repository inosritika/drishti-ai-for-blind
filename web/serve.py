"""Serve the DRISHTI web UI and proxy voice input to Saaras — OWNER: ARYAN.

Stdlib only, on purpose: `make web` must work on any laptop with nothing
installed and no build step.

Why a server at all when the page is static: both features need the Sarvam
key, and it must never ship inside page JavaScript. This keeps the key in
.env on the laptop and exposes five endpoints:

    POST /api/hear      body: audio/wav  ->  {"language": "ta-IN"|null,
                                              "transcript": str, "heard": str}
    POST /api/describe  body: video/*    ->  {"job": "<id>"}
    GET  /api/job/<id>                   ->  status.json + narration + segments
    POST /api/script    body: application/pdf -> {"script": "<id>"}
    GET  /api/script/<id>                ->  {"state", "text", "pages", …}

`script` is a SEPARATE pathway, not a pipeline stage: a screenplay PDF (or a
page photo) goes to Sarvam Document Intelligence and the written script comes
back as text on disk. If the page sends a parsed script id along with describe
AND DRISHTI_SCRIPT_CONTEXT=1 is set, the text is handed to the pipeline as
background context for scene understanding; with the flag off (the default)
the id is ignored and describe behaves exactly as before.

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
import os
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
from drishti.script_doc import extract_script  # noqa: E402

PORT = 8080
MAX_AUDIO = 4 * 1024 * 1024    # a few seconds of 16kHz mono WAV is ~100KB
MAX_VIDEO = 120 * 1024 * 1024  # two minutes of typical 720p fits comfortably
MAX_PDF = 25 * 1024 * 1024     # ten pages of a screenplay is well under 1MB

JOB_ID = re.compile(r"^[0-9]{8}_[0-9]{6}_[a-z0-9-]+$")
# Job id -> tail of the child's stdout, so a failed run can explain itself.
LOGS: dict[str, list[str]] = {}

# Script id -> {"state", "text", "error", …}. In memory only: the durable copy
# is runs/scripts/<id>/, written by script_doc.
SCRIPTS: dict[str, dict] = {}
SCRIPTS_ROOT = REPO / "runs" / "scripts"

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


def script_context_enabled() -> bool:
    """Feature flag for script-as-context. Off by default, everywhere."""
    return os.getenv("DRISHTI_SCRIPT_CONTEXT", "").strip().lower() in {"1", "true", "yes", "on"}


def start_job(video: bytes, filename: str, language: str, cast: str = "",
              script_id: str = "") -> str:
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

    # A parsed script rides along only when the feature flag says so. The id is
    # validated against the same shape as job ids and must resolve to a file we
    # wrote — never a path from the client.
    if script_id and script_context_enabled():
        if JOB_ID.match(script_id):
            script_md = (SCRIPTS_ROOT / script_id / "script.md").resolve()
            if script_md.is_file() and script_md.is_relative_to(SCRIPTS_ROOT.resolve()):
                command += ["--script", str(script_md)]

    def run() -> None:
        child = subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in child.stdout:
            LOGS[job_id].append(line.rstrip())
            del LOGS[job_id][:-40]  # keep the tail bounded
        code = child.wait()

        # The pipeline records its own error for anything it anticipates, but
        # it cannot record what it did not survive — a failure outside a stage's
        # try block, an import error, a kill. Whatever the cause, a dead child
        # with nothing written would leave the page polling for ever. Treat a
        # non-zero exit as the last word and put the reason where the UI reads
        # it, so this can never hang again regardless of how the run died.
        status_path = job / "status.json"
        status = read_json(status_path, default={}) or {}
        if code != 0 and not status.get("error"):
            tail = [line for line in LOGS.get(job_id, []) if line.strip()]
            # The runner indents progress and prints failures flush-left, so
            # the unindented run at the end IS the message meant for a human.
            message: list[str] = []
            for line in reversed(tail):
                if line[:1].isspace():
                    if message:
                        break
                    continue
                message.insert(0, line)
            status["error"] = ("\n".join(message) or "\n".join(tail[-4:])
                               or f"the run exited with code {code}")
            status["stage"] = status.get("stage") or "failed"
            status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2))

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


def start_script(pdf: bytes, filename: str, language: str) -> str:
    """Stage the PDF and parse it in the background, same shape as start_job.

    Document Intelligence is a job API with its own polling, so this can take
    the better part of a minute — it must not hold the request open.
    """
    from datetime import datetime

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", Path(filename).stem).strip("-").lower()[:40]
    script_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slug or 'script'}"
    out_dir = SCRIPTS_ROOT / script_id
    out_dir.mkdir(parents=True, exist_ok=True)

    staged = out_dir / (Path(filename).name or "script.pdf")
    staged.write_bytes(pdf)

    SCRIPTS[script_id] = {"state": "running", "name": staged.name, "language": language}

    def run() -> None:
        try:
            manifest = extract_script(staged, out_dir, {"language": language})
            SCRIPTS[script_id] = {
                **SCRIPTS[script_id],
                "state": "done",
                "text": (out_dir / "script.md").read_text(encoding="utf-8"),
                "pages": manifest.get("pages"),
                "characters": manifest.get("characters"),
                "pages_failed": manifest.get("pages_failed"),
            }
        except Exception as exc:
            SCRIPTS[script_id] = {**SCRIPTS[script_id], "state": "error", "error": str(exc)}

    threading.Thread(target=run, daemon=True).start()
    return script_id


class Handler(BaseHTTPRequestHandler):
    # Browsers request MP4s in byte ranges to read metadata first, then stream
    # and seek. The BaseHTTPRequestHandler default is HTTP/1.0, which leaves a
    # valid output.mp4 looking like a zero-duration blank player in Chrome.
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

    def _send_video(self, path: Path) -> None:
        """Stream one MP4, including the single byte range browsers request."""
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
            if not match:
                self.send_error(416, "invalid byte range")
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = int(last) if last else end
            elif last:
                suffix = int(last)
                start = max(0, size - suffix)
            else:
                self.send_error(416, "invalid byte range")
                return
            if start >= size or end < start:
                self.send_error(416, "range not satisfiable")
                return
            end = min(end, size - 1)

        length = end - start + 1
        self.send_response(206 if requested else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                block = handle.read(min(64 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = self.path.split("?")[0]

        if path.startswith("/api/script/"):
            state = SCRIPTS.get(path[len("/api/script/"):])
            self._send_json(200 if state else 404, state or {"error": "no such script"})
            return

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
                    self._send_video(video)
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
                self._send_json(413, {"error": "video too large — keep it under 120 MB"})
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                job_id = start_job(
                    self.rfile.read(length),
                    query.get("name", ["clip.mp4"])[0],
                    query.get("language", ["auto"])[0],
                    query.get("cast", [""])[0],
                    query.get("script", [""])[0],
                )
                self._send_json(200, {"job": job_id})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/script":
            if not 0 < length <= MAX_PDF:
                self._send_json(413, {"error": "PDF too large — 25MB maximum"})
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                script_id = start_script(
                    self.rfile.read(length),
                    query.get("name", ["script.pdf"])[0],
                    query.get("language", ["en-IN"])[0],
                )
                self._send_json(200, {"script": script_id})
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
