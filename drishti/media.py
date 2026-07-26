"""Stage: validate — OWNER: NISHANT

Probe the clip and produce the mono WAV every downstream audio stage uses.

reads:
    input.mp4

writes:
    meta.json   {"duration": float, "has_audio": bool, "width": int,
                 "height": int, "fps": float}
    audio.wav   mono, 16 kHz, pcm_s16le

Tested behaviour to keep (see drishti_e2e.py: extract_audio, media_duration,
has_audio_stream):
  - mono / 16000 Hz / pcm_s16le, because that is what Saaras chunking expects
  - optional denoise `highpass=f=80,afftdn=nf=-25` for steady room hiss ONLY.
    It does not remove background music and must never be used to fight music.
  - if the source has no audio stream, synthesise a silent track of the same
    duration (anullsrc) so downstream stages have something to read
  - Saaras REST rejects clips over ~29.5s: raise a clear error above that

Two notes on why this file looks the way it does:

`meta["duration"]` is `common.media_duration(input.mp4)` and nothing else.
`config.verify_job` compares `media_duration(output.mp4)` against this number,
so if the two were measured differently the duration invariant would compare
two clocks and fail on a correct run.

Every filter value is cfg -> env -> default, never a literal in the ffmpeg
command. audio.wav is read ONLY by the gaps stage (mix reads input.mp4), so
denoise settings can never degrade delivered audio — they only change detection.
That makes them safe to sweep hard.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import wave
from pathlib import Path

from .common import has_stream, log, media_duration, require_binary, run, write_json

# Saaras REST refuses anything longer. Fail here, in two seconds, rather than
# after twenty chunk uploads.
DEFAULT_MAX_DURATION = 29.5

# Room hiss only. Raising these to fight background music defeats the entire
# premise of the project — see the module docstring.
DEFAULT_HIGHPASS_HZ = 80.0
DEFAULT_AFFTDN_NF = -25.0

SAMPLE_RATE = 16000

# audio.wav and input.mp4 should describe the same span of time. Beyond this,
# every gap timestamp is shifted against the video and nothing downstream can
# tell — narration just lands early or late.
DRIFT_WARN = 0.05


# --------------------------------------------------------------------------
# cfg -> env -> default
# --------------------------------------------------------------------------


def _setting(cfg: dict, key: str, env: str, default):
    """cfg wins, then the environment, then the built-in default."""
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


def _bool_setting(cfg: dict, key: str, env: str, default: bool) -> bool:
    value = _setting(cfg, key, env, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def audio_filter(cfg: dict) -> str:
    """The -af chain, or "" for no filtering at all."""
    if not _bool_setting(cfg, "denoise", "DRISHTI_DENOISE", True):
        return ""
    explicit = _setting(cfg, "audio_filter", "DRISHTI_AUDIO_FILTER", "")
    if explicit:
        return str(explicit)
    highpass = _float_setting(cfg, "highpass_hz", "DRISHTI_HIGHPASS_HZ", DEFAULT_HIGHPASS_HZ)
    afftdn = _float_setting(cfg, "afftdn_nf", "DRISHTI_AFFTDN_NF", DEFAULT_AFFTDN_NF)
    return f"highpass=f={highpass:g},afftdn=nf={afftdn:g}"


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------


def _parse_frame_rate(value: str) -> float:
    """ffprobe reports '24000/1001'. Downstream wants 23.976."""
    text = (value or "").strip()
    if not text or text in ("0/0", "N/A"):
        return 0.0
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            denom = float(denominator)
            return float(numerator) / denom if denom else 0.0
        except ValueError:
            return 0.0
    with contextlib.suppress(ValueError):
        return float(text)
    return 0.0


def probe_video(path: Path) -> dict:
    """width / height / fps of the first video stream. Zeros if there is none."""
    result = run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
            "-of", "json",
            str(path),
        ],
        capture=True,
    )
    streams = json.loads(result.stdout or "{}").get("streams") or [{}]
    stream = streams[0]

    fps = _parse_frame_rate(stream.get("avg_frame_rate", ""))
    if not fps:
        # Variable frame rate clips can report avg_frame_rate as 0/0.
        fps = _parse_frame_rate(stream.get("r_frame_rate", ""))

    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(fps, 3),
    }


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        return handle.getnframes() / rate if rate else 0.0


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def extract_audio(source: Path, destination: Path, cfg: dict) -> None:
    """Mono 16 kHz pcm_s16le from the first audio stream.

    Note when sweeping: ffmpeg only accepts afftdn nf between -80 and -20, so
    -25 is already near the gentle end. Anything outside that range is rejected
    by the filter, not by us.
    """
    command = [
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-i", str(source),
        "-map", "0:a:0",           # a 5.1 + stereo source must not pick at random
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le",
    ]
    chain = audio_filter(cfg)
    if chain:
        command += ["-af", chain]
    command.append(str(destination))
    try:
        run(command)
    except subprocess.CalledProcessError as exc:
        # Almost always a filter value ffmpeg won't accept. Show its complaint
        # instead of a traceback — this path gets hit constantly while tuning.
        detail = (exc.stderr or "").strip().splitlines()
        raise SystemExit(
            f"ffmpeg could not extract audio with -af {chain!r}:\n  "
            + "\n  ".join(detail[-3:] or ["(no output)"])
        )


def synthesise_silence(destination: Path, duration: float) -> None:
    """A silent track for a video with no audio stream at all."""
    run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=mono:sample_rate={SAMPLE_RATE}",
        "-t", f"{duration:.6f}",
        "-c:a", "pcm_s16le",
        str(destination),
    ])


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------


def prepare(job: Path, cfg: dict) -> None:
    """Write meta.json and audio.wav into `job`.

    cfg keys used: denoise (bool, default True), highpass_hz, afftdn_nf,
                   audio_filter (raw -af string, overrides the two above),
                   max_duration (default 29.5)
    """
    job = Path(job)
    source = job / "input.mp4"
    if not source.is_file():
        raise SystemExit(f"No input.mp4 in {job}. Copy a clip in, or use config.new_job().")

    require_binary("ffmpeg")
    require_binary("ffprobe")

    duration = media_duration(source)
    max_duration = _float_setting(cfg, "max_duration", "DRISHTI_MAX_DURATION", DEFAULT_MAX_DURATION)
    if duration > max_duration:
        raise SystemExit(
            f"{source.name} is {duration:.2f}s. Saaras REST rejects clips over "
            f"{max_duration:.1f}s — trim it, or raise max_duration if you know why."
        )

    has_audio = has_stream(source, "a")
    meta = {"duration": duration, "has_audio": has_audio, **probe_video(source)}

    destination = job / "audio.wav"
    chain = audio_filter(cfg) if has_audio else ""
    if has_audio:
        extract_audio(source, destination, cfg)
    else:
        # Downstream stages must always have a WAV to read. gaps will find no
        # speech, language.json will say "unknown", and the pipeline stops and
        # asks rather than narrating a clip in a language nobody established.
        synthesise_silence(destination, duration)

    write_json(job / "meta.json", meta)

    log(f"  validate: {duration:.2f}s  {meta['width']}x{meta['height']}  {meta['fps']:g}fps")
    log(f"  audio: {'extracted' if has_audio else 'SYNTHESISED SILENCE (no audio stream)'}"
        f"  filter={chain or 'none'}")

    # audio.wav and input.mp4 must agree on how long the clip is. If they do
    # not, every gap timestamp is offset against the video by the difference —
    # and no invariant downstream can see it, because gaps and narration drift
    # together. Evidence only; do not fail the stage on it.
    measured = wav_duration(destination)
    drift = abs(measured - duration)
    if drift > DRIFT_WARN:
        log(
            f"  WARN: audio.wav is {measured:.3f}s but input.mp4 is {duration:.3f}s "
            f"({drift * 1000:.0f}ms drift) — gap timestamps will be shifted against "
            f"the video by roughly that much."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="validate stage: probe a clip and extract mono audio")
    parser.add_argument("job", type=Path, help="job directory containing input.mp4")
    parser.add_argument("--no-denoise", action="store_true", help="skip the -af chain entirely")
    parser.add_argument("--highpass", type=float, help=f"highpass Hz (default {DEFAULT_HIGHPASS_HZ:g})")
    parser.add_argument("--afftdn-nf", type=float, help=f"afftdn noise floor (default {DEFAULT_AFFTDN_NF:g})")
    parser.add_argument("--audio-filter", help="raw -af chain, overrides --highpass/--afftdn-nf")
    parser.add_argument("--max-duration", type=float, help=f"clip length ceiling (default {DEFAULT_MAX_DURATION})")
    args = parser.parse_args()

    prepare(
        args.job,
        {
            "denoise": False if args.no_denoise else None,
            "highpass_hz": args.highpass,
            "afftdn_nf": args.afftdn_nf,
            "audio_filter": args.audio_filter,
            "max_duration": args.max_duration,
        },
    )
