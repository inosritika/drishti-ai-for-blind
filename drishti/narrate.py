"""Stage: narrate — OWNER: TANISHQ

Write one audio-description line per usable gap, in the language the pipeline
resolved.

reads:
    segments.json, transcript.txt, cfg["output_language"]

writes:
    narration.json  [{"gap_index": int, "start": float, "end": float,
                      "max_duration": float, "language": str, "text": str}, ...]

CHANGED 11:15 — you no longer pick which gap gets which scene beat. The `align`
stage (Aryan) does that and hands you segments.json:

    [{"gap_index", "start", "end", "max_duration", "char_budget",
      "language", "beats": [...], "score", "reason"}, ...]

Walk that list, write ONE line per entry, and copy gap_index / start / end /
max_duration straight through. No timing decisions live in this file any more —
that removes the whole class of overlap bug.

Tested behaviour to keep (see drishti_e2e.py: generate_narrations,
sarvam_chat_text):
  - ONE narration request per segment. This is an invariant: asking for
    narration per visual beat produced five overlapping lines all starting at
    the same timestamp.
  - model sarvam-30b, temperature 0.1, reasoning_effort=None
    (null returns content; "low" spends the token budget reasoning instead)
  - PLAIN TEXT output, not JSON. Structured output truncated twice on the real
    clip with "Unterminated string". Strip markdown fences and quotes.
  - transcript is CONTEXT ONLY: never narrate what the dialogue already says
  - never mention the camera; never invent detail to fill uncertainty
  - pass cache_ns="sarvam_chat"

BUDGET IN CHARACTERS, NOT SECONDS
  segment["char_budget"] is the length to ask for and to check. Do NOT ask the
  model for "at most N seconds" — that exact instruction returned a line which
  rendered in 6.23s against a 4s window, because seconds mean nothing to it.
  Characters it can count. The budget is already computed from max_duration,
  the output language's measured speech rate and a safety margin, so a line
  inside budget should fit on the first synthesis. If the model overshoots,
  ask again with the actual character count, then truncate at a sentence
  boundary. max_duration stays in narration.json for tts_fit to verify against.

WHAT TO SAY WITH THE BEATS YOU ARE GIVEN
  A segment carries EVERY beat that overlaps its window, which is usually more
  than fits — the fixture offers five beats and 163 characters. Choosing among
  them is your call, not align's, because compressing three beats into one verb
  clause is a language problem. The verified reference line did exactly that:

      "सूट पहने कई पुरुष उतरकर भीतर बने बैठक कक्ष में प्रवेश करते हैं"

  — cars, men, the cut inside and the boardroom, in one sentence. Put this
  priority in the prompt:
    1. changes of place. An unannounced cut is the most disorienting thing for
       a blind viewer and the one thing they cannot infer from the soundtrack.
    2. who is present, and whether that changed.
    3. what someone physically did.
    4. ambient continuation — drop this first when short of room.
  Merge related beats into clauses rather than spending a sentence each, and
  say nothing the dialogue is about to say itself.

  Each beat carries "when":
    "during"  happens inside the silence — the default material.
    "before"  happened just before the window opened; narrate in past tense.
    "after"   has NOT happened yet. At most a short clause of warning, and
              never the outcome — it spoils the scene. Drop it first.

LANGUAGE:
  - cfg["output_language"] is authoritative and always present. Never default,
    never re-detect, never hardcode hi-IN.
  - instruct the model with an explicit language name and script, e.g.
    "Hindi written in Devanagari", "Tamil written in Tamil script",
    "Indian English". A bare language code is not enough — passing hi-IN in the
    prompt still returned English.
  - VALIDATE before writing narration.json: Devanagari ratio for hi-IN/mr-IN,
    Tamil script for ta-IN, Latin script for en-IN. On mismatch, retry once,
    then fail the stage. Never let a wrong-script line reach TTS.
  - prefer natural words over English loanwords in Indic output
    ("बोर्डरूम" -> "बैठक कक्ष")
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import (
    SARVAM_BASE_URL,
    env_key,
    http_json,
    log,
    read_json,
    script_ok,
    write_json,
)
from .config import SUPPORTED_TTS

CHAT_URL = f"{SARVAM_BASE_URL}/v1/chat/completions"
CHAT_MODEL = "sarvam-30b"
CACHE_NS = "sarvam_chat"

# Explicit human name + script per code. The model needs the script spelled out
# — passing "hi-IN" alone in the prompt has produced English replies before.
LANGUAGE_PROMPT_NAME: dict[str, str] = {
    "en-IN": "Indian English (Latin script)",
    "hi-IN": "Hindi written in Devanagari",
    "mr-IN": "Marathi written in Devanagari",
    "bn-IN": "Bengali written in Bengali script",
    "gu-IN": "Gujarati written in Gujarati script",
    "kn-IN": "Kannada written in Kannada script",
    "ml-IN": "Malayalam written in Malayalam script",
    "od-IN": "Odia written in Odia script",
    "pa-IN": "Punjabi written in Gurmukhi",
    "ta-IN": "Tamil written in Tamil script",
    "te-IN": "Telugu written in Telugu script",
}

# Sentence terminators across the scripts we support. Used when the model
# overshoots the budget and we need to cut at the nearest complete sentence
# rather than mid-word.
_SENTENCE_ENDERS = tuple(".!?।॥？！")

# Strips ```lang ... ``` fences the model sometimes wraps a plain sentence in.
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\n?|\n?```$")

# Paired quote characters the model likes to wrap a "reply-only" answer in.
_QUOTE_PAIRS = ('""', "''", "“”", "‘’", "«»", "「」")


def _clean(text: str) -> str:
    """Strip fences, wrapping quotes, and outer whitespace from a model reply."""
    stripped = _FENCE_RE.sub("", text.strip()).strip()
    if len(stripped) >= 2:
        for opener, closer in _QUOTE_PAIRS:
            if stripped.startswith(opener) and stripped.endswith(closer):
                stripped = stripped[len(opener):-len(closer)].strip()
                break
    return stripped


def _sarvam_chat(system: str, user: str) -> str:
    """POST /v1/chat/completions and return the assistant's cleaned reply.

    reasoning_effort must serialise as JSON null. Setting it to "low" (the
    documented default) empirically returns empty content because the whole
    completion budget goes to internal reasoning tokens.
    """
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
    response = http_json(CHAT_URL, payload, headers, cache_ns=CACHE_NS)
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected chat response shape: {response!r}") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"chat returned empty or non-string content: {content!r}")
    return _clean(content)


def _truncate_at_sentence(text: str, limit: int) -> str:
    """Longest prefix of `text` under `limit` that ends on a sentence boundary.

    Last-resort recovery only. If nothing under the limit ends cleanly, cut at
    the last whitespace before the limit rather than mid-word.
    """
    if len(text) <= limit:
        return text
    window = text[:limit]
    best = max(window.rfind(ender) for ender in _SENTENCE_ENDERS)
    if best >= 0:
        return window[: best + 1].strip()
    space = window.rfind(" ")
    if space > 0:
        return window[:space].strip()
    return window.strip()


def _system_prompt(language: str) -> str:
    lang_name = LANGUAGE_PROMPT_NAME[language]
    return (
        "You write audio description for a blind or low-vision listener. Your\n"
        "line will be SPOKEN ALOUD by a text-to-speech engine into a silent gap\n"
        "in the film — the listener never sees it, they only hear it, and only\n"
        "for the duration of that gap before dialogue resumes.\n\n"
        f"Write in {lang_name}. Do not use any other language or script.\n"
        "Reply with the sentence only — no JSON, no quotes, no prefix, no explanation.\n\n"
        "Because this is spoken, not read:\n"
        "  - Write one complete, natural sentence a person would actually say.\n"
        "  - No bullet points, no lists, no headings, no parenthetical asides.\n"
        "  - No abbreviations, symbols, or numerals the TTS could mispronounce\n"
        "    (write \"three men\", not \"3 men\"; \"and\", not \"&\").\n"
        "  - Prefer natural, speakable phrasing.\n\n"
        "Content priorities, in order:\n"
        "  1. Change of place (an unannounced cut). The single most disorienting\n"
        "     thing for a blind listener and the one thing they cannot infer from sound.\n"
        "  2. Who is present, and whether that changed.\n"
        "  3. What someone physically did.\n"
        "  4. Ambient continuation — drop this first when short of room.\n\n"
        "Merge related beats into clauses rather than spending a sentence each.\n"
        "Never mention the camera or the shot. Never invent detail to fill\n"
        "uncertainty — if `uncertain_details` flags something, avoid it or hedge.\n"
        "Say nothing the dialogue is about to say itself.\n\n"
        "Each beat carries a `when`:\n"
        '  "during" — happens inside the silence; describe as it is.\n'
        '  "before" — happened just before the window; use past tense.\n'
        '  "after"  — has NOT happened yet. At most a short clause of warning;\n'
        "             never narrate the outcome. Drop it first when short of room.\n\n"
        "Prefer natural words in the target language over English loanwords in\n"
        "Indic output (e.g. Hindi should say बैठक कक्ष, not बोर्डरूम)."
    )


def _format_beat(beat: dict) -> str:
    entities = ", ".join(beat.get("entities", []) or []) or "—"
    uncertain = ", ".join(beat.get("uncertain_details", []) or []) or "—"
    return (
        f"- {float(beat['start']):.2f}s–{float(beat['end']):.2f}s "
        f"when={beat['when']} confidence={float(beat.get('confidence', 0.0)):.2f}\n"
        f"    event: {(beat.get('event') or '').strip()}\n"
        f"    entities: {entities}\n"
        f"    uncertain: {uncertain}"
    )


def _user_prompt(segment: dict, transcript: str, char_budget: int) -> str:
    beats = segment.get("beats", [])
    beats_block = "\n".join(_format_beat(beat) for beat in beats)
    transcript_block = transcript.strip() or "(no dialogue detected in this clip)"

    # `after` beats are look-ahead; they should not be counted toward the
    # coverage floor because forcing them in spoils the scene.
    coverable = [b for b in beats if b.get("when") in ("during", "before")]
    floor = max(1, int(char_budget * 0.6))

    if len(coverable) >= 2:
        coverage_min = min(len(coverable), 3)
        coverage_note = (
            f"There are {len(coverable)} beats marked `during` or `before`. "
            f"Cover at least {coverage_min} of them in one sentence — merge related "
            "beats into clauses rather than picking one and ignoring the rest. "
            "`after` beats are look-ahead: mention them briefly or drop them, "
            "never narrate the outcome."
        )
    else:
        coverage_note = (
            "There is one primary beat to describe. Add relevant detail from "
            "surrounding beats without padding."
        )

    return (
        f"Character budget: return between {floor} and {char_budget} characters. "
        f"Aim close to {char_budget} when there is more than one beat worth "
        "mentioning — the listener wants coverage, not brevity for its own sake. "
        f"Hard ceiling: {char_budget} characters.\n\n"
        f"Window: {float(segment['start']):.2f}s – {float(segment['end']):.2f}s "
        f"in the clip.\n\n"
        f"{coverage_note}\n\n"
        f"Visible beats:\n{beats_block}\n\n"
        "Dialogue transcript for context — DO NOT repeat what any of this says:\n"
        f'"""\n{transcript_block}\n"""\n\n'
        "Write one sentence of audio description within the character budget."
    )


def _generate_line(
    segment: dict, transcript: str, language: str, char_budget: int
) -> str:
    """One narration line for one segment. Two bounded recovery paths.

    Overshoot is retried once with the actual character count in the prompt,
    then hard-truncated at a sentence boundary. Wrong-script output is retried
    once with a stricter language reminder, then the stage fails — never let a
    wrong-script line reach TTS.
    """
    system = _system_prompt(language)
    user = _user_prompt(segment, transcript, char_budget)

    text = _sarvam_chat(system, user)

    if len(text) > char_budget:
        retry_user = (
            user
            + f"\n\nYour previous reply was {len(text)} characters, over the "
            f"{char_budget}-character limit. Rewrite the same idea under "
            f"{char_budget} characters."
        )
        text = _sarvam_chat(system, retry_user)
        if len(text) > char_budget:
            text = _truncate_at_sentence(text, char_budget)

    if not script_ok(text, language):
        lang_name = LANGUAGE_PROMPT_NAME[language]
        strict_system = (
            system
            + f"\n\nCRITICAL: your previous attempt was not in {lang_name}. "
            f"Write ONLY in {lang_name}. Do not use any other script."
        )
        text = _sarvam_chat(strict_system, user)
        if len(text) > char_budget:
            text = _truncate_at_sentence(text, char_budget)
        if not script_ok(text, language):
            raise SystemExit(
                f"narrate: model refused to write segment "
                f"{segment.get('gap_index')} in {language}. Got: {text[:80]!r}"
            )

    return text


def write(job: Path, cfg: dict) -> None:
    """Write narration.json into `job`, one entry per segments.json entry.

    cfg keys used: output_language (REQUIRED)
    """
    language = cfg.get("output_language")
    if not language:
        raise SystemExit(
            "narrate: cfg['output_language'] is required. The pipeline resolves "
            "it from language.json; do not call this stage standalone without one."
        )
    if language not in SUPPORTED_TTS:
        raise SystemExit(
            f"narrate: {language!r} is not a language Bulbul can speak. "
            f"Supported: {', '.join(sorted(SUPPORTED_TTS))}"
        )
    if language not in LANGUAGE_PROMPT_NAME:
        raise SystemExit(
            f"narrate: no prompt-name mapping for {language!r}. Add it to "
            f"LANGUAGE_PROMPT_NAME in drishti/narrate.py."
        )

    segments = read_json(job / "segments.json", default=[])
    if not segments:
        raise SystemExit(
            "narrate: segments.json is empty — nothing to narrate. Rerun align, "
            "or drop a reviewed segments.json into the job directory."
        )

    transcript_path = job / "transcript.txt"
    transcript = (
        transcript_path.read_text(encoding="utf-8") if transcript_path.is_file() else ""
    )

    log(f"  narrate: {len(segments)} segment(s) in {language}")

    narrations: list[dict[str, Any]] = []
    for segment in segments:
        char_budget = int(segment.get("char_budget", 0))
        if char_budget <= 0:
            raise SystemExit(
                f"narrate: segment gap_index={segment.get('gap_index')!r} has "
                f"char_budget={char_budget!r}. align should have dropped it."
            )

        text = _generate_line(segment, transcript, language, char_budget)

        narrations.append({
            "gap_index": segment["gap_index"],
            "start": float(segment["start"]),
            "end": float(segment["end"]),
            "max_duration": float(segment["max_duration"]),
            "language": language,
            "text": text,
        })
        preview = text if len(text) <= 60 else text[:60] + "…"
        log(f"  gap {segment['gap_index']} ({len(text)}/{char_budget} chars): {preview}")

    write_json(job / "narration.json", narrations)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m drishti.narrate <job-dir> [output_language]")
    lang = sys.argv[2] if len(sys.argv) > 2 else "en-IN"
    write(Path(sys.argv[1]), {"output_language": lang})
