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
  - model sarvam-105b, temperature 0.1, reasoning_effort=None
    (null returns content; "low" spends the token budget reasoning instead)
  - PLAIN TEXT output, not JSON. Structured output truncated twice on the real
    clip with "Unterminated string". Strip markdown fences and quotes.
  - transcript is CONTEXT ONLY: never narrate what the dialogue already says
  - never mention the camera; never invent detail to fill uncertainty
  - pass cache_ns="sarvam_chat"

BUDGET IN CHARACTERS, NOT SECONDS
  segment["char_budget"] is the length to aim for and to steer the prompt with.
  Do NOT ask the model for "at most N seconds" — that exact instruction returned
  a line which rendered in 6.23s against a 4s window, because seconds mean
  nothing to it. Characters it can count. The budget is computed from
  max_duration, the output language's measured speech rate and a safety margin,
  so a line inside budget should fit on the first synthesis.

  If the model overshoots the budget, ask ONCE more with the actual character
  count in the retry prompt. If it still overshoots, accept the complete
  sentence and pass it through — tts_fit's re-pace / shorten / skip loop will
  adapt duration. Never truncate mid-sentence here; a broken utterance is worse
  than a slightly-fast delivery. max_duration stays in narration.json for
  tts_fit to verify and adapt against.

  Coverage scales with budget: under 60 chars the whitelist asks for one beat;
  under 100, two; otherwise three. A 49-char window cannot hold three beats
  without collapsing grammar — asking for what actually fits is the whole
  point of the char budget in the first place.

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
from .config import SUPPORTED_TTS, tone_register

CHAT_URL = f"{SARVAM_BASE_URL}/v1/chat/completions"
CHAT_MODEL = "sarvam-105b"
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
        "The beats you receive are in chronological order. Your one sentence should\n"
        "trace the arc from the first `during` or `before` beat to the last —\n"
        "leaving out a middle beat leaves the listener disoriented.\n\n"
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


_NAME_IN_SCRIPT: dict[tuple[str, str], str] = {}


def localize_name(name: str, language: str) -> str:
    """The character's name written in the output language's own script.

    Handing the model an English name inside a Hindi prompt drags the whole
    line into Latin: first it answered "Mr Bean grimaced and" (plain English),
    then "Mr Bean ka haath mug ko side" (romanised Hindi). Both were correctly
    rejected by the script check, and no amount of restating the language in
    the note fixed it — the Latin name in front of it was the stronger signal.

    So we transliterate once per (name, language) and put the result in the
    note, leaving nothing Latin to copy. This is also simply right: a
    Devanagari listener should hear मिस्टर बीन, not an English word dropped
    into a Hindi sentence.

    Falls back to the original name if the model returns something in the
    wrong script — a Latin name is a blemish, a failed stage is a broken demo.
    """
    if script_ok(name, language):
        return name  # already in the right script (or the language is Latin)
    key = (name, language)
    if key in _NAME_IN_SCRIPT:
        return _NAME_IN_SCRIPT[key]

    lang_name = LANGUAGE_PROMPT_NAME.get(language, language)
    try:
        written = _sarvam_chat(
            f"You transliterate names into {lang_name}. Reply with the "
            f"transliterated name only — no explanation, no quotes.",
            f"Write this character name in {lang_name}: {name}",
        )
    except Exception:  # noqa: BLE001 — never fail the stage over a name
        written = ""
    if not written or not script_ok(written, language):
        written = name
    _NAME_IN_SCRIPT[key] = written
    return written


def _user_prompt(segment: dict, transcript: str, char_budget: int) -> str:
    beats = segment.get("beats", [])
    beats_block = "\n".join(_format_beat(beat) for beat in beats)
    transcript_block = transcript.strip() or "(no dialogue detected in this clip)"

    # `after` beats are look-ahead; they should not be counted toward the
    # coverage floor because forcing them in spoils the scene.
    coverable = [b for b in beats if b.get("when") in ("during", "before")]
    floor = max(1, int(char_budget * 0.6))

    # Coverage scales with budget: a 49-char window cannot hold three beats
    # without collapsing grammar. Ask for what actually fits.
    if char_budget < 60:
        max_covered = 1
    elif char_budget < 100:
        max_covered = 2
    else:
        max_covered = 3
    target_count = min(len(coverable), max_covered)

    if target_count >= 2:
        required = coverable[:target_count]
        checklist = "\n".join(
            f"  {i + 1}. {(beat.get('event') or '').strip()}"
            for i, beat in enumerate(required)
        )
        # The ceiling outranks coverage. The old wording ("MUST reference all")
        # made the model obey the checklist over the character budget — 159
        # chars against 87 on the Chalti brawl, which tts_fit could not rescue
        # and skipped. A complete short line under budget beats full coverage.
        coverage_note = (
            "Cover the following in this order of importance, and STOP adding "
            "content as you near the character ceiling — drop the later items "
            "without hesitation rather than run over:\n"
            f"{checklist}\n\n"
            "Merge related items into clauses; do not spend a sentence each. Do not "
            "invent detail beyond these items. `after` beats are look-ahead: mention "
            "them briefly or drop them, never narrate the outcome."
        )
    elif target_count == 1:
        beat_event = (coverable[0].get("event") or "").strip()
        if char_budget < 140:
            coverage_note = (
                f"Your one sentence should describe this beat: {beat_event}\n\n"
                "Keep it a natural, complete sentence. Do not force in the other beats; "
                "the budget is too tight for more than one clean clause. `after` beats "
                "are look-ahead: drop them entirely."
            )
        else:
            coverage_note = (
                f"The primary beat to describe: {beat_event}\n\n"
                "Unfold it across your sentences moment by moment, adding real "
                "detail from the surrounding beats without padding. `after` "
                "beats are look-ahead: drop them entirely."
            )
    else:
        coverage_note = (
            "There is one primary beat to describe. Add relevant detail from "
            "surrounding beats without padding."
        )

    # Characters a human named, as {name: visual description}. align has
    # already substituted the name wherever it could match the wording
    # mechanically; this covers the rest — "the man in the bowler hat", a
    # phrasing no pattern anticipated, or prose in another language. The model
    # links description to phrasing, which is the part it is actually good at.
    cast = segment.get("cast") or {}
    cast_note = ""
    if cast:
        language = segment.get("language") or "en-IN"
        rows = "\n".join(f"  - {localize_name(name, language)}: {description}"
                         for name, description in cast.items())
        # The names and descriptions here are English, and saying so in English
        # is enough to pull the whole line into English: asked for Hindi with a
        # cast note attached, the model returned "Mr Bean grimaced and" and the
        # script check rejected it. Restate the output language inside the note
        # and require the name in that script — which is also correct practice,
        # since a Devanagari listener should hear मिस्टर बीन, not a Latin word.
        lang_name = LANGUAGE_PROMPT_NAME[segment.get("language") or "en-IN"]
        cast_note = (
            "\n\nA viewer told us who is in this clip:\n" + rows + "\n"
            "When a beat refers to one of these people, use the NAME rather "
            "than describing them again. Anyone NOT listed keeps their "
            "description — never invent or guess a name for them.\n"
            f"These names are written in English for your reference only. Your "
            f"reply must still be entirely in {lang_name}, and each name must be "
            f"written in that same script.\n"
        )

    # Ritika reads the scene's mood; this turns it into a writing style.
    # Deliberately modulated, not theatrical — an over-acted describer reads as
    # patronising, and professional AD keeps the narrator out of the film's way.
    register = tone_register(segment.get("tone"))

    # A long silence with a one-line description leaves the listener abandoned:
    # every single-sentence run on the 28.7s Chaplin window filled at most 54%
    # of its budget. Ask for a counted number of SHORT sentences instead — one
    # moment each — sized at ~110 chars of spoken sentence. Short windows keep
    # the single-sentence ask, which the run history shows lands on budget.
    if char_budget >= 140:
        n_sentences = max(2, min(4, char_budget // 110))
        closing = (
            f"This is a LONG gap. Write EXACTLY {n_sentences} short sentences "
            "of audio description — one moment each, in chronological order. "
            f"The TOTAL of all {n_sentences} sentences together must stay under "
            f"the {char_budget}-character ceiling, so keep each sentence to "
            f"roughly {char_budget // n_sentences} characters."
        )
    else:
        closing = "Write one sentence of audio description within the character budget."

    return (
        f"{cast_note}"
        f"Register for this scene: {register} "
        "The register shapes word choice and rhythm only — it must never make "
        "the line longer.\n\n"
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
        f"{closing}"
    )


def _generate_line(
    segment: dict, transcript: str, language: str, char_budget: int
) -> str:
    """One narration line for one segment. One bounded recovery layer each.

    Overshoot is retried once with the actual character count in the prompt.
    If the retry still overshoots we accept it intact — tts_fit's re-pace /
    shorten / skip loop will adapt. Never chop mid-sentence: a broken
    utterance is worse than a slightly-fast one. Wrong-script output is
    retried once with a stricter language reminder, then the stage fails.
    """
    system = _system_prompt(language)
    user = _user_prompt(segment, transcript, char_budget)

    text = _sarvam_chat(system, user)

    if len(text) > char_budget:
        retry_user = (
            user
            + f"\n\nYour previous reply was {len(text)} characters, over the "
            f"{char_budget}-character target. Rewrite the same idea under "
            f"{char_budget} characters, keeping a complete sentence."
        )
        text = _sarvam_chat(system, retry_user)

    if not script_ok(text, language):
        lang_name = LANGUAGE_PROMPT_NAME[language]
        strict_system = (
            system
            + f"\n\nCRITICAL: your previous attempt was not in {lang_name}. "
            f"Write ONLY in {lang_name}. Do not use any other script."
        )
        text = _sarvam_chat(strict_system, user)
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
            "tone": segment.get("tone"),
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
