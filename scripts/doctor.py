#!/usr/bin/env python3
"""Check that this machine can run DRISHTI. Run `make doctor` before you build."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

OK = "  ok   "
BAD = " FAIL  "
WARN = " warn  "

failures = 0


def check(label: str, passed: bool, detail: str = "", fatal: bool = True) -> None:
    global failures
    if passed:
        mark = OK
    elif fatal:
        mark = BAD
        failures += 1
    else:
        mark = WARN
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("DRISHTI doctor\n")

    version = sys.version_info
    check(
        "python >= 3.11",
        version >= (3, 11),
        f"found {version.major}.{version.minor}.{version.micro}",
    )

    for binary in ("ffmpeg", "ffprobe"):
        path = shutil.which(binary)
        check(binary, bool(path), path or "not on PATH — brew install ffmpeg")

    env_path = Path(".env")
    check(".env present", env_path.is_file(), "copy .env.example to .env")

    values: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()

    for key in ("SARVAM_API_KEY", "OPENAI_API_KEY"):
        value = values.get(key) or os.getenv(key, "")
        check(key, bool(value), "empty" if not value else f"set ({len(value)} chars)")

    node = shutil.which("node")
    check("node (frontend only)", bool(node), node or "not on PATH", fatal=False)

    print()
    if failures:
        print(f"{failures} blocking problem(s). Fix these before building.")
        return 1
    print("All good. Work in runs/dev/ — use --profile demo only for stage runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
