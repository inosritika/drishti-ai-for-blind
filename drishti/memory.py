"""Entity memory — OWNER: RITIKA — Increment 3, not part of Milestone 1.

Carries characters across clips so the second clip of the same content does not
re-describe everyone from scratch. This is a scored rubric line ("Memory and
Context"), not a nice-to-have.

reads:
    scenes.json, transcript.txt, narration.json  (of a finished job)
    registry path from cfg["registry"]

writes:
    the registry, a flat JSON file:
    {"entities": [{"descriptor": str, "name": str|null,
                   "first_seen": {"job": str, "t": float}}, ...]}

Build the easy version first:
  1. CONSISTENCY — clip 2 says "the same man in the grey suit" instead of
     describing him again. This alone demonstrates memory and is low risk.
  2. NAME BINDING — bind "man in a grey suit" to "Rohan" when the transcript
     names him. Harder: it needs a name spoken in clip 1 AND a correct link to
     a visual descriptor. Only attempt after (1) works.

The registry is injected into the narrate prompt for the NEXT job. Keep it a
flat file; no database.
"""

from __future__ import annotations

from pathlib import Path


def update(job: Path, cfg: dict) -> None:
    """Extract entities from a finished job into the registry."""
    raise NotImplementedError("RITIKA: entity memory — Increment 3")


def context_for_prompt(cfg: dict) -> str:
    """Return a short registry summary to inject into the narrate prompt."""
    raise NotImplementedError("RITIKA: entity memory — Increment 3")


if __name__ == "__main__":
    import sys

    update(Path(sys.argv[1]), {"registry": "runs/registry.json"})
