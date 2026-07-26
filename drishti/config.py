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

# Characters per second through Bulbul v3 at pace 1.05, measured live today.
# We budget narration in characters, never in seconds: asked for "at most four
# seconds" the model returned a line that rendered in 6.23s, because seconds
# mean nothing to it. Characters it can count.
#
# We guessed Indic script would run *slower* per character than Latin, on the
# theory that an abugida packs more sound into each character. Measurement says
# the opposite, and the guess cost us a third of every Hindi window:
#
#   chars   duration   chars/s
#      53     3.56s      14.89     hi-IN, pace 1.05, speaker anand
#      85     5.26s      16.17
#     107     7.29s      14.67
#     109     7.28s      14.96
#
# Devanagari matras, nuktas and viramas are separate codepoints that carry no
# duration of their own, so "दरवाज़े" is seven characters but three syllables.
# Per character, Hindi therefore runs *further* than English, not less far.
# Rates are measured on lines of 50+ characters: a short line is dominated by
# fixed lead-in and lead-out silence and reads artificially slow (a 37-char
# line clocked 17.68 chars/s), which would over-budget exactly the short gaps
# that have the least room to recover.
SPEECH_RATES: dict[str, float] = {"en-IN": 14.0, "hi-IN": 15.0}
# Anything still unmeasured keeps the cautious rate — under-filling a window is
# recoverable, overrunning it is not. Both languages we have measured came in
# at 14–15, so this is deliberately pessimistic rather than an estimate.
DEFAULT_SPEECH_RATE = 11.0
# Aim short of the brim so the fit loop has room to re-pace instead of skip.
BUDGET_MARGIN = 0.9


# --------------------------------------------------------------------------
# Bulbul v3 limits — probed against the live API, not taken from the docs.
# The published docs claim temperature runs to 2.0; the API rejects anything
# above 1.0. pitch and loudness are v2-only and return HTTP 400 on v3.
# --------------------------------------------------------------------------
TTS_PACE_MIN, TTS_PACE_MAX = 0.5, 2.0
TTS_TEMPERATURE_MIN, TTS_TEMPERATURE_MAX = 0.01, 1.0
BASE_TTS_PACE = 1.05

# Measured pace sweep on a 126-character English line, live:
#
#   pace   duration   chars/s   speed-up vs 1.05   efficiency
#   1.05     6.64s      18.96         1.00x          100%
#   1.25     5.31s      23.74         1.25x          105%
#   1.35     5.40s      23.33         1.23x           96%
#   1.40     4.86s      25.93         1.37x          103%
#   1.50     4.60s      27.41         1.45x          101%
#
# Two things follow. Delivery scales essentially linearly to 1.5 with no
# plateau, so a faster tone really does buy proportionally more content — the
# char_budget scaling is sound. But Bulbul is not monotonic at fine grain:
# 1.25 and 1.30 rendered identically and 1.35 came out *slower* than 1.30.
# Pace steps below ~0.10 are inside that noise, so tone deltas are spaced
# wider than that or they are not really distinct.
#
# The ceiling that matters is not the API's 2.0 but speak.py's MAX_PACE (1.5),
# where the fit loop gives up and starts shortening. A tone opening at 1.40
# leaves the loop only 7% room to re-pace before it must cut words; at 1.35 it
# has 11%. That is why the fastest tone stops at 1.35 rather than 1.40 — the
# last 0.05 buys ~2% more content and costs a third of the recovery margin.
MAX_TONE_PACE = 1.35


def speech_rate(language: str | None) -> float:
    """Characters per second we expect Bulbul to deliver in this language."""
    return SPEECH_RATES.get(language or "", DEFAULT_SPEECH_RATE)


def char_budget(
    seconds: float, language: str | None, pace: float = BASE_TTS_PACE
) -> int:
    """How many characters of `language` fit in `seconds` of window.

    SPEECH_RATES were measured at pace 1.05, and delivery scales close enough
    to linearly with pace that a slower tone must be given a smaller budget.
    Omit `pace` and you get the pre-tone behaviour exactly.
    """
    scale = max(TTS_PACE_MIN, min(TTS_PACE_MAX, pace)) / BASE_TTS_PACE
    return max(0, int(seconds * speech_rate(language) * scale * BUDGET_MARGIN))


# --------------------------------------------------------------------------
# Tone presets
# --------------------------------------------------------------------------
#
# Bulbul v3 has NO emotion, style, pitch or loudness control — probed against
# the live API. The only knobs are pace, temperature and speaker. Swapping
# speaker changes who is talking, not how, which is why voice-hopping alone
# sounded flat.
#
# So expressivity is mostly a WRITING problem. Bulbul infers prosody from
# lexical and punctuation cues: measured on the same content, "A woman looks
# out of the window at the rain" renders in 2.56s, while "She watches the rain
# streak the glass... quietly, for a long moment" renders in 3.93s with a
# visibly different dynamic profile. The em-dash, the ellipsis and the clause
# structure did that, not a parameter.
#
# Each preset therefore carries both halves:
#   register     the writing instruction — the part that actually carries tone
#   pace_delta   added to BASE_TTS_PACE; NEVER NEGATIVE, see below
#   temperature  0.01–1.0; Sarvam's own default is 0.6
#
# WHY NO TONE EVER SLOWS THE PACE
# -------------------------------
# Gap space is the scarcest resource in this product, and char_budget scales
# with pace. Dropping to 0.87 for a somber line would cost ~17% of what we are
# able to say in that window — shorter lines, more shortening, more skips. That
# trades information a blind viewer cannot get anywhere else for a mood effect.
# The fit loop also only ever raises pace, so starting below base fights it.
#
# A slow *feeling* comes from punctuation instead: an ellipsis or a hard full
# stop buys a pause exactly where it earns one, rather than stretching every
# syllable. Same seconds, far better spent. So quiet tones hold base pace and
# do their work through temperature and register.
#
# Deliberately modulated rather than theatrical. Professional audio-description
# practice favours a narrator who does not compete with the film's own score,
# and an over-acted describer reads as patronising. Energy follows the scene;
# the voice stays trustworthy.


@dataclass(frozen=True)
class Tone:
    name: str
    pace_delta: float
    temperature: float
    register: str


NEUTRAL_TONE = "neutral"

TONE_PRESETS: dict[str, Tone] = {
    "neutral": Tone(
        "neutral", 0.00, 0.70,
        "Plain, even description. Say what is there and stop.",
    ),
    "tense": Tone(
        "tense", 0.20, 0.90,
        "Short clauses. Hard full stops. No adjectives that soften. "
        "Let the shortness carry the pressure.",
    ),
    "energetic": Tone(
        "energetic", 0.30, 0.90,
        "Active verbs, present tense, one clause running into the next. "
        "Keep it moving; never pause to qualify.",
    ),
    # Only +0.10: comedy timing lives in the pause before the payoff, which the
    # register buys with punctuation. Rushing a joke kills it.
    "playful": Tone(
        "playful", 0.10, 1.00,
        "Set it up, then land it — a comma or dash before the payoff. "
        "Understate the joke; never explain it.",
    ),
    # gentle and somber hold base pace on purpose — they slow the ear with
    # punctuation, not by stretching every syllable. See the note above.
    "gentle": Tone(
        "gentle", 0.00, 0.75,
        "Longer, unhurried phrasing. Use an ellipsis where a breath belongs, "
        "so the pause is written rather than drawled. Warm, never sentimental.",
    ),
    "somber": Tone(
        "somber", 0.00, 0.55,
        "Spare and still. Short declaratives separated by hard full stops — "
        "the stops do the slowing. No flourish, no adjectives of feeling.",
    ),
}

TONES: frozenset[str] = frozenset(TONE_PRESETS)


def normalize_tone(value: str | None) -> str:
    """Map anything to a known tone. Unrecognised input becomes neutral.

    A model picking its own label must never be able to fail the run — a
    surprising tone is a small cosmetic loss, a crash is a demo.
    """
    if not value:
        return NEUTRAL_TONE
    candidate = str(value).strip().lower()
    return candidate if candidate in TONE_PRESETS else NEUTRAL_TONE


def tone_params(
    tone: str | None, base_pace: float = BASE_TTS_PACE
) -> tuple[float, float]:
    """Return (pace, temperature) for a tone, clamped to what the API accepts."""
    preset = TONE_PRESETS[normalize_tone(tone)]
    pace = max(TTS_PACE_MIN, min(TTS_PACE_MAX, base_pace + preset.pace_delta))
    temperature = max(
        TTS_TEMPERATURE_MIN, min(TTS_TEMPERATURE_MAX, preset.temperature)
    )
    return round(pace, 3), round(temperature, 3)


def tone_register(tone: str | None) -> str:
    """The writing instruction for a tone — for the narrate prompt."""
    return TONE_PRESETS[normalize_tone(tone)].register


def tone_char_budget(seconds: float, language: str | None, tone: str | None) -> int:
    """Character budget adjusted for the pace this tone will actually use.

    A gentle line is spoken slower, so fewer characters fit in the same window.
    Budgeting at the base pace would send the fit loop into shorten-or-skip on
    exactly the lines we most want to hear.
    """
    pace, _ = tone_params(tone)
    return char_budget(seconds, language, pace)


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

            # Optional field: only assert on it once a run actually carries one.
            raw_tone = item.get("tone")
            if raw_tone is not None and str(raw_tone).strip().lower() not in TONES:
                problems.append(
                    f"{label}: tone {raw_tone!r} is not one of "
                    f"{', '.join(sorted(TONES))}"
                )

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
            if (
                complete
                and wav
                and not (job / Path(wav).name).is_file()
                and not Path(wav).is_file()
            ):
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
