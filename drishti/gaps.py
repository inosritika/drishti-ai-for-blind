"""Stage: gaps — OWNER: NISHANT

Find the windows where nobody is speaking, and identify the spoken language.
This is our core Sarvam-depth claim: Saaras answers "was speech transcribed
here?", which works through continuous background music, where energy-based
silence detection and WebRTC VAD both fail.

reads:
    audio.wav, meta.json

writes:
    gaps.json       [{"start": float, "end": float, "duration": float}, ...]
    chunks.json     [{"index": int, "start": float, "end": float,
                      "has_speech": bool, "transcript": str,
                      "language_code": str|null, "language_probability": float|null}, ...]
    transcript.txt  all chunk transcripts joined, in order
    language.json   {"source_language": "hi-IN"|"en-IN"|...|"unknown",
                     "confidence": float,
                     "evidence": [{"language_code": str, "seconds": float}, ...]}

Tested behaviour to keep (see drishti_e2e.py: detect_gaps_with_saaras):
  - 1.5s non-overlapping chunks, split with the `wave` module
  - POST /speech-to-text, model saaras:v3, language_code="unknown"
  - empty transcript == no speech; merge consecutive silent chunks
  - 150 ms edge padding removed from BOTH ends of every gap
  - drop gaps shorter than min_gap after padding
  - pass cache_ns="saaras_stt" so a rerun costs nothing

Language detection (do not skip — the whole pipeline routes on this):
  - keep Saaras's language_code and language_probability for every chunk that
    HAS speech
  - pick the dominant language weighted by spoken chunk duration
  - write "unknown" — never a guess — when any of these hold:
      * no speech at all
      * fewer than 2 usable speech chunks
      * confidence < 0.70
      * no clearly dominant language
  - you decide the SOURCE language only. The pipeline decides the OUTPUT
    language. Nothing in this file mentions hi-IN as a default.

Keep energy-based silencedetect available as a secondary mode for quiet
room-noise clips (cfg["detector"] == "silence"), but Saaras is the default.

Three decisions this file makes, and why:

`confidence` is the duration-weighted MEAN language_probability of the dominant
language's chunks — read off fixtures/jobs/english_sample, whose 0.91 is exactly
the mean of its 0.89 and 0.93. It is deliberately NOT a share of spoken time,
which is why dominance needs its own separate test: a genuinely half-Hindi
half-English clip can have a confident probability on every single chunk.

A lone silent chunk between two speech chunks is treated as speech. A word
split across a chunk boundary can transcribe as empty in both halves, inventing
a gap where someone is talking — the one failure mode that puts narration over
dialogue. Bridging costs nothing real: a single 1.5s window is below min_gap
anyway, so no usable gap is ever lost.

A trailing chunk shorter than min_chunk is dropped rather than analysed. Silence
we never verified must never become a gap.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
import time
import unicodedata
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import (
    SARVAM_BASE_URL,
    env_key,
    http_multipart,
    log,
    read_json,
    run,
    write_json,
)
from .config import normalize_language

STT_URL = f"{SARVAM_BASE_URL}/speech-to-text"
STT_MODEL = "saaras:v3"

DEFAULT_CHUNK_SECONDS = 1.5
DEFAULT_MIN_GAP = 1.6
DEFAULT_EDGE_PADDING = 0.15
DEFAULT_NOISE_DB = -32.0
DEFAULT_CONCURRENCY = 4

# A trailing fragment shorter than this is never sent and never becomes a gap.
DEFAULT_MIN_CHUNK = 0.5

# Language routing thresholds.
MIN_SPEECH_CHUNKS = 2      # one confident chunk is not evidence
MIN_CONFIDENCE = 0.70      # mean language_probability of the dominant language
MIN_DOMINANCE = 0.60       # dominant language's share of spoken seconds
TIE_MARGIN = 0.05          # closer than this to the runner-up is a tie, not a winner

# How much of a second language makes a clip genuinely code-switched. Measured
# on real footage: a 194s English clip reported 3s of Hindi (2%) and another
# reported 3s Hindi plus 1.5s Gujarati (6%). Both are single-language clips with
# a stray exclamation, so "more than one language appeared" is not a usable
# signal — the runner-up has to be a real share of the dialogue.
CODE_SWITCH_SHARE = 0.15

# Sarvam rate-limits sustained chunk uploads. common.py retries 429s three times
# over ~6s, which a 130-chunk clip blows straight through.
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF = 5.0

# Transcribed junk that means "I heard music", not "I heard speech".
_JUNK = {"", "-", "--", "...", "…", "♪", "♪♪", "[music]", "(music)", "[music]."}

# A chunk needs this many words before it counts as speech. Set to 1 to keep
# every single-word transcript (the behaviour before this was measured).
#
# Measured over five clips: of 139 speech chunks, the 34 one-word ones are where
# essentially all of the hallucination lives — `Hello` appeared 11 times and
# `Okay` 12, always over music or percussion, never with a second word attached.
# The 93 three-plus-word chunks contained no false positives at all. On the Bean
# clip, which has no dialogue whatsoever, dropping one-word chunks turns 2 wrong
# gaps into the 1 correct gap spanning the whole clip.
#
# The cost is real and known: single-word dialogue exists ("Cindy?", "Right?",
# "What?"), and this throws it away too. That is survivable because a lone
# dropped chunk is put back by bridge_isolated_silence when both neighbours are
# speech, and because 1.5s is under min_gap, so one dropped chunk on its own can
# never open a gap.
DEFAULT_MIN_WORDS = 2


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


def _int_setting(cfg: dict, key: str, env: str, default: int) -> int:
    return int(_float_setting(cfg, key, env, float(default)))


# --------------------------------------------------------------------------
# speech vs music
# --------------------------------------------------------------------------


def is_speech(transcript: str) -> bool:
    """Did Saaras actually transcribe words, or hallucinate over the score?

    Whisper-family models emit '...', '♪' or a stray '[Music]' on pure music.
    Those are not speech, and counting them as speech would hide real gaps.

    Deliberately biased towards "yes". Calling music speech only costs us a gap
    we could have narrated in; calling speech music invents a gap and puts
    narration on top of dialogue. Only the second one can ruin a demo.

    Counting has to include combining marks: `\\w` does not match Devanagari
    vowel signs, so "मैं" measures as a single character and would otherwise be
    thrown away as noise.
    """
    text = (transcript or "").strip()
    if text.lower() in _JUNK:
        return False
    letters = [
        char for char in text
        if unicodedata.category(char)[0] in ("L", "M", "N")
    ]
    return len(letters) >= 1


def word_count(transcript: str) -> int:
    """Words in a transcript, counting any script.

    Splitting on whitespace is enough for every language Saaras returns — the
    point is only to tell a bare "Okay" apart from a phrase.
    """
    return len([word for word in (transcript or "").split() if word])


def drop_short_chunks(chunks: list[dict], min_words: int) -> int:
    """Un-flag speech chunks whose transcript is too short to trust.

    Runs BEFORE bridge_isolated_silence on purpose. A one-word chunk sitting
    between two real speech chunks gets dropped here and then put straight back
    by bridging, so genuine one-word dialogue inside a conversation survives —
    only one-word chunks surrounded by silence, which is what hallucination over
    music looks like, actually stay dropped.
    """
    if min_words <= 1:
        return 0
    dropped = 0
    for chunk in chunks:
        if chunk["has_speech"] and word_count(chunk["transcript"]) < min_words:
            chunk["has_speech"] = False
            dropped += 1
    return dropped


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def split_chunks(wav_path: Path, chunk_seconds: float, min_chunk: float) -> list[dict]:
    """Slice audio.wav into fixed windows. Returns [{index, start, end, bytes}]."""
    with wave.open(str(wav_path), "rb") as handle:
        rate = handle.getframerate()
        width = handle.getsampwidth()
        channels = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())

    frame_size = width * channels
    per_chunk = int(round(chunk_seconds * rate)) * frame_size

    chunks: list[dict] = []
    for index, offset in enumerate(range(0, len(frames), per_chunk)):
        payload = frames[offset:offset + per_chunk]
        start = (offset // frame_size) / rate
        end = start + len(payload) / frame_size / rate
        if end - start < min_chunk:
            log(f"  skipping {end - start:.2f}s tail at {start:.2f}s — too short to verify")
            continue
        chunks.append({
            "index": index,
            "start": round(start, 3),
            "end": round(end, 3),
            "bytes": payload,
            "rate": rate,
            "width": width,
            "channels": channels,
        })
    return chunks


def _write_chunk_wav(chunk: dict, path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(chunk["channels"])
        handle.setsampwidth(chunk["width"])
        handle.setframerate(chunk["rate"])
        handle.writeframes(chunk["bytes"])


# --------------------------------------------------------------------------
# Saaras
# --------------------------------------------------------------------------


def _first(payload: dict, *names: str):
    """Saaras field names have moved between versions; accept any of them."""
    for name in names:
        if payload.get(name) not in (None, ""):
            return payload[name]
    return None


def _stt_with_backoff(path: Path) -> dict:
    """One Saaras call, surviving sustained rate limiting.

    common.py already retries 429s, but only three times over about six
    seconds. A long clip sends far more chunks than that budget covers, and
    losing the stage means re-running everything not yet cached.
    """
    for attempt in range(1, RATE_LIMIT_RETRIES + 1):
        try:
            return http_multipart(
                STT_URL,
                {"model": STT_MODEL, "language_code": "unknown"},
                "file",
                path,
                {"api-subscription-key": env_key("SARVAM_API_KEY")},
                cache_ns="saaras_stt",
            )
        except RuntimeError as exc:
            rate_limited = "429" in str(exc) or "rate_limit" in str(exc).lower()
            if not rate_limited or attempt == RATE_LIMIT_RETRIES:
                raise
            wait = RATE_LIMIT_BACKOFF * attempt
            log(f"  rate limited, waiting {wait:.0f}s (attempt {attempt}/{RATE_LIMIT_RETRIES})")
            time.sleep(wait)
    raise AssertionError("unreachable")


def transcribe_chunk(chunk: dict, workdir: Path) -> dict:
    """One Saaras call. Cached on the chunk's bytes, so reruns are free."""
    path = workdir / f"chunk_{chunk['index']:03d}.wav"
    _write_chunk_wav(chunk, path)

    response = _stt_with_backoff(path)

    transcript = (_first(response, "transcript", "text") or "").strip()
    raw_language = _first(response, "language_code", "detected_language", "language")
    probability = _first(response, "language_probability", "language_confidence", "confidence")

    speech = is_speech(transcript)
    return {
        "index": chunk["index"],
        "start": chunk["start"],
        "end": chunk["end"],
        "has_speech": speech,
        "transcript": transcript,
        # Language is evidence only where there was something to hear.
        "language_code": (normalize_language(raw_language) or raw_language) if speech else None,
        "language_probability": float(probability) if speech and probability is not None else None,
    }


def transcribe_all(chunks: list[dict], concurrency: int) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="drishti-chunks-") as tmp:
        workdir = Path(tmp)
        if concurrency <= 1:
            results = [transcribe_chunk(chunk, workdir) for chunk in chunks]
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                results = list(pool.map(lambda c: transcribe_chunk(c, workdir), chunks))
    return sorted(results, key=lambda item: item["index"])


# --------------------------------------------------------------------------
# chunks -> gaps
# --------------------------------------------------------------------------


def bridge_isolated_silence(chunks: list[dict]) -> int:
    """A single silent chunk between two speech chunks is a split word, not a gap.

    Returns how many chunks were flipped, for the run log.
    """
    flipped = 0
    for position in range(1, len(chunks) - 1):
        if (
            not chunks[position]["has_speech"]
            and chunks[position - 1]["has_speech"]
            and chunks[position + 1]["has_speech"]
        ):
            chunks[position]["has_speech"] = True
            flipped += 1
    return flipped


def gaps_from_chunks(
    chunks: list[dict], edge_padding: float, min_gap: float, duration: float
) -> list[dict]:
    """Merge runs of silent chunks, pad both ends inward, drop what's too short."""
    gaps: list[dict] = []
    run_start: float | None = None
    run_end = 0.0

    for chunk in chunks:
        if chunk["has_speech"]:
            if run_start is not None:
                gaps.append({"start": run_start, "end": run_end})
                run_start = None
        else:
            if run_start is None:
                run_start = chunk["start"]
            run_end = chunk["end"]
    if run_start is not None:
        gaps.append({"start": run_start, "end": run_end})

    padded: list[dict] = []
    for gap in gaps:
        # Padding is uniform, including at t=0 where nothing precedes — that is
        # what fixtures/jobs/english_sample encodes (0.0 -> 0.15).
        start = max(0.0, gap["start"] + edge_padding)
        end = min(duration, gap["end"] - edge_padding)
        if end - start < min_gap:
            continue
        padded.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
        })
    return padded


def check_gaps(gaps: list[dict], duration: float) -> None:
    """gap_index is a positional index into this list (config.py:198), so order
    matters as much as the values. verify_job does not check any of this."""
    previous_end = -1.0
    for index, gap in enumerate(gaps):
        if gap["start"] < 0 or gap["end"] > duration + 0.001:
            raise SystemExit(f"gap {index} {gap} falls outside the clip (0–{duration:.2f}s)")
        if gap["start"] >= gap["end"]:
            raise SystemExit(f"gap {index} {gap} has no duration")
        if gap["start"] < previous_end:
            raise SystemExit(f"gap {index} {gap} overlaps the previous gap")
        previous_end = gap["end"]


# --------------------------------------------------------------------------
# language
# --------------------------------------------------------------------------


def detect_language(chunks: list[dict]) -> dict:
    """Report what Saaras heard. Never guess — the pipeline decides the output.

    confidence = duration-weighted mean language_probability over the dominant
    language's chunks (see fixtures/jobs/english_sample). Because that says
    nothing about dominance, share of spoken seconds is tested separately.
    """
    spoken = [c for c in chunks if c["has_speech"] and c.get("language_code")]

    seconds: dict[str, float] = {}
    weighted: dict[str, float] = {}
    for chunk in spoken:
        code = chunk["language_code"]
        span = chunk["end"] - chunk["start"]
        probability = chunk.get("language_probability")
        seconds[code] = seconds.get(code, 0.0) + span
        weighted[code] = weighted.get(code, 0.0) + span * (
            probability if probability is not None else 1.0
        )

    evidence = [
        {"language_code": code, "seconds": round(value, 3)}
        for code, value in sorted(seconds.items(), key=lambda item: -item[1])
    ]
    unknown = {"source_language": "unknown", "confidence": 0.0, "evidence": evidence}

    if len(spoken) < MIN_SPEECH_CHUNKS or not seconds:
        return {**unknown, "reason": "not enough speech to identify a language"}

    total = sum(seconds.values())
    ranked = sorted(seconds.items(), key=lambda item: -item[1])
    top_code, top_seconds = ranked[0]
    share = top_seconds / total if total else 0.0
    confidence = round(weighted[top_code] / top_seconds, 3) if top_seconds else 0.0

    runner_up_share = (ranked[1][1] / total) if len(ranked) > 1 else 0.0
    tied = len(ranked) > 1 and (share - runner_up_share) <= TIE_MARGIN

    if tied:
        # A genuine tie. Report both languages and let pipeline.py decide the
        # policy — a guess written here would sail past verify_job, because it
        # only checks that source and output agree.
        return {**unknown, "confidence": confidence, "code_switched": True,
                "reason": f"{ranked[0][0]} and {ranked[1][0]} are too close to call"}
    if share < MIN_DOMINANCE:
        return {**unknown, "confidence": confidence,
                "reason": f"no dominant language ({top_code} is only {share:.0%} of speech)"}
    if confidence < MIN_CONFIDENCE:
        return {**unknown, "confidence": confidence,
                "reason": f"{top_code} confidence {confidence:.2f} is below {MIN_CONFIDENCE}"}

    return {
        "source_language": top_code,
        "confidence": confidence,
        "evidence": evidence,
        # Genuinely mixed dialogue, not one stray exclamation.
        "code_switched": runner_up_share >= CODE_SWITCH_SHARE,
    }


# --------------------------------------------------------------------------
# secondary detector
# --------------------------------------------------------------------------


def gaps_from_silencedetect(
    wav_path: Path, noise_db: float, min_gap: float, edge_padding: float, duration: float
) -> list[dict]:
    """Energy-based fallback for quiet room-noise clips. Fails through music —
    that is the whole reason Saaras is the default."""
    # silencedetect reports on stderr, which common.run always captures.
    result = run([
        "ffmpeg", "-nostdin", "-v", "info", "-i", str(wav_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_gap}",
        "-f", "null", "-",
    ])
    stderr = result.stderr or ""

    starts = [float(value) for value in re.findall(r"silence_start: (-?[\d.]+)", stderr)]
    ends = [float(value) for value in re.findall(r"silence_end: (-?[\d.]+)", stderr)]
    if len(ends) < len(starts):
        ends.append(duration)  # silence running to the end of the clip

    # Each window is already a distinct silence — do NOT feed these through
    # gaps_from_chunks, which would merge adjacent entries into one run.
    gaps: list[dict] = []
    for window_start, window_end in zip(starts, ends):
        start = max(0.0, window_start + edge_padding)
        end = min(duration, window_end - edge_padding)
        if end - start < min_gap:
            continue
        gaps.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
        })
    return gaps


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------


def detect(job: Path, cfg: dict) -> None:
    """Write gaps.json, chunks.json, transcript.txt and language.json into `job`.

    cfg keys used: detector ("saaras"|"silence", default "saaras"),
                   chunk_seconds (default 1.5), min_gap (default 1.6),
                   edge_padding (default 0.15), noise_db (default -32.0),
                   min_chunk (default 0.5), concurrency (default 4),
                   bridge_silence (bool, default True),
                   min_words (default 2)
    """
    job = Path(job)
    wav_path = job / "audio.wav"
    if not wav_path.is_file():
        raise SystemExit(f"No audio.wav in {job}. Run the validate stage first.")

    meta = read_json(job / "meta.json", default={})
    duration = float(meta.get("duration") or 0.0)
    if not duration:
        raise SystemExit(f"meta.json in {job} has no duration. Run the validate stage first.")

    detector = str(_setting(cfg, "detector", "DRISHTI_DETECTOR", "saaras")).lower()
    chunk_seconds = _float_setting(cfg, "chunk_seconds", "DRISHTI_CHUNK_S", DEFAULT_CHUNK_SECONDS)
    min_gap = _float_setting(cfg, "min_gap", "DRISHTI_MIN_GAP_S", DEFAULT_MIN_GAP)
    edge_padding = _float_setting(cfg, "edge_padding", "DRISHTI_GAP_PAD_S", DEFAULT_EDGE_PADDING)
    noise_db = _float_setting(cfg, "noise_db", "DRISHTI_NOISE_DB", DEFAULT_NOISE_DB)
    min_chunk = _float_setting(cfg, "min_chunk", "DRISHTI_MIN_CHUNK_S", DEFAULT_MIN_CHUNK)
    concurrency = _int_setting(cfg, "concurrency", "DRISHTI_STT_CONCURRENCY", DEFAULT_CONCURRENCY)
    bridge = str(_setting(cfg, "bridge_silence", "DRISHTI_BRIDGE_SILENCE", "1")).lower() not in (
        "0", "false", "no", "off",
    )
    min_words = _int_setting(cfg, "min_words", "DRISHTI_MIN_WORDS", DEFAULT_MIN_WORDS)

    log(f"  gaps: detector={detector} chunk={chunk_seconds}s pad={edge_padding}s "
        f"min_gap={min_gap}s concurrency={concurrency} min_words={min_words}")

    if detector == "silence":
        # No STT, so there is no language evidence to report. Saying "unknown"
        # is the honest answer; guessing here would be laundered as fact.
        gaps = gaps_from_silencedetect(wav_path, noise_db, min_gap, edge_padding, duration)
        chunks: list[dict] = []
        language = {"source_language": "unknown", "confidence": 0.0, "evidence": [],
                    "reason": "silencedetect mode performs no speech recognition"}
        transcript = ""
    else:
        raw_chunks = split_chunks(wav_path, chunk_seconds, min_chunk)
        log(f"  {len(raw_chunks)} chunks -> Saaras")
        chunks = transcribe_all(raw_chunks, concurrency)

        dropped = drop_short_chunks(chunks, min_words)
        if dropped:
            log(f"  dropped {dropped} chunk(s) under {min_words} word(s) — "
                f"hallucination over music looks exactly like this")

        if bridge:
            flipped = bridge_isolated_silence(chunks)
            if flipped:
                log(f"  bridged {flipped} isolated silent chunk(s) — likely split words")

        gaps = gaps_from_chunks(chunks, edge_padding, min_gap, duration)
        language = detect_language(chunks)
        transcript = " ".join(c["transcript"].strip() for c in chunks if c["has_speech"]).strip()

    check_gaps(gaps, duration)

    write_json(job / "gaps.json", gaps)
    write_json(job / "chunks.json", chunks)
    write_json(job / "language.json", language)
    (job / "transcript.txt").write_text(transcript + "\n" if transcript else "", encoding="utf-8")

    spoken = sum(1 for c in chunks if c["has_speech"])
    log(f"  speech in {spoken}/{len(chunks)} chunks · {len(gaps)} usable gap(s)")
    for index, gap in enumerate(gaps):
        log(f"    gap {index}: {gap['start']:.2f} → {gap['end']:.2f}  ({gap['duration']:.2f}s)")
    detail = language.get("reason")
    log(f"  language: {language['source_language']} (confidence {language['confidence']:.2f})"
        + (f" — {detail}" if detail else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="gaps stage: find dialogue-free windows")
    parser.add_argument("job", type=Path, help="job directory containing audio.wav and meta.json")
    parser.add_argument("--detector", choices=["saaras", "silence"], help="default saaras")
    parser.add_argument("--chunk-seconds", type=float, help=f"default {DEFAULT_CHUNK_SECONDS}")
    parser.add_argument("--min-gap", type=float, help=f"default {DEFAULT_MIN_GAP}")
    parser.add_argument("--edge-padding", type=float, help=f"default {DEFAULT_EDGE_PADDING}")
    parser.add_argument("--noise-db", type=float, help=f"silence mode only, default {DEFAULT_NOISE_DB}")
    parser.add_argument("--concurrency", type=int, help=f"parallel Saaras calls, default {DEFAULT_CONCURRENCY}")
    parser.add_argument("--no-bridge", action="store_true", help="do not bridge isolated silent chunks")
    parser.add_argument("--min-words", type=int,
                        help=f"words needed to count as speech, default {DEFAULT_MIN_WORDS}")
    parser.add_argument("--keep-single-words", action="store_true",
                        help="trust one-word transcripts (turns the hallucination filter off)")
    args = parser.parse_args()

    detect(
        args.job,
        {
            "detector": args.detector,
            "chunk_seconds": args.chunk_seconds,
            "min_gap": args.min_gap,
            "edge_padding": args.edge_padding,
            "noise_db": args.noise_db,
            "concurrency": args.concurrency,
            "bridge_silence": False if args.no_bridge else None,
            "min_words": 1 if args.keep_single_words else args.min_words,
        },
    )
