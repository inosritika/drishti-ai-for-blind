"""Shared helpers — OWNER: ARYAN.

FROZEN, ADDITIVE ONLY. Append new functions freely; never change or rename an
existing one. This is the single file all four verticals import, so it is the
only real merge magnet in the repo.

What you get:
    log, run, require_binary                 process + logging
    media_duration, has_stream               ffprobe
    read_json, write_json                    JSON that survives Devanagari
    env_key, load_env                        credentials from .env
    http_json, http_multipart                HTTP with retry + response cache
    script_ratio, script_ok                  writing-system validation
    api_stats, reset_api_stats               call counts for the status panel

CACHING — the important part. Pass `cache_ns=` on any API call:

    http_json(url, payload, headers, cache_ns="sarvam_tts")

The key is a content hash of the request, NOT the job directory, so the same
clip in a brand-new job dir reuses every Saaras chunk, every 30B line and every
TTS render. Reruns are free, which is also what makes the on-stage demo return
in seconds. Set DRISHTI_NO_CACHE=1 to force real calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

SARVAM_BASE_URL = "https://api.sarvam.ai"
OPENAI_URL = "https://api.openai.com/v1/responses"

# Unicode blocks per writing system, used to prove the model wrote in the
# language we asked for before any audio gets synthesised.
SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "devanagari": ((0x0900, 0x097F),),
    "tamil": ((0x0B80, 0x0BFF),),
    "bengali": ((0x0980, 0x09FF),),
    "gujarati": ((0x0A80, 0x0AFF),),
    "gurmukhi": ((0x0A00, 0x0A7F),),
    "kannada": ((0x0C80, 0x0CFF),),
    "malayalam": ((0x0D00, 0x0D7F),),
    "odia": ((0x0B00, 0x0B7F),),
    "telugu": ((0x0C00, 0x0C7F),),
    "latin": ((0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F)),
}

LANGUAGE_SCRIPT: dict[str, str] = {
    "hi-IN": "devanagari",
    "mr-IN": "devanagari",
    "ta-IN": "tamil",
    "bn-IN": "bengali",
    "gu-IN": "gujarati",
    "pa-IN": "gurmukhi",
    "kn-IN": "kannada",
    "ml-IN": "malayalam",
    "od-IN": "odia",
    "te-IN": "telugu",
    "en-IN": "latin",
}


# --------------------------------------------------------------------------
# logging + process
# --------------------------------------------------------------------------


def log(message: str) -> None:
    print(message, flush=True)


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command, raising CalledProcessError with stderr attached on failure."""
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = (result.stderr or "").strip()[-2000:]
        raise subprocess.CalledProcessError(
            result.returncode, command, output=result.stdout, stderr=detail
        )
    return result


def require_binary(name: str) -> None:
    if not shutil.which(name):
        raise SystemExit(f"Missing required binary: {name}. Try: brew install ffmpeg")


# --------------------------------------------------------------------------
# ffprobe
# --------------------------------------------------------------------------


def media_duration(path: Path | str) -> float:
    """Duration in seconds. Always measure — never trust a promised duration."""
    result = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture=True,
    )
    return float(result.stdout.strip())


def has_stream(path: Path | str, kind: str) -> bool:
    """kind: 'v' for video, 'a' for audio."""
    result = run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", f"{kind}:0",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(path),
        ],
        capture=True,
    )
    return bool(result.stdout.strip())


def has_audio_stream(path: Path | str) -> bool:
    return has_stream(path, "a")


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def read_json(path: Path | str, default: Any = None) -> Any:
    file_path = Path(path)
    if not file_path.is_file():
        if default is not None:
            return default
        raise FileNotFoundError(f"Missing required file: {file_path}")
    return json.loads(file_path.read_text(encoding="utf-8"))


def write_json(path: Path | str, value: Any) -> None:
    """ensure_ascii=False so Devanagari and Tamil stay readable on disk."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------


def load_env(start: Path | None = None) -> None:
    """Load .env into os.environ without overwriting anything already set.

    Searches upward from `start` (default: cwd) so running from a subdirectory
    still finds the repo root .env.
    """
    directory = (start or Path.cwd()).resolve()
    for candidate in [directory, *directory.parents]:
        env_file = candidate / ".env"
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
        return


def env_key(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set. Copy .env.example to .env and paste your key in, "
            f"then check it with: make doctor"
        )
    return value


# --------------------------------------------------------------------------
# response cache
# --------------------------------------------------------------------------

_CACHE_ROOT = Path(os.getenv("DRISHTI_CACHE_DIR", ".cache"))
_API_CALLS: dict[str, int] = {}
_CACHE_HITS: dict[str, int] = {}


def _cache_enabled() -> bool:
    return os.getenv("DRISHTI_NO_CACHE", "").strip() not in ("1", "true", "yes")


def _digest(*parts: bytes) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _cache_path(namespace: str, key: str) -> Path:
    return _CACHE_ROOT / namespace / f"{key}.json"


def _cache_read(namespace: str, key: str) -> dict[str, Any] | None:
    if not _cache_enabled():
        return None
    path = _cache_path(namespace, key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    _CACHE_HITS[namespace] = _CACHE_HITS.get(namespace, 0) + 1
    return payload


def _cache_write(namespace: str, key: str, value: dict[str, Any]) -> None:
    if not _cache_enabled():
        return
    path = _cache_path(namespace, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file so an interrupted run can't leave a truncated entry
    # that later reads as a cache hit.
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def api_stats() -> dict[str, dict[str, int]]:
    """Network calls and cache hits per namespace — feeds the status panel."""
    return {"calls": dict(_API_CALLS), "cache_hits": dict(_CACHE_HITS)}


def reset_api_stats() -> None:
    _API_CALLS.clear()
    _CACHE_HITS.clear()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _request(request: urllib.request.Request, url: str, attempts: int, timeout: int) -> dict[str, Any]:
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if (exc.code == 429 or exc.code >= 500) and attempt < attempts:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:1200]}") from exc
        except urllib.error.URLError as exc:
            if attempt < attempts:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc
    raise AssertionError("unreachable")


def http_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    cache_ns: str | None = None,
    attempts: int = 3,
    timeout: int = 180,
) -> dict[str, Any]:
    """POST JSON, retrying on 429/5xx. Pass cache_ns to make reruns free."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    key = ""
    if cache_ns:
        key = _digest(url.encode("utf-8"), body)
        cached = _cache_read(cache_ns, key)
        if cached is not None:
            return cached

    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    result = _request(request, url, attempts, timeout)

    if cache_ns:
        _API_CALLS[cache_ns] = _API_CALLS.get(cache_ns, 0) + 1
        _cache_write(cache_ns, key, result)
    return result


def http_multipart(
    url: str,
    fields: dict[str, str],
    file_field: str,
    file_path: Path | str,
    headers: dict[str, str],
    *,
    content_type: str = "audio/wav",
    cache_ns: str | None = None,
    attempts: int = 3,
    timeout: int = 180,
) -> dict[str, Any]:
    """POST multipart/form-data with one file, retrying on 429/5xx.

    The cache key includes the file bytes, so re-chunking the same audio is free.
    """
    path = Path(file_path)
    file_bytes = path.read_bytes()

    key = ""
    if cache_ns:
        key = _digest(
            url.encode("utf-8"),
            json.dumps(fields, sort_keys=True).encode("utf-8"),
            file_bytes,
        )
        cached = _cache_read(cache_ns, key)
        if cached is not None:
            return cached

    boundary = f"----drishti-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{path.name}"\r\n'
        ).encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        file_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])

    request = urllib.request.Request(
        url,
        data=b"".join(chunks),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **headers},
        method="POST",
    )
    result = _request(request, url, attempts, timeout)

    if cache_ns:
        _API_CALLS[cache_ns] = _API_CALLS.get(cache_ns, 0) + 1
        _cache_write(cache_ns, key, result)
    return result


# --------------------------------------------------------------------------
# writing-system validation
# --------------------------------------------------------------------------


def script_ratio(text: str, script: str) -> float:
    """Fraction of letters in `text` that belong to `script`. 0.0 if no letters."""
    ranges = SCRIPT_RANGES.get(script)
    if not ranges:
        raise ValueError(f"Unknown script: {script}")
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    matched = sum(
        1
        for char in letters
        if any(low <= ord(char) <= high for low, high in ranges)
    )
    return matched / len(letters)


def script_ok(text: str, language: str, *, threshold: float | None = None) -> bool:
    """Did the model actually write in the language we asked for?

    Indic output allows some Latin for proper nouns (default 0.60). English is
    held stricter (0.90) and additionally rejected if it contains Indic letters,
    because "English" output peppered with Devanagari is a routing bug.
    """
    script = LANGUAGE_SCRIPT.get(language)
    if not script:
        return True  # unknown language: nothing to assert
    if script == "latin":
        if any(
            any(low <= ord(char) <= high for low, high in ranges)
            for char in text
            for name, ranges in SCRIPT_RANGES.items()
            if name != "latin"
        ):
            return False
        return script_ratio(text, "latin") >= (threshold if threshold is not None else 0.90)
    return script_ratio(text, script) >= (threshold if threshold is not None else 0.60)


load_env()
