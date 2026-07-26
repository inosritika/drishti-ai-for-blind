"""Stage: scenes — OWNER: RITIKA

Turn the video into factual, timestamped scene beats. Vision is a sensor here,
not the scored capability: it supplies visible facts, and Sarvam-30B turns those
facts into narration in whatever language the pipeline resolved.

reads:
    input.mp4, meta.json

writes:
    scenes.json  {"summary": str,
                  "beats": [{"start": float, "end": float, "event": str,
                             "entities": [str], "intensity": int,
                             "confidence": float, "uncertain_details": [str]}, ...]}

Tested behaviour to keep (see drishti_e2e.py: extract_frames, understand_scenes,
SCENE_SCHEMA):
  - frames at fps=1 (use 2 for fast action), scale=768:-2, q:v 4, max 45 frames
  - OpenAI Responses API, model from cfg/OPENAI_MODEL (default gpt-5.6)
  - send a text label "Frame N, t=X.XXs" immediately BEFORE each image
  - image detail "low"; images as base64 data URLs
  - strict json_schema output; every beat carries confidence and
    uncertain_details
  - prompt rule: describe only directly visible action. Never infer intent,
    relationships, identity, causation, dialogue, or off-screen events. When
    evidence is ambiguous, lower confidence and record the uncertainty instead
    of inventing a detail.
  - pass cache_ns="openai_vision"

LANGUAGE-NEUTRAL: write beats as stable factual text. This file never chooses
the narration language and never mentions hi-IN.

Fallback: if the API path misbehaves, hand-reviewed beats live in
demo/scene_fallbacks/<clip>.json. Copying one into a job dir as scenes.json is a
legitimate path — the pipeline skips any stage whose outputs already exist.
"""

from __future__ import annotations

from pathlib import Path


def understand(job: Path, cfg: dict) -> None:
    """Write scenes.json into `job`.

    cfg keys used: frame_fps (default 1.0), max_frames (default 45),
                   openai_model (default env OPENAI_MODEL or "gpt-5.6")
    """
    raise NotImplementedError("RITIKA: scenes stage")


if __name__ == "__main__":
    import sys

    understand(Path(sys.argv[1]), {})
