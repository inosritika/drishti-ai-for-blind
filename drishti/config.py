"""Profiles, job directories and run verification — OWNER: ARYAN.

Two profiles, two different worlds:

    dev   runs/dev/…    permissive. Invariant failures print as warnings.
    demo  runs/demo/…   strict. A failed invariant aborts the run and no
                        output is written.

The cache is shared between them on purpose: it is keyed by a content hash of
the request, so it cannot serve a wrong answer, and every dev iteration makes
the demo run faster.

`verify_job` is the 12:30 gate checklist expressed as code. At 3pm, tired,
nobody reliably eyeballs whether narration ends 80ms inside a gap boundary.
This does, on every single run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import (
    has_stream,
    media_duration,
    read_json,
    script_ok,
)

# Languages Bulbul v3 can speak. The resolver refuses to commit to anything
# outside this set rather than guessing.
SUPPORTED_TTS: frozenset[str] = frozenset({
    "en-IN", "hi-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN",
    "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN",
})

# Saaras sometimes reports a bare code; normalise to the Sarvam form.
_LANGUAGE_ALIASES: dict[str, str] = {
    "en": "en-IN", "eng": "en-IN", "en-in": "en-IN", "en-us": "en-IN",
    "hi": "hi-IN", "hin": "hi-IN",
    "bn": "bn-IN", "ben": "bn-IN",
    "gu": "gu-IN", "guj": "gu-IN",
    "kn": "kn-IN", "kan": "kn-IN",
    "ml": "ml-IN", "mal": "ml-IN",
    "mr": "mr-IN", "mar": "mr-IN",
    "od": "od-IN", "or": "od-IN", "ori": "od-IN", "or-in": "od-IN",
    "pa": "pa-IN", "pan": "pa-IN",
    "ta": "ta-IN", "tam": "ta-IN",
    "te": "te-IN", "tel": "te-IN",
}

# Tolerances. These are not arbitrary — see drishti_e2e.py and HANDOFF.md.
FIT_TOLERANCE = 0.08      # a TTS render may exceed its window by this much
GAP_TOLERANCE = 0.05      # float slop when comparing narration to gap bounds
DURATION_TOLERANCE = 0.05  # output must preserve source duration within this


@dataclass(frozen=True)
class Profile:
    name: str
    jobs_root: Path
    strict: bool


PROFILES: dict[str, Profile] = {
    "dev": Profile(name="dev", jobs_root=Path("runs/dev"), strict=False),
    "demo": Profile(name="demo", jobs_root=Path("runs/demo"), strict=True),
}


def get_profile(name: str | None = None) -> Profile:
    """Resolve the profile from an explicit name, then DRISHTI_PROFILE, then dev."""
    import os

    chosen = (name or os.getenv("DRISHTI_PROFILE") or "dev").strip().lower()
    if chosen not in PROFILES:
        raise SystemExit(
            f"Unknown profile {chosen!r}. Choose one of: {', '.join(sorted(PROFILES))}"
        )
    return PROFILES[chosen]


def new_job(profile: Profile, clip: Path | str, label: str | None = None) -> Path:
    """Create a fresh job directory and copy the clip in as input.mp4.

    Always a NEW directory per clip: a chunk cache from a different clip would
    silently poison gap detection.
    """
    import shutil

    source = Path(clip)
    if not source.is_file():
        raise SystemExit(f"Clip does not exist: {source}")

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", label or source.stem).strip("-").lower()[:40]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    job = profile.jobs_root / f"{stamp}_{slug or 'job'}"
    job.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source, job / "input.mp4")
    return job


def normalize_language(code: str | None) -> str | None:
    """Map a detected code to a Sarvam code. None when it cannot be resolved."""
    if not code:
        return None
    raw = code.strip()
    if not raw or raw.lower() in ("unknown", "und", "none"):
        return None
    if raw in SUPPORTED_TTS:
        return raw
    lowered = raw.lower()
    if lowered in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[lowered]
    # "hi-in" -> "hi-IN"
    if "-" in lowered:
        head, _, tail = lowered.partition("-")
        candidate = f"{head}-{tail.upper()}"
        if candidate in SUPPORTED_TTS:
            return candidate
        return _LANGUAGE_ALIASES.get(head)
    return None


def verify_job(job: Path, cfg: dict, *, complete: bool = True) -> list[str]:
    """Return a list of invariant failures. Empty list means the run is clean.

    `complete=False` skips checks on artifacts that do not exist yet, for
    verifying a partial run mid-pipeline.
    """
    job = Path(job)
    problems: list[str] = []

    meta = read_json(job / "meta.json", default={})
    gaps = read_json(job / "gaps.json", default=[])
    narration = read_json(job / "narration.json", default=[])
    language = read_json(job / "language.json", default={})
    output = job / "output.mp4"

    output_language = cfg.get("output_language")

    # --- language routing -------------------------------------------------
    if output_language:
        if output_language not in SUPPORTED_TTS:
            problems.append(
                f"output_language {output_language!r} is not a language Bulbul can speak"
            )
        if str(cfg.get("language", "auto")).lower() == "auto":
            source = normalize_language(language.get("source_language"))
            if source and source != output_language:
                problems.append(
                    f"auto mode: detected source {source} but output is "
                    f"{output_language} — the clip's own language must win"
                )

    # --- narration ---------------------------------------------------------
    if not narration:
        if complete:
            problems.append("no narration segments — an unchanged video is not a result")
    else:
        seen_gaps: set[int] = set()
        for index, item in enumerate(narration):
            label = f"narration[{index}]"

            if item.get("skipped"):
                continue

            gap_index = item.get("gap_index")
            if gap_index in seen_gaps:
                problems.append(
                    f"{label}: gap {gap_index} already has a narration — "
                    "one gap, at most one narration"
                )
            seen_gaps.add(gap_index)

            text = (item.get("text") or "").strip()
            if not text:
                problems.append(f"{label}: empty narration text")

            item_language = item.get("language") or output_language
            if output_language and item_language != output_language:
                problems.append(
                    f"{label}: language {item_language!r} does not match resolved "
                    f"output_language {output_language!r}"
                )
            if text and item_language and not script_ok(text, item_language):
                problems.append(
                    f"{label}: text is not written in {item_language} — {text[:40]!r}"
                )

            start = float(item.get("start", 0.0))
            max_duration = float(item.get("max_duration", 0.0))
            wav_duration = item.get("wav_duration")

            if isinstance(gap_index, int) and 0 <= gap_index < len(gaps):
                gap = gaps[gap_index]
                if start < float(gap["start"]) - GAP_TOLERANCE:
                    problems.append(
                        f"{label}: starts at {start:.2f}s, before its gap "
                        f"opens at {float(gap['start']):.2f}s"
                    )
                if wav_duration is not None:
                    ends = start + float(wav_duration)
                    if ends > float(gap["end"]) + GAP_TOLERANCE:
                        problems.append(
                            f"{label}: ends at {ends:.2f}s, after dialogue resumes "
                            f"at {float(gap['end']):.2f}s — this talks over speech"
                        )
            elif gaps:
                problems.append(f"{label}: gap_index {gap_index!r} is not a detected gap")

            if wav_duration is None:
                if complete:
                    problems.append(f"{label}: never synthesised (no wav_duration)")
            elif max_duration and float(wav_duration) > max_duration + FIT_TOLERANCE:
                problems.append(
                    f"{label}: audio is {float(wav_duration):.2f}s but its window "
                    f"allows {max_duration:.2f}s"
                )

            wav = item.get("wav")
            if wav and not (job / Path(wav).name).is_file() and not Path(wav).is_file():
                problems.append(f"{label}: wav file is missing: {wav}")

    # --- final output ------------------------------------------------------
    if not output.is_file():
        if complete:
            problems.append("output.mp4 was never written")
    else:
        if not has_stream(output, "v"):
            problems.append("output.mp4 has no video stream")
        if not has_stream(output, "a"):
            problems.append("output.mp4 has no audio stream")
        source_duration = meta.get("duration")
        if source_duration:
            actual = media_duration(output)
            drift = abs(actual - float(source_duration))
            if drift > DURATION_TOLERANCE:
                problems.append(
                    f"output.mp4 is {actual:.2f}s but the source is "
                    f"{float(source_duration):.2f}s — duration must be preserved"
                )

    return problems


def enforce(job: Path, cfg: dict, profile: Profile, *, complete: bool = True) -> list[str]:
    """Verify a job, then warn (dev) or abort (demo). Returns the problem list."""
    from .common import log

    problems = verify_job(job, cfg, complete=complete)
    if not problems:
        log(f"  verify: clean ({profile.name})")
        return problems

    for problem in problems:
        log(f"  {'FAIL' if profile.strict else 'warn'}: {problem}")

    if profile.strict:
        raise SystemExit(
            f"\ndemo profile: {len(problems)} invariant(s) failed — refusing to ship "
            f"this run.\nFix them, or rerun in dev to keep iterating."
        )
    return problems
