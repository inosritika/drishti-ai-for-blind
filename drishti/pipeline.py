"""Stage runner — OWNER: ARYAN.

The whole design is one property: **a stage is skipped when its outputs already
exist.** That single rule gives us four things for free.

  * Fixtures need no special code path — a fixture job is a job directory whose
    outputs happen to be present already.
  * Substitution is automatic — as each teammate's module lands, delete that
    stage's outputs and rerun. Everything upstream stays put.
  * Resume works — for language selection now, and for narration approval in
    Increment 2, with no second mechanism.
  * Reruns are free, which is what makes the on-stage demo return in seconds.

Usage:

    # fresh run from a clip
    python3 -m drishti.pipeline --clip demo/clips/clip_a.mp4 --language auto

    # rerun one stage of an existing job
    python3 -m drishti.pipeline --job runs/dev/2026… --force gaps

    # strict run for the stage
    python3 -m drishti.pipeline --clip demo/clips/clip_a.mp4 --profile demo

    # contract check only, no media needed
    python3 -m drishti.pipeline --job fixtures/jobs/hindi_sample --check
"""

from __future__ import annotations

import argparse
import importlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import STAGE_ORDER
from .common import (
    api_stats,
    log,
    read_json,
    require_binary,
    reset_api_stats,
    write_json,
)
from .config import (
    SUPPORTED_TTS,
    Profile,
    enforce,
    get_profile,
    new_job,
    normalize_language,
    verify_job,
)


# --------------------------------------------------------------------------
# stage table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    name: str
    module: str
    function: str
    owner: str
    outputs: list[str] = field(default_factory=list)
    done: Callable[[Path], bool] | None = None

    def is_done(self, job: Path) -> bool:
        if self.done is not None:
            return self.done(job)
        return all((job / name).exists() for name in self.outputs)

    def clear(self, job: Path) -> None:
        for name in self.outputs:
            target = job / name
            if target.is_file():
                target.unlink()


def _narration_written(job: Path) -> bool:
    items = read_json(job / "narration.json", default=None)
    return bool(items) and all((item.get("text") or "").strip() for item in items)


def _tts_done(job: Path) -> bool:
    """narration.json is both input and output here, so check the wav fields."""
    items = read_json(job / "narration.json", default=None)
    if not items:
        return False
    return all(
        item.get("skipped") or item.get("wav_duration") is not None for item in items
    )


def _clear_tts(job: Path) -> None:
    for wav in job.glob("narration_*.wav"):
        wav.unlink()
    items = read_json(job / "narration.json", default=[])
    for item in items:
        for key in ("wav", "wav_duration", "pace", "skipped"):
            item.pop(key, None)
    if items:
        write_json(job / "narration.json", items)


STAGES: list[Stage] = [
    Stage("validate", "media", "prepare", "Nishant", ["meta.json", "audio.wav"]),
    Stage("gaps", "gaps", "detect", "Nishant",
          ["gaps.json", "chunks.json", "transcript.txt", "language.json"]),
    Stage("scenes", "scenes", "understand", "Ritika", ["scenes.json"]),
    Stage("cast", "cast", "bind", "Aryan", ["cast.json"]),
    Stage("align", "align", "plan", "Aryan", ["segments.json"]),
    Stage("narrate", "narrate", "write", "Tanishq", ["narration.json"],
          done=_narration_written),
    Stage("tts_fit", "speak", "synthesize", "Tanishq", [], done=_tts_done),
    Stage("mix", "mix", "render", "Nishant", ["output.mp4"]),
]

assert [stage.name for stage in STAGES] == STAGE_ORDER, "stage table drifted from STAGE_ORDER"


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


class Status:
    """Owns status.json. No stage ever writes this file."""

    def __init__(self, job: Path, cfg: dict) -> None:
        self.job = job
        self.data: dict[str, Any] = {
            "stage": "queued",
            "pct": 0,
            "profile": cfg.get("profile"),
            "language_requested": cfg.get("language"),
            "source_language": None,
            "language_confidence": None,
            "output_language": None,
            "stage_timings": {},
            "api": {},
            "problems": [],
            "error": None,
        }
        self.flush()

    def set(self, **fields: Any) -> None:
        self.data.update(fields)
        self.flush()

    def stage(self, name: str, index: int) -> None:
        self.data["stage"] = name
        self.data["pct"] = int(index / len(STAGES) * 100)
        self.flush()

    def timing(self, name: str, seconds: float) -> None:
        self.data["stage_timings"][name] = round(seconds, 2)
        self.data["api"] = api_stats()
        self.flush()

    def flush(self) -> None:
        write_json(self.job / "status.json", self.data)


# --------------------------------------------------------------------------
# language resolution — the one place this decision is made
# --------------------------------------------------------------------------


def resolve_language(job: Path, cfg: dict, status: Status) -> str:
    """Turn detection evidence plus the user's request into one output language.

    Nishant reports what Saaras heard. This decides what we speak. Nothing
    downstream re-decides, and nothing anywhere falls back to Hindi.
    """
    detected = read_json(job / "language.json", default={})
    source_raw = detected.get("source_language")
    source = normalize_language(source_raw)
    confidence = detected.get("confidence")

    status.set(source_language=source or source_raw, language_confidence=confidence)

    requested = str(cfg.get("language", "auto")).strip().lower()

    if requested and requested != "auto":
        explicit = normalize_language(requested)
        if not explicit:
            raise SystemExit(
                f"--language {cfg['language']!r} is not a language Bulbul can speak.\n"
                f"Supported: {', '.join(sorted(SUPPORTED_TTS))}"
            )
        if source and explicit != source:
            log(f"  note: translating — source is {source}, output will be {explicit}")
        return explicit

    if not source:
        raise SystemExit(
            f"Could not determine the spoken language "
            f"(detected {source_raw!r}, confidence {confidence!r}).\n"
            f"This clip needs an explicit choice — rerun with, for example, "
            f"--language hi-IN.\nWe never guess a language, and we never "
            f"default to Hindi."
        )
    if source not in SUPPORTED_TTS:
        raise SystemExit(
            f"Detected {source}, which Bulbul cannot speak. Rerun with an "
            f"explicit --language from: {', '.join(sorted(SUPPORTED_TTS))}"
        )
    return source


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def _call(stage: Stage, job: Path, cfg: dict) -> None:
    """Import and invoke a stage lazily, so one broken module can't stop the rest."""
    try:
        module = importlib.import_module(f".{stage.module}", package="drishti")
    except Exception as exc:  # noqa: BLE001 - a teammate's file may be mid-edit
        raise RuntimeError(
            f"could not import drishti.{stage.module} ({exc}). "
            f"{stage.owner} may be mid-edit — try again, or copy a fixture "
            f"output into the job to skip this stage."
        ) from exc
    getattr(module, stage.function)(job, cfg)


def run(job: Path, cfg: dict, profile: Profile, force: set[str] | None = None) -> Path:
    require_binary("ffmpeg")
    require_binary("ffprobe")
    reset_api_stats()

    force = force or set()
    status = Status(job, cfg)
    log(f"\nDRISHTI · {profile.name} · {job}")

    for name in force:
        stage = next((item for item in STAGES if item.name == name), None)
        if stage is None:
            raise SystemExit(f"Unknown stage {name!r}. Choose from: {', '.join(STAGE_ORDER)}")
        (_clear_tts if stage.name == "tts_fit" else stage.clear)(job)
        log(f"  forced rerun: {name}")

    for index, stage in enumerate(STAGES, start=1):
        if stage.is_done(job):
            log(f"[{index}/{len(STAGES)}] {stage.name}: skip (already done)")
            if stage.name == "gaps" and not cfg.get("output_language"):
                cfg["output_language"] = resolve_language(job, cfg, status)
                status.set(output_language=cfg["output_language"])
                log(f"  language: {cfg['output_language']}")
            continue

        status.stage(stage.name, index - 1)
        log(f"[{index}/{len(STAGES)}] {stage.name} ({stage.owner})…")
        started = time.time()
        try:
            _call(stage, job, cfg)
        except NotImplementedError:
            status.set(
                error=f"stage '{stage.name}' is not built yet",
                stage=stage.name,
            )
            raise SystemExit(
                f"\nStage '{stage.name}' is not built yet — that's {stage.owner}'s module "
                f"(drishti/{stage.module}.py).\n"
                f"To keep moving, copy a known-good {' or '.join(stage.outputs) or 'output'} "
                f"into {job}/ and rerun; the runner will skip the stage."
            ) from None
        except Exception as exc:  # noqa: BLE001 - surface any stage failure cleanly
            status.set(error=f"{stage.name}: {exc}", stage=stage.name)
            raise

        status.timing(stage.name, time.time() - started)

        if stage.name == "gaps":
            cfg["output_language"] = resolve_language(job, cfg, status)
            status.set(output_language=cfg["output_language"])
            log(f"  language: {cfg['output_language']}")

    status.stage("done", len(STAGES))
    problems = enforce(job, cfg, profile)
    status.set(problems=problems, stage="done", pct=100)

    total = sum(status.data["stage_timings"].values())
    log(f"\nDone in {total:.1f}s · {job / 'output.mp4'}")
    log(f"API: {api_stats()}")
    return job


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="drishti", description="Build an audio-described video")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--clip", type=Path, help="source MP4 — creates a new job directory")
    source.add_argument("--job", type=Path, help="existing job directory to continue")

    parser.add_argument("--profile", choices=("dev", "demo"), default=None)
    parser.add_argument("--language", default="auto",
                        help="'auto' matches the clip's own language (default), or a code like hi-IN")
    parser.add_argument("--label", default=None, help="name for the new job directory")
    parser.add_argument("--force", action="append", default=[],
                        help="rerun a stage even if its outputs exist (repeatable, or 'all')")
    parser.add_argument("--check", action="store_true",
                        help="verify an existing job's contracts and exit")

    parser.add_argument("--speaker", default="anand")
    parser.add_argument("--pace", type=float, default=1.05)
    parser.add_argument("--max-segments", type=int, default=4)
    parser.add_argument("--cast", default="",
                        help="character names present, e.g. \"Mr Bean\". Free text is\naccepted — a pasted cast list or synopsis works too. Names come only from\nhere: the model is never allowed to invent one.")
    parser.add_argument("--chunk-seconds", type=float, default=1.5)
    parser.add_argument("--min-gap", type=float, default=1.6)
    parser.add_argument("--edge-padding", type=float, default=0.15)
    parser.add_argument("--frame-fps", type=float, default=1.0)
    parser.add_argument("--max-frames", type=int, default=45)
    parser.add_argument("--detector", choices=("saaras", "silence"), default="saaras")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profile = get_profile(args.profile)

    cfg: dict[str, Any] = {
        "profile": profile.name,
        "language": args.language,
        "speaker": args.speaker,
        "pace": args.pace,
        "max_segments": args.max_segments,
        "cast": args.cast,
        "chunk_seconds": args.chunk_seconds,
        "min_gap": args.min_gap,
        "edge_padding": args.edge_padding,
        "frame_fps": args.frame_fps,
        "max_frames": args.max_frames,
        "detector": args.detector,
    }

    if args.check:
        job = args.job or args.clip
        if job is None or not Path(job).is_dir():
            raise SystemExit("--check needs --job pointing at a job directory")
        job = Path(job)
        detected = read_json(job / "language.json", default={})
        cfg["output_language"] = (
            normalize_language(detected.get("source_language"))
            if args.language == "auto"
            else normalize_language(args.language)
        )
        log(f"checking {job} (language: {cfg['output_language']})")
        problems = verify_job(job, cfg, complete=False)
        for problem in problems:
            log(f"  FAIL: {problem}")
        if problems:
            log(f"\n{len(problems)} contract problem(s).")
            return 1
        log("  contracts clean.")
        return 0

    job = Path(args.job) if args.job else new_job(profile, args.clip, args.label)

    force = set(args.force)
    if "all" in force:
        force = {stage.name for stage in STAGES}

    run(job, cfg, profile, force=force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
