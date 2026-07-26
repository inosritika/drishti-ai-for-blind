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

from pathlib import Path


def write(job: Path, cfg: dict) -> None:
    """Write narration.json into `job`, one entry per segments.json entry.

    cfg keys used: output_language (REQUIRED)
    """
    raise NotImplementedError("TANISHQ: narrate stage")


if __name__ == "__main__":
    import sys

    write(Path(sys.argv[1]), {"output_language": "hi-IN"})
