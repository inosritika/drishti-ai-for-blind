"""Stage: narrate — OWNER: TANISHQ

Write one audio-description line per usable gap, in the language the pipeline
resolved.

reads:
    gaps.json, scenes.json, transcript.txt, cfg["output_language"]

writes:
    narration.json  [{"gap_index": int, "start": float, "end": float,
                      "max_duration": float, "language": str, "text": str}, ...]

Tested behaviour to keep (see drishti_e2e.py: select_gap_candidates,
generate_narrations, sarvam_chat_text):
  - ONE narration request per selected gap. This is an invariant: asking for
    narration per visual beat produced five overlapping lines all starting at
    the same timestamp.
  - model sarvam-30b, temperature 0.1, reasoning_effort=None
    (null returns content; "low" spends the token budget reasoning instead)
  - PLAIN TEXT output, not JSON. Structured output truncated twice on the real
    clip with "Unterminated string". Strip markdown fences and quotes.
  - only beats with confidence >= 0.55, overlapping the gap
  - max_spoken_seconds = gap.duration - 0.25
  - prefer longer gaps with confident, high-intensity events; cap at
    cfg["max_segments"]
  - transcript is CONTEXT ONLY: never narrate what the dialogue already says
  - never mention the camera; never invent detail to fill uncertainty
  - pass cache_ns="sarvam_chat"

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
    """Write narration.json into `job`.

    cfg keys used: output_language (REQUIRED), max_segments (default 4)
    """
    raise NotImplementedError("TANISHQ: narrate stage")


if __name__ == "__main__":
    import sys

    write(Path(sys.argv[1]), {"output_language": "hi-IN"})
