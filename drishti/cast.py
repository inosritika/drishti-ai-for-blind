"""Stage: cast — OWNER: ARYAN.

Bind character NAMES a human gave us to the visual DESCRIPTORS Ritika's scene
beats already use, so narration can say "Mr Bean reaches for the kettle"
instead of "a suited man reaches for the kettle".

reads:
    scenes.json, input.mp4, cfg["cast"]

writes:
    cast.json  {"bindings": {"<descriptor>": "<name>", ...},
                "unmatched": ["<name>", ...],
                "candidates": ["<descriptor>", ...]}

WHY THE WORK IS SPLIT THIS WAY
------------------------------
Three parties, none of which can invent a name on its own:

  the human   supplies the NAME. A name cannot enter the pipeline unless a
              person asserted it.
  this stage  supplies only the BINDING — which of Ritika's descriptors that
              name refers to. It picks from a closed list or says nothing.
  align       does the substitution, mechanically and reproducibly.

That split is not defensive over-engineering, it is a measured result. Asked
openly "who is this character?", gpt-5.6 answered correctly on iconic
characters (Mr Bean, The Tramp — consistently, across frames) but on an
ordinary film scene it replied "Larry Summers": a confident, wrong
identification of a real living person. A blind listener cannot catch that.
So the model is never allowed to be the SOURCE of a name.

WHY A VISION CALL AND NOT TEXT MATCHING
---------------------------------------
Tempting alternative: look the name up somewhere, then match that description
against Ritika's descriptors as text. It fails on both counts that matter.

  1. It cannot resolve a tie. In the Chaplin ship clip "bowler-hatted man" and
     "bearded man" each appear in 7 of 8 beats. Only looking at the frame tells
     you which one is the Tramp.
  2. It cannot verify PRESENCE. Given "Sherlock Holmes" and a boardroom scene,
     description similarity happily matches "balding man in dark suit". The
     vision call returns UNKNOWN, because it can see that is not him.

The prompt leans hard on that second point: a wrong match is worse than no
match, so when in doubt answer UNKNOWN. Without that instruction the model
forced a match on the Sherlock Holmes case; with it, it refuses.

DEGRADATION
-----------
No names supplied      -> empty bindings, align does nothing, unchanged output.
Name not recognised    -> listed in `unmatched`, the descriptor survives.
Wrong binding          -> cannot be prevented, only surfaced. `bindings` is
                          rendered in the web transparency panel so a human
                          sees what we decided.
"""

from __future__ import annotations

import base64
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .common import OPENAI_URL, env_key, http_json, log, read_json, write_json

# Frames are cheap; one is enough to bind, and keeps the stage under a second
# of network time. Taken from the middle of the clip, where the principal
# character is most likely on screen.
FRAME_WIDTH = 640
# A name must map to a descriptor from this list or to nothing at all.
REFUSAL = "UNKNOWN"


def parse_names(raw: str | list[str] | None) -> list[str]:
    """Accept "Mr Bean, Teddy", a list, or a pasted synopsis.

    Free text is honoured on purpose: someone who wants to paste a cast list or
    a Wikipedia paragraph should not be blocked, and short comma-separated
    input is the common case. Anything long is left for the model to read.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    elif "\n" in raw or len(raw) > 120:
        # Looks like prose. Keep it whole; the prompt reads it as context.
        return [raw.strip()]
    else:
        items = re.split(r"[,;]", raw)
    return [item.strip() for item in items if item.strip()]


def candidate_descriptors(scenes: dict[str, Any]) -> list[str]:
    """Every distinct way Ritika's beats refer to something visible.

    Prefers a top-level `entities` registry when scenes.py provides one, since
    that carries descriptions and is free of the label drift we see per-beat
    (the same person appearing as "man" in one beat and "suited man" in the
    next). Falls back to collecting per-beat entity labels, so this stage never
    blocks on a scenes.py schema change.
    """
    registry = scenes.get("entities")
    if isinstance(registry, dict) and registry:
        return [
            f"{key}: {value}" if isinstance(value, str) and value.strip() else str(key)
            for key, value in registry.items()
        ]
    if isinstance(registry, list) and registry:
        out = []
        for item in registry:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("entity") or "").strip()
                desc = str(item.get("description") or "").strip()
                if name:
                    out.append(f"{name}: {desc}" if desc else name)
            elif isinstance(item, str) and item.strip():
                out.append(item.strip())
        if out:
            return out

    seen: dict[str, None] = {}
    for beat in scenes.get("beats", []) or []:
        for entity in beat.get("entities", []) or []:
            label = str(entity).strip()
            if label:
                seen.setdefault(label, None)
    return list(seen)


def descriptor_key(candidate: str) -> str:
    """"suited man: a thin man in tweed" -> "suited man"."""
    return candidate.split(":", 1)[0].strip()


def _grab_frame(video: Path, at_seconds: float) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
        frame = Path(handle.name)
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y",
             "-ss", f"{at_seconds:.2f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-1", str(frame)],
            check=True,
        )
        return frame.read_bytes()
    finally:
        frame.unlink(missing_ok=True)


def _prompt(names: list[str], candidates: list[str]) -> str:
    return (
        "Someone has typed the character names below. They may be WRONG — these "
        "characters may not appear in this clip at all. Verify against the frame.\n"
        + "\n".join(f"  - {name}" for name in names)
        + "\n\nA separate system described the people and objects visible, using "
        "only what it could see:\n"
        + "\n".join(f"  - {candidate}" for candidate in candidates)
        + "\n\nFor each name: if you RECOGNISE that specific fictional character in "
        "this frame, reply `Name => descriptor`, copying the descriptor text "
        f"EXACTLY as written above. If you do not recognise the character, or the "
        f"frame simply shows someone who is not that character, reply "
        f"`Name => {REFUSAL}`.\n\n"
        "A wrong match is far worse than no match — a blind listener cannot catch "
        f"the error. When in any doubt, answer {REFUSAL}. Never invent a "
        "descriptor that is not in the list. Reply with one line per name and "
        "nothing else."
    )


def _ask(frame: bytes, names: list[str], candidates: list[str]) -> str:
    payload = {
        "model": "gpt-5.6",
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": _prompt(names, candidates)},
            {"type": "input_image",
             "image_url": "data:image/jpeg;base64," + base64.b64encode(frame).decode()},
        ]}],
    }
    response = http_json(
        OPENAI_URL, payload,
        {"Authorization": f"Bearer {env_key('OPENAI_API_KEY')}"},
        cache_ns="cast_bind",
    )
    parts = [
        block["text"]
        for item in response.get("output", []) or []
        for block in item.get("content", []) or []
        if block.get("type") == "output_text"
    ]
    return "\n".join(parts).strip()


def parse_reply(reply: str, names: list[str], candidates: list[str]) -> dict[str, str]:
    """`Name => descriptor` lines -> {descriptor_key: name}, dropping refusals.

    Every descriptor is checked back against the candidate list: a value the
    model invented is discarded rather than substituted into narration.
    """
    by_key = {descriptor_key(c).lower(): descriptor_key(c) for c in candidates}
    wanted = {name.lower(): name for name in names}
    bindings: dict[str, str] = {}

    for line in reply.splitlines():
        if "=>" not in line:
            continue
        left, _, right = line.partition("=>")
        name = wanted.get(left.strip().strip("-• ").lower())
        descriptor = descriptor_key(right.strip().strip("`\"' "))
        if not name or not descriptor or descriptor.upper() == REFUSAL:
            continue
        canonical = by_key.get(descriptor.lower())
        if canonical and canonical not in bindings:
            bindings[canonical] = name
    return bindings


def bind(job: Path, cfg: dict) -> None:
    """Write cast.json into `job`."""
    names = parse_names(cfg.get("cast"))
    scenes = read_json(job / "scenes.json", default={})
    candidates = candidate_descriptors(scenes if isinstance(scenes, dict) else {})

    result: dict[str, Any] = {"bindings": {}, "unmatched": names, "candidates": candidates}

    if not names:
        write_json(job / "cast.json", result)
        log("  cast: no names given — narration will use descriptions only")
        return
    if not candidates:
        write_json(job / "cast.json", result)
        log("  cast: scenes.json named nothing visible, so there is nothing to bind")
        return

    video = job / "input.mp4"
    if not video.is_file():
        write_json(job / "cast.json", result)
        log("  cast: no input.mp4 to look at — skipping")
        return

    duration = float((read_json(job / "meta.json", default={}) or {}).get("duration") or 0.0)
    reply = _ask(_grab_frame(video, duration / 2 if duration else 1.0), names, candidates)

    bindings = parse_reply(reply, names, candidates)
    result["bindings"] = bindings
    result["unmatched"] = [n for n in names if n not in bindings.values()]
    write_json(job / "cast.json", result)

    for descriptor, name in bindings.items():
        log(f"  cast: {name} = {descriptor!r}")
    for name in result["unmatched"]:
        log(f"  cast: {name} not recognised here — keeping the description instead")


if __name__ == "__main__":
    import sys

    bind(Path(sys.argv[1]), {"cast": sys.argv[2] if len(sys.argv) > 2 else ""})
