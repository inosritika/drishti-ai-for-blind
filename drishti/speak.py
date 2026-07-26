"""Stage: tts_fit — OWNER: TANISHQ

Synthesise each narration line with Bulbul and make it FIT its gap. Never trust
a promised duration: a line requested for "at most four seconds" rendered at
6.23s. Synthesise, measure, then adapt.

reads:
    narration.json

writes:
    narration_XX.wav   one per fitted segment
    narration.json     updated in place — each entry gains
                       "wav": str, "wav_duration": float, "pace": float,
                       and "skipped": true if it could not be made to fit

Tested behaviour to keep (see drishti_e2e.py: sarvam_tts, fit_tts_segments):
  - POST /text-to-speech, model bulbul:v3, base pace 1.05,
    speech_sample_rate 24000, output_audio_codec wav, temperature 0.35
  - response audio is base64 in audios[0]
  - measure the written WAV with ffprobe — never estimate
  - fit loop, in order:
        1. re-pace: pace = min(1.5, pace * actual / max_duration * 1.04)
        2. shorten once via sarvam-30b to
           floor(len(text) * max_duration / actual * 0.82) characters
        3. accept if actual <= max_duration + 0.08
        4. otherwise SKIP the segment and mark it — a segment that overruns
           its gap talks over dialogue, which is the one thing we never do
  - pass cache_ns="sarvam_tts"

LANGUAGE:
  - use the same cfg["output_language"] the narrate stage used, with a speaker
    that actually supports it. Bulbul supports fewer languages than sarvam-30b
    text generation.
  - EARLY TASK: verify which languages bulbul:v3 supports and which speaker
    works for each, then give Aryan that list — the pipeline's resolver checks
    against it before it commits to a language.
"""

from __future__ import annotations

import base64
from math import floor
from pathlib import Path
from typing import Any

from .common import (
    SARVAM_BASE_URL,
    env_key,
    http_json,
    log,
    media_duration,
    read_json,
    write_json,
)
from .config import FIT_TOLERANCE, SUPPORTED_TTS, tone_params

TTS_URL = f"{SARVAM_BASE_URL}/text-to-speech"
CHAT_URL = f"{SARVAM_BASE_URL}/v1/chat/completions"

TTS_MODEL = "bulbul:v3"
CHAT_MODEL = "sarvam-30b"

TTS_CACHE_NS = "sarvam_tts"
CHAT_CACHE_NS = "sarvam_chat"

# Empirical constants from the reference CLI experiment. Do not change these
# without a matching update to config.SPEECH_RATES: the align stage's char
# budget assumes narration comes out at pace 1.05.
BASE_PACE = 1.05
MAX_PACE = 1.5
SAMPLE_RATE = 24000
TEMPERATURE = 0.35


# --------------------------------------------------------------------------
# Bulbul call
# --------------------------------------------------------------------------


def _synthesize(text: str, language: str, speaker: str, pace: float,
                temperature: float = TEMPERATURE) -> bytes:
    """POST /text-to-speech and return the decoded WAV bytes.

    The base64 in audios[0] already carries the 44-byte RIFF header, so the
    bytes can be written straight to a file and read back by ffprobe.
    """
    payload: dict[str, Any] = {
        "text": text,
        "target_language_code": language,
        "model": TTS_MODEL,
        "speaker": speaker,
        "pace": round(pace, 3),
        "speech_sample_rate": SAMPLE_RATE,
        "output_audio_codec": "wav",
        "temperature": round(temperature, 3),
        "enable_preprocessing": True,
    }
    headers = {"api-subscription-key": env_key("SARVAM_API_KEY")}
    response = http_json(TTS_URL, payload, headers, cache_ns=TTS_CACHE_NS)
    try:
        b64 = response["audios"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected TTS response shape: {response!r}") from exc
    if not isinstance(b64, str) or not b64:
        raise RuntimeError(f"TTS returned empty audio: {response!r}")
    return base64.b64decode(b64)


# --------------------------------------------------------------------------
# shorten via sarvam-30b (one call, plain text)
# --------------------------------------------------------------------------


def _shorten(text: str, target_chars: int, language: str) -> str:
    """Ask sarvam-30b to rewrite `text` shorter, keeping the same language.

    Deliberately does not use narrate.py's LANGUAGE_PROMPT_NAME map: the input
    is already correctly scripted; we only need the model to keep it that way
    and cut length. Bypassing the full narrate prompt keeps this step under
    its own cache key and out of narrate's blast radius.
    """
    system = (
        "You compress one sentence to fit a character limit. Rewrite the input\n"
        "into a shorter sentence with the same meaning, in the SAME language and\n"
        "script as the input — do not translate. Keep the sentence natural and\n"
        "speakable (this text will be spoken aloud). Reply with the shortened\n"
        "sentence only, no quotes, no prefix, no explanation."
    )
    user = (
        f"Target: at most {target_chars} characters (count carefully).\n"
        f"Language: {language}.\n\n"
        f"Sentence to shorten:\n{text}"
    )
    payload: dict[str, Any] = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "reasoning_effort": None,
    }
    headers = {"api-subscription-key": env_key("SARVAM_API_KEY")}
    response = http_json(CHAT_URL, payload, headers, cache_ns=CHAT_CACHE_NS)
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected chat response shape: {response!r}") from exc
    if not isinstance(content, str):
        raise RuntimeError(f"shorten returned non-string content: {content!r}")
    return content.strip().strip('"').strip("'").strip("“”").strip()


# --------------------------------------------------------------------------
# fit loop for one segment
# --------------------------------------------------------------------------


def _fit(
    text: str,
    max_duration: float,
    language: str,
    speaker: str,
    wav_path: Path,
    tone: str | None = None,
) -> tuple[str, float, float] | None:
    """Return (final_text, final_pace, final_wav_duration) if fit; None on skip.

    Writes the accepted WAV to `wav_path`. On skip, no WAV is left at that path.
    """
    limit = max_duration + FIT_TOLERANCE

    # --- attempt 1: the tone's own pace --------------------------------------
    # A tense scene is read faster and a gentle one is not stretched — the
    # register slows a gentle line with punctuation rather than by drawling.
    # align budgeted for this same pace, so attempt 1 should already fit.
    pace, temperature = tone_params(tone, BASE_PACE)
    wav_bytes = _synthesize(text, language, speaker, pace, temperature)
    wav_path.write_bytes(wav_bytes)
    actual = media_duration(wav_path)
    log(f"    attempt 1: pace={pace:.2f} actual={actual:.2f}s (max {max_duration:.2f}s)")

    if actual <= limit:
        return text, pace, actual

    # --- attempt 2: re-pace -------------------------------------------------
    pace = min(MAX_PACE, pace * actual / max_duration * 1.04)
    wav_bytes = _synthesize(text, language, speaker, pace, temperature)
    wav_path.write_bytes(wav_bytes)
    actual = media_duration(wav_path)
    log(f"    attempt 2: pace={pace:.2f} actual={actual:.2f}s")

    if actual <= limit:
        return text, pace, actual

    # --- attempt 3: shorten once, then re-synthesize ------------------------
    target_chars = max(1, floor(len(text) * max_duration / actual * 0.82))
    if target_chars >= len(text):
        # Nothing to shave — either the text is already at floor(1) or the ratio
        # rounded up. Falling straight through to skip is the honest outcome.
        log(f"    shorten target {target_chars} >= current length {len(text)}; skipping")
        wav_path.unlink(missing_ok=True)
        return None

    shortened = _shorten(text, target_chars, language)
    log(f"    shortened: {len(text)}→{len(shortened)} chars target={target_chars}")

    if not shortened or shortened == text:
        wav_path.unlink(missing_ok=True)
        return None

    wav_bytes = _synthesize(shortened, language, speaker, pace, temperature)
    wav_path.write_bytes(wav_bytes)
    actual = media_duration(wav_path)
    log(f"    attempt 3: pace={pace:.2f} actual={actual:.2f}s")

    if actual <= limit:
        return shortened, pace, actual

    # --- give up ------------------------------------------------------------
    wav_path.unlink(missing_ok=True)
    return None


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def synthesize(job: Path, cfg: dict) -> None:
    """Synthesise narration_XX.wav files and update narration.json in place.

    cfg keys used: output_language (REQUIRED), speaker (default "anand"),
                   pace (default handled inside)
    """
    language = cfg.get("output_language")
    if not language:
        raise SystemExit(
            "tts_fit: cfg['output_language'] is required — the pipeline resolves it."
        )
    if language not in SUPPORTED_TTS:
        raise SystemExit(
            f"tts_fit: {language!r} is not a language Bulbul can speak. "
            f"Supported: {', '.join(sorted(SUPPORTED_TTS))}"
        )

    speaker = cfg.get("speaker") or "anand"

    narrations = read_json(job / "narration.json", default=[])
    if not narrations:
        raise SystemExit(
            "tts_fit: narration.json is empty or missing — run narrate first."
        )

    log(f"  tts_fit: {len(narrations)} line(s) in {language}, speaker {speaker}")

    for index, item in enumerate(narrations):
        # Already done in a prior run — leave it alone. `_clear_tts` in the
        # pipeline wipes these fields on --force, so if we see them here it is
        # deliberate resume.
        if item.get("skipped"):
            log(f"  segment {index}: previously skipped, leaving as-is")
            continue
        if item.get("wav_duration") is not None:
            log(f"  segment {index}: already synthesised, leaving as-is")
            continue

        text = (item.get("text") or "").strip()
        if not text:
            raise SystemExit(f"tts_fit: narration[{index}] has empty text")

        max_duration = float(item.get("max_duration", 0.0))
        if max_duration <= 0:
            raise SystemExit(
                f"tts_fit: narration[{index}] max_duration={max_duration}; "
                "should have come from a real gap"
            )

        wav_name = f"narration_{index:02d}.wav"
        wav_path = job / wav_name

        log(f"  segment {index} (max {max_duration:.2f}s): {text[:60]}"
            f"{'…' if len(text) > 60 else ''}")

        result = _fit(text, max_duration, language, speaker, wav_path,
                      tone=item.get("tone"))

        if result is None:
            item["skipped"] = True
            item.pop("wav", None)
            item.pop("wav_duration", None)
            item.pop("pace", None)
            log(f"  segment {index}: SKIPPED — could not fit into {max_duration:.2f}s")
            continue

        final_text, final_pace, final_duration = result
        item["text"] = final_text
        item["wav"] = wav_name
        item["wav_duration"] = round(final_duration, 3)
        item["pace"] = round(final_pace, 3)
        item.pop("skipped", None)
        log(
            f"  segment {index}: OK — {final_duration:.2f}s at pace {final_pace:.2f} "
            f"→ {wav_name}"
        )

    write_json(job / "narration.json", narrations)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: python -m drishti.speak <job-dir> [output_language] [speaker]"
        )
    lang = sys.argv[2] if len(sys.argv) > 2 else "en-IN"
    spk = sys.argv[3] if len(sys.argv) > 3 else "anand"
    synthesize(Path(sys.argv[1]), {"output_language": lang, "speaker": spk})
