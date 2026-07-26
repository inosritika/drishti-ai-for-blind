"""Stage: mix — OWNER: NISHANT

Duck the original soundtrack only while narration plays, and mux the result.

reads:
    input.mp4, narration.json, narration_XX.wav

writes:
    output.mp4

Tested filter chain to keep exactly (see drishti_e2e.py: mux):
    [i:a]adelay=<start_ms>:all=1[nN]        one per narration segment
    [n1][n2]...amix=inputs=N:duration=longest:normalize=0[narr_raw]
    [narr_raw]apad=whole_dur=<src>,atrim=0:<src>[narr]     <-- the truncation fix
    [narr]asplit=2[narr_sc][narr_mix]
    [0:a][narr_sc]sidechaincompress=threshold=0.015:ratio=8:attack=10:release=250[ducked]
    [ducked][narr_mix]amix=inputs=2:duration=first:normalize=0[mix]

  - video is stream-copied (-c:v copy); audio aac 192k; -movflags +faststart
  - apad + atrim are not optional: without them the output is truncated. A
    12-second synthetic mux smoke test caught this the first time.
  - if the source has no audio stream, build a silent one first
  - refuse to write output.mp4 if there are zero fitted segments — an
    unchanged copy is not a result
  - output duration must equal input duration within 0.05s

Smoke-test on a synthetic tone before touching real narration.

Two things the frozen chain does not spell out, both learned the hard way:

`normalize=0` on every amix. amix divides by the number of inputs by default,
so the film would come out quiet and it would sound like a bad mix rather than
a wrong flag.

Everything is forced to one sample rate and channel layout before it meets
anything else. Bulbul returns 24 kHz mono, sources are usually 44.1 or 48 kHz
stereo, and sidechaincompress needs its two inputs to agree. ffmpeg would
mostly insert conversions on its own; being explicit means the graph fails
loudly instead of resampling somewhere unexpected.
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import wave
from pathlib import Path

from .common import log, media_duration, read_json, require_binary, run

# Everything meets at one format before it meets anything else.
MIX_RATE = 48000
MIX_LAYOUT = "stereo"

# Ducking. Gentle threshold, hard ratio: narration is quiet relative to a film
# mix, so the compressor has to react to a small signal.
DEFAULT_DUCK_THRESHOLD = 0.015
DEFAULT_DUCK_RATIO = 8.0
DEFAULT_DUCK_ATTACK = 10.0
DEFAULT_DUCK_RELEASE = 250.0

DEFAULT_AUDIO_BITRATE = "192k"
DURATION_TOLERANCE = 0.05   # matches config.DURATION_TOLERANCE

# Smoke test tone.
SMOKE_FREQUENCY = 660.0
SMOKE_MAX_SECONDS = 3.0
SMOKE_MAX_SEGMENTS = 3


# --------------------------------------------------------------------------
# cfg -> env -> default
# --------------------------------------------------------------------------


def _setting(cfg: dict, key: str, env: str, default):
    if cfg.get(key) is not None:
        return cfg[key]
    raw = os.getenv(env, "").strip()
    return raw if raw else default


def _float_setting(cfg: dict, key: str, env: str, default: float) -> float:
    value = _setting(cfg, key, env, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SystemExit(f"{env}/{key} must be a number, got {value!r}")


# --------------------------------------------------------------------------
# segments
# --------------------------------------------------------------------------


def load_segments(job: Path) -> list[dict]:
    """Narration entries that actually have audio on disk.

    Tanishq's fit loop marks a line `skipped` when no pace or wording fits the
    window. Those are a correct outcome, not an error — they just contribute
    nothing to the mix.
    """
    items = read_json(job / "narration.json", default=[])
    segments: list[dict] = []
    for index, item in enumerate(items):
        if item.get("skipped"):
            continue
        name = item.get("wav")
        if not name:
            continue
        path = job / Path(name).name
        if not path.is_file():
            raise SystemExit(
                f"narration[{index}] points at {name}, which is not in {job}. "
                f"Rerun the tts_fit stage."
            )
        segments.append({"start": float(item.get("start", 0.0)), "path": path})
    return sorted(segments, key=lambda segment: segment["start"])


# --------------------------------------------------------------------------
# the filter graph
# --------------------------------------------------------------------------


def build_filter(segments: list[dict], duration: float, source_label: str, cfg: dict) -> str:
    threshold = _float_setting(cfg, "duck_threshold", "DRISHTI_DUCK_THRESHOLD", DEFAULT_DUCK_THRESHOLD)
    ratio = _float_setting(cfg, "duck_ratio", "DRISHTI_DUCK_RATIO", DEFAULT_DUCK_RATIO)
    attack = _float_setting(cfg, "duck_attack", "DRISHTI_DUCK_ATTACK", DEFAULT_DUCK_ATTACK)
    release = _float_setting(cfg, "duck_release", "DRISHTI_DUCK_RELEASE", DEFAULT_DUCK_RELEASE)

    fmt = f"aformat=sample_rates={MIX_RATE}:channel_layouts={MIX_LAYOUT}"
    parts = [f"[{source_label}]{fmt}[src]"]

    for position, segment in enumerate(segments):
        delay_ms = int(round(segment["start"] * 1000))
        # all=1 delays every channel. Without it only the left channel moves and
        # the narration arrives in one ear.
        parts.append(f"[{segment['input']}:a]{fmt},adelay={delay_ms}:all=1[n{position}]")

    if len(segments) == 1:
        narration = "[n0]"
    else:
        labels = "".join(f"[n{position}]" for position in range(len(segments)))
        parts.append(f"{labels}amix=inputs={len(segments)}:duration=longest:normalize=0[narr_raw]")
        narration = "[narr_raw]"

    # apad + atrim: without them the mix ends with the last narration segment
    # and the tail of the film is silently cut off.
    parts.append(f"{narration}apad=whole_dur={duration:.6f},atrim=0:{duration:.6f}[narr]")
    parts.append("[narr]asplit=2[narr_sc][narr_mix]")
    parts.append(
        f"[src][narr_sc]sidechaincompress=threshold={threshold:g}:ratio={ratio:g}"
        f":attack={attack:g}:release={release:g}[ducked]"
    )
    parts.append("[ducked][narr_mix]amix=inputs=2:duration=first:normalize=0[mix]")
    return ";".join(parts)


def render_segments(
    job: Path, segments: list[dict], destination: Path, duration: float, cfg: dict
) -> None:
    """Mux `segments` over input.mp4 and write `destination`."""
    source = job / "input.mp4"
    has_audio = bool(read_json(job / "meta.json", default={}).get("has_audio", True))

    command = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source)]
    next_input = 1

    if has_audio:
        source_label = "0:a"
    else:
        # No audio stream at all: give the chain something to duck.
        command += [
            "-f", "lavfi",
            "-t", f"{duration:.6f}",
            "-i", f"anullsrc=channel_layout={MIX_LAYOUT}:sample_rate={MIX_RATE}",
        ]
        source_label = f"{next_input}:a"
        next_input += 1

    for segment in segments:
        segment["input"] = next_input
        command += ["-i", str(segment["path"])]
        next_input += 1

    command += [
        "-filter_complex", build_filter(segments, duration, source_label, cfg),
        "-map", "0:v:0",
        "-map", "[mix]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", str(_setting(cfg, "audio_bitrate", "DRISHTI_AUDIO_BITRATE", DEFAULT_AUDIO_BITRATE)),
        "-movflags", "+faststart",   # the browser player needs moov up front
        str(destination),
    ]

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(command)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        raise SystemExit("ffmpeg could not build the mix:\n  " + "\n  ".join(detail[-4:]))


def check_output(destination: Path, duration: float) -> None:
    """The mix must not change how long the film is. This is what apad fixes."""
    actual = media_duration(destination)
    drift = abs(actual - duration)
    if drift > DURATION_TOLERANCE:
        raise SystemExit(
            f"{destination.name} is {actual:.3f}s but the source is {duration:.3f}s "
            f"({drift * 1000:.0f}ms). apad/atrim did not hold — do not ship this."
        )


# --------------------------------------------------------------------------
# smoke test
# --------------------------------------------------------------------------


def write_tone(path: Path, seconds: float, frequency: float = SMOKE_FREQUENCY) -> None:
    """A mono 24 kHz tone standing in for a Bulbul render."""
    rate = 24000
    amplitude = 12000
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(int(seconds * rate)):
            value = int(amplitude * math.sin(2 * math.pi * frequency * index / rate))
            frames += int(value).to_bytes(2, "little", signed=True)
        handle.writeframes(bytes(frames))


def smoke(job: Path, cfg: dict) -> None:
    """Render a tone into the real detected gaps, before any TTS exists.

    Everything lands in preview/ so no contract file is touched — narration.json
    and narration_XX.wav belong to Tanishq, and output.mp4 must only ever come
    from real narration.
    """
    job = Path(job)
    duration = float(read_json(job / "meta.json", default={}).get("duration") or 0.0)
    if not duration:
        raise SystemExit(f"No meta.json duration in {job}. Run the validate stage first.")

    gaps = read_json(job / "gaps.json", default=[])
    if not gaps:
        raise SystemExit(f"No gaps in {job} to place a tone in. Run the gaps stage first.")

    preview = job / "preview"
    segments = []
    for index, gap in enumerate(gaps[:SMOKE_MAX_SEGMENTS]):
        seconds = min(SMOKE_MAX_SECONDS, float(gap["duration"]) - 0.25)
        if seconds <= 0:
            continue
        path = preview / f"smoke_{index:02d}.wav"
        write_tone(path, seconds)
        segments.append({"start": float(gap["start"]), "path": path})
        log(f"  tone {index}: {gap['start']:.2f}s for {seconds:.2f}s")

    if not segments:
        raise SystemExit("No gap is long enough to hold a tone.")

    destination = preview / "smoke.mp4"
    render_segments(job, segments, destination, duration, cfg)
    check_output(destination, duration)
    log(f"  smoke: {destination}")
    log("  listen: the tone must sit inside a gap and the film must duck under it.")


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------


def render(job: Path, cfg: dict) -> None:
    """Write output.mp4 into `job`."""
    job = Path(job)
    require_binary("ffmpeg")
    require_binary("ffprobe")

    source = job / "input.mp4"
    if not source.is_file():
        raise SystemExit(f"No input.mp4 in {job}.")

    duration = float(read_json(job / "meta.json", default={}).get("duration") or 0.0)
    if not duration:
        duration = media_duration(source)

    segments = load_segments(job)
    if not segments:
        # An unchanged copy of the film is not audio description.
        raise SystemExit(
            f"No narration audio in {job} — nothing to mix. "
            f"Run the narrate and tts_fit stages first."
        )

    destination = job / "output.mp4"
    render_segments(job, segments, destination, duration, cfg)
    check_output(destination, duration)

    log(f"  mix: {len(segments)} segment(s) ducked into {duration:.2f}s")
    for position, segment in enumerate(segments):
        log(f"    {position}: {segment['start']:.2f}s  {segment['path'].name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="mix stage: duck the soundtrack under narration")
    parser.add_argument("job", type=Path, help="job directory")
    parser.add_argument("--smoke", action="store_true",
                        help="render a tone into the detected gaps as preview/smoke.mp4")
    parser.add_argument("--duck-threshold", type=float, help=f"default {DEFAULT_DUCK_THRESHOLD}")
    parser.add_argument("--duck-ratio", type=float, help=f"default {DEFAULT_DUCK_RATIO}")
    parser.add_argument("--duck-attack", type=float, help=f"ms, default {DEFAULT_DUCK_ATTACK}")
    parser.add_argument("--duck-release", type=float, help=f"ms, default {DEFAULT_DUCK_RELEASE}")
    args = parser.parse_args()

    options = {
        "duck_threshold": args.duck_threshold,
        "duck_ratio": args.duck_ratio,
        "duck_attack": args.duck_attack,
        "duck_release": args.duck_release,
    }
    (smoke if args.smoke else render)(args.job, options)
