"""Pathway: script_doc — OWNER: NISHANT

Screenplay PDF in, the written script out, via Sarvam Document Intelligence
(Sarvam Vision / doc-digitization). Nothing in the video pipeline imports this
and it is deliberately NOT in `STAGE_ORDER` — it is a standalone pathway that
parks its output on disk for a consumer that does not exist yet.

Run it on its own, the same way every stage runs on its own:

    python3 -m drishti.script_doc path/to/script.pdf

reads:
    <pdf>                   one PDF (or ZIP of pages), <=200MB, <=10 pages

writes (into --out, default runs/scripts/<pdf stem>/):
    script.md               the extracted script, reading order preserved
    script.json             {"source", "language", "output_format", "job_id",
                             "pages": int, "characters": int, "files": [...]}
    pages.json              the API's own structured page-level JSON, verbatim
    raw.zip                 exactly what the API returned, never rewritten
    raw/                    the unpacked zip

Why the flow looks like this: doc-digitization is a *job* API, not a one-shot
POST like Saaras. Five calls, in order, all on `api.sarvam.ai`:

    POST /doc-digitization/job/v1                     -> job_id
    POST /doc-digitization/job/v1/upload-files        -> presigned upload URL
    PUT  <presigned url>                              -> the PDF bytes
    POST /doc-digitization/job/v1/{job_id}/start      -> queued
    GET  /doc-digitization/job/v1/{job_id}/status     -> poll to a terminal state
    POST /doc-digitization/job/v1/{job_id}/download-files -> presigned zip URL

Two deliberate omissions from `common.py`:

  - **No `cache_ns`.** A job id is stateful; caching the create call would hand
    back a job id that has already been consumed. Idempotence comes from the
    pipeline's own rule instead — a completed output directory is skipped unless
    you pass `--force`. Rerunning a parsed PDF costs nothing and no credits.
  - **Own HTTP helpers.** `common.http_json` is POST-and-JSON only; this needs a
    GET, a binary PUT to Azure blob storage, and a binary GET. Writing four
    small stdlib helpers here beats editing the one file all four verticals
    import.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .common import SARVAM_BASE_URL, env_key, load_env, log, write_json

BASE = f"{SARVAM_BASE_URL}/doc-digitization/job/v1"

DEFAULT_LANGUAGE = "en-IN"
DEFAULT_OUTPUT_FORMAT = "md"          # "markdown" is a 400; the API wants "md"
DEFAULT_OUT_ROOT = Path("runs/scripts")

# Poll budget. Ten pages of a screenplay come back in well under a minute; the
# ceiling exists so a stuck job fails loudly instead of hanging a terminal.
POLL_INTERVAL_S = 3.0
POLL_TIMEOUT_S = 600.0

MAX_BYTES = 200 * 1024 * 1024

TERMINAL_OK = {"Completed", "PartiallyCompleted"}
TERMINAL_BAD = {"Failed", "Cancelled"}


# --------------------------------------------------------------------------
# HTTP — stdlib only, same retry temperament as common.py
# --------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"api-subscription-key": env_key("SARVAM_API_KEY")}


def _request(request: urllib.request.Request, *, attempts: int = 3, timeout: int = 120) -> bytes:
    """Send a prepared request, retrying on 429/5xx with a widening backoff."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            last = RuntimeError(f"{request.get_method()} {request.full_url} -> {error.code}: {detail}")
            if error.code not in (429, 500, 502, 503, 504):
                raise last from error
        except urllib.error.URLError as error:
            last = RuntimeError(f"{request.get_method()} {request.full_url} -> {error.reason}")
        if attempt < attempts - 1:
            time.sleep(2 ** attempt)
    raise last if last else RuntimeError("request failed with no error recorded")


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **_headers()},
        method="POST",
    )
    return json.loads(_request(request).decode("utf-8"))


def _get(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_headers(), method="GET")
    return json.loads(_request(request).decode("utf-8"))


def _put_file(url: str, path: Path) -> None:
    """Upload to the presigned URL. Azure block blobs require the type header;
    sending it to any other storage backend is harmless."""
    request = urllib.request.Request(
        url,
        data=path.read_bytes(),
        headers={
            "Content-Type": "application/pdf" if path.suffix.lower() == ".pdf" else "application/zip",
            "x-ms-blob-type": "BlockBlob",
        },
        method="PUT",
    )
    _request(request, timeout=300)


def _download(url: str, destination: Path) -> None:
    """Presigned download URL — it carries its own auth, so no key header."""
    request = urllib.request.Request(url, method="GET")
    destination.write_bytes(_request(request, timeout=300))


# --------------------------------------------------------------------------
# job flow
# --------------------------------------------------------------------------


def _create_job(language: str, output_format: str) -> str:
    body = _post(BASE, {"job_parameters": {"language": language, "output_format": output_format}})
    job_id = body.get("job_id")
    if not job_id:
        raise RuntimeError(f"create returned no job_id: {body}")
    return str(job_id)


def _upload(job_id: str, pdf: Path) -> None:
    body = _post(f"{BASE}/upload-files", {"job_id": job_id, "files": [pdf.name]})
    urls = body.get("upload_urls") or {}
    # The response keys by filename; take ours by name, else the only entry.
    url = urls.get(pdf.name) or (next(iter(urls.values())) if len(urls) == 1 else None)
    if isinstance(url, dict):                       # some responses nest it
        url = url.get("file_url") or url.get("url")
    if not url:
        raise RuntimeError(f"no upload URL for {pdf.name}: {body}")
    _put_file(str(url), pdf)


def _poll(job_id: str, *, timeout: float = POLL_TIMEOUT_S) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        status = _get(f"{BASE}/{job_id}/status")
        state = str(status.get("job_state", ""))
        if state != last_state:
            log(f"  job {state.lower() or 'unknown'}…")
            last_state = state
        if state in TERMINAL_OK:
            return status
        if state in TERMINAL_BAD:
            raise RuntimeError(f"job {job_id} ended {state}: {status.get('error_message') or status}")
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"job {job_id} still {last_state or 'unknown'} after {timeout:.0f}s")


def _download_outputs(job_id: str, destination: Path) -> None:
    body = _post(f"{BASE}/{job_id}/download-files", {})
    urls = body.get("download_urls") or {}
    if not urls:
        raise RuntimeError(f"job {job_id} completed with no downloadable output: {body}")
    entry = next(iter(urls.values()))
    url = entry.get("file_url") or entry.get("url") if isinstance(entry, dict) else entry
    if not url:
        raise RuntimeError(f"malformed download_urls: {body}")
    _download(str(url), destination)


# --------------------------------------------------------------------------
# unpacking
# --------------------------------------------------------------------------


def _unpack(archive: Path, out_dir: Path) -> list[Path]:
    """Extract the result zip, refusing any member that escapes out_dir."""
    raw = out_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            target = (raw / member.filename).resolve()
            if not str(target).startswith(str(raw.resolve())):
                raise RuntimeError(f"refusing zip member outside output dir: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(member))
            written.append(target)
    return sorted(written)


def _collect_text(files: list[Path], output_format: str) -> str:
    """Join the per-page text files in filename order — the API names them so
    that lexical order is page order."""
    suffix = {"md": ".md", "html": ".html", "json": ".json"}.get(output_format, ".md")
    pages = [p for p in files if p.suffix.lower() == suffix]
    if not pages and suffix != ".json":                    # json is always present
        pages = [p for p in files if p.suffix.lower() == ".json"]
    return "\n\n".join(p.read_text(encoding="utf-8", errors="replace").strip() for p in pages)


def _collect_pages(files: list[Path]) -> Any:
    """The structured page-level JSON the API ships alongside md/html."""
    blobs = [p for p in files if p.suffix.lower() == ".json"]
    if not blobs:
        return None
    if len(blobs) == 1:
        return json.loads(blobs[0].read_text(encoding="utf-8"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in blobs]


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def extract_script(pdf: Path, out_dir: Path, cfg: dict | None = None) -> dict[str, Any]:
    """Run one PDF through doc-digitization and write the script into out_dir.

    Returns the manifest also written as script.json. Raises on failure — this
    never writes a half-populated output directory as if it had succeeded.
    """
    cfg = cfg or {}
    language = cfg.get("language") or DEFAULT_LANGUAGE
    output_format = cfg.get("output_format") or DEFAULT_OUTPUT_FORMAT

    pdf = Path(pdf)
    if not pdf.is_file():
        raise SystemExit(f"No such file: {pdf}")
    if pdf.suffix.lower() not in (".pdf", ".zip"):
        raise SystemExit(f"Expected a .pdf or .zip, got {pdf.suffix or 'no extension'}")
    size = pdf.stat().st_size
    if size > MAX_BYTES:
        raise SystemExit(f"{pdf.name} is {size / 1e6:.0f}MB; the API limit is 200MB")

    # Resolved, because _unpack returns resolved paths and the manifest lists
    # them relative to this directory.
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"script_doc: {pdf.name} ({size / 1e3:.0f}kB) · language {language} · format {output_format}")

    job_id = _create_job(language, output_format)
    log(f"  job {job_id}")
    _upload(job_id, pdf)
    _post(f"{BASE}/{job_id}/start", {})
    status = _poll(job_id)

    archive = out_dir / "raw.zip"
    _download_outputs(job_id, archive)
    files = _unpack(archive, out_dir)

    text = _collect_text(files, output_format)
    (out_dir / "script.md").write_text(text + "\n" if text else "", encoding="utf-8")

    pages = _collect_pages(files)
    if pages is not None:
        write_json(out_dir / "pages.json", pages)

    detail = (status.get("job_details") or [{}])[0]
    manifest = {
        "source": str(pdf),
        "job_id": job_id,
        "job_state": status.get("job_state"),
        "language": language,
        "output_format": output_format,
        "pages": detail.get("total_pages"),
        "pages_succeeded": detail.get("pages_succeeded"),
        "pages_failed": detail.get("pages_failed"),
        "characters": len(text),
        "files": [str(p.relative_to(out_dir)) for p in files],
    }
    write_json(out_dir / "script.json", manifest)

    log(f"  {manifest['pages'] or '?'} page(s) · {len(text)} characters -> {out_dir / 'script.md'}")
    if detail.get("pages_failed"):
        log(f"  WARNING: {detail['pages_failed']} page(s) failed — script.md is incomplete")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="extract the written script from a PDF via Sarvam Document Intelligence")
    parser.add_argument("pdf", type=Path, help="screenplay PDF (or ZIP of page images)")
    parser.add_argument("--out", type=Path, help=f"output dir, default {DEFAULT_OUT_ROOT}/<pdf stem>")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE,
                        help=f"BCP-47 code the document is written in, default {DEFAULT_LANGUAGE}")
    parser.add_argument("--format", dest="output_format", choices=["md", "html", "json"],
                        default=DEFAULT_OUTPUT_FORMAT, help=f"default {DEFAULT_OUTPUT_FORMAT}")
    parser.add_argument("--force", action="store_true", help="re-parse even if script.md already exists")
    args = parser.parse_args()

    load_env()
    destination = args.out or DEFAULT_OUT_ROOT / args.pdf.stem
    if (destination / "script.md").exists() and not args.force:
        log(f"script_doc: {destination / 'script.md'} already exists — skipping (use --force)")
    else:
        extract_script(args.pdf, destination, {"language": args.language, "output_format": args.output_format})
