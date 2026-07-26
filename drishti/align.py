"""Stage: align — OWNER: ARYAN.

The join between two verticals. Nishant reports where it is quiet; Ritika
reports what is visible. Deciding *which* visible thing is available in *which*
quiet window belongs to neither of them — it is a policy decision over their
evidence, exactly like language resolution.

reads:
    gaps.json, scenes.json, cfg["output_language"]

writes:
    segments.json  [{"gap_index": int, "start": float, "end": float,
                     "max_duration": float, "char_budget": int,
                     "language": str, "beats": [beat + "when", ...],
                     "score": float, "reason": str}, ...]

WHAT THIS STAGE DOES NOT DO
---------------------------
It does not decide what is worth saying. Every beat that survives the
confidence floor is handed to `narrate` intact, and the model chooses what to
lead with, what to merge into a clause, and what to drop.

That split is deliberate. Ranking beats by "importance" here would mean
inventing a proxy for viewer value out of `confidence` and `intensity`, and
that proxy is wrong: on the very first real clip the most valuable beat — the
cut from outside a building to an interior corridor, the one thing a blind
viewer cannot recover from context — carried the LOWEST confidence and the
LOWEST intensity of the five. Meanwhile compressing three beats into one verb
clause ("suited men get out and move inside to the boardroom") is a linguistic
operation, not a selection one. Dropping beats before the model sees them can
only destroy phrasing it would have found for free.

So align answers the mechanical questions only:

  WHERE can we speak            a gap, minus tail padding
  WHAT is available to say      every beat overlapping that window
  HOW MUCH room is there        char_budget, in the output language
  WHEN did it happen            before / during / after the window

The conflicts it resolves:

  1. A gap with no beat inside it. Real audio description describes what just
     happened, so we look back a few seconds before the gap opens.
  2. One beat spanning several gaps. Each beat is assigned to exactly ONE gap,
     so the same action is never described twice. A window the beat actually
     falls inside always wins over one that merely reaches for it.
  3. Several beats in one gap. All of them go to a single narration call. One
     gap, at most one narration, always — asking for narration per beat
     produced five overlapping lines that all started at the same timestamp.
  4. A beat that barely clips a gap. Overlap is measured, not assumed.
  5. A gap too short to say anything once tail padding is removed. Dropped.
  6. More gaps than a listener wants filled. Ranked, and only the best
     `max_segments` survive — narrating every silence is exhausting.

`score` and `reason` stay in the output on purpose: the UI shows why a window
was chosen, which is far more convincing than a bare timestamp.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import log, read_json, write_json
from .config import normalize_tone, tone_char_budget

# A narration must leave a little air before dialogue resumes.
TAIL_PADDING = 0.25
# Below this there is no point speaking at all.
MIN_NARRATION = 0.8
# Beats below this confidence are not trustworthy enough to narrate. This is
# the one filter that belongs here rather than in the prompt: it is a question
# about the evidence, not about the viewer, and the model has no way to tell a
# shaky observation from a solid one once the number is gone.
MIN_CONFIDENCE = 0.55
# How far before a gap we may reach to describe what just happened.
LOOKBACK = 4.0
# How far past a gap we may reach. Deliberately small: a little warning of what
# is coming orients the viewer, a lot of it spoils the scene.
LOOKAHEAD = 1.0
# A window this long is room enough for a full sentence; past it, extra silence
# does not make the window more worth choosing.
AMPLE_WINDOW = 6.0


# Articles to swallow when a descriptor becomes a name, so "a suited man
# stands" becomes "Mr Bean stands" and never "a Mr Bean stands".
_ARTICLE = re.compile(r"\b(?:a|an|the)\s+$", re.IGNORECASE)


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


# People-words used to find the head noun of an entity description, so
# "Dark-haired adult man in a brown suit" tells us the sentence will call this
# person some kind of "man".
_HEAD_NOUNS = (
    "man", "woman", "boy", "girl", "child", "person", "figure", "lady",
    "gentleman", "waiter", "driver", "officer", "passenger",
)


def _head_noun(description: str) -> str | None:
    """The word a sentence will most likely use for this person."""
    words = re.findall(r"[a-z]+", description.lower())
    return next((word for word in words if word in _HEAD_NOUNS), None)


def phrase_patterns(
    bindings: dict[str, str], entity_details: dict[str, str]
) -> list[tuple[re.Pattern[str], str]]:
    """Patterns matching how the EVENT TEXT refers to each bound person.

    scenes.py writes IDs into `entities` but deliberately keeps them out of the
    prose: the beat says "The suited man grimaces", never "man1 grimaces". So
    substituting the ID alone renames the entity list and leaves the sentence
    saying "the suited man" — exactly the thing we set out to fix.

    We bridge it with the head noun of the entity's own description. "man1:
    Dark-haired adult man in a brown suit" gives "man", so "the man", "a suited
    man" and "the dark-haired man" all become the name.

    Only when unambiguous. Two people whose descriptions are both some kind of
    "man" means a bare "the man" could be either, so neither is rewritten in
    prose and both keep their descriptions. entity_details makes that check
    exact — it lists people and nothing else.
    """
    heads: dict[str, list[str]] = {}
    words: dict[str, set[str]] = {}
    for entity_id, description in entity_details.items():
        head = _head_noun(str(description))
        if head:
            heads.setdefault(head, []).append(entity_id)
        words[entity_id] = set(re.findall(r"[a-z]{3,}", str(description).lower()))

    patterns: list[tuple[re.Pattern[str], str]] = []
    for entity_id, name in bindings.items():
        description = entity_details.get(entity_id)
        if not description:
            continue
        head = _head_noun(str(description))
        if not head:
            continue
        sharing = heads.get(head, [])

        if len(sharing) == 1:
            # Nobody else is a "man": any "the … man" is this person.
            patterns.append((
                re.compile(r"\b(?:a|an|the)\s+(?:[\w-]+\s+){0,3}?" + head + r"\b",
                           re.IGNORECASE),
                name,
            ))
            continue

        # Two men in frame. A bare "the man" really is ambiguous and stays put,
        # but the prose rarely leaves it bare — it says "the bowler-hatted man"
        # to tell them apart, and "bowler" appears in this entity's description
        # and no one else's. Match only on those distinguishing words.
        others: set[str] = set()
        for other_id in sharing:
            if other_id != entity_id:
                others |= words.get(other_id, set())
        distinctive = sorted(
            word for word in words.get(entity_id, set())
            if word not in others and word != head and len(word) > 3
        )
        if not distinctive:
            continue
        alternatives = "|".join(re.escape(word) for word in distinctive)
        # "the bowler-hatted man", "a mustached man", "the man with the cane"
        patterns.append((
            re.compile(
                r"\b(?:a|an|the)\s+(?:[\w-]*\s+)?(?:" + alternatives + r")[\w-]*\s+"
                r"(?:[\w-]+\s+)?" + head + r"\b",
                re.IGNORECASE,
            ),
            name,
        ))
    return patterns


def apply_cast(
    beats: list[dict[str, Any]],
    bindings: dict[str, str],
    entity_details: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Replace bound descriptors with character names, in text and entities.

    Pure and reversible: the original wording is kept on the beat as
    `event_described`, so the UI can show what was seen versus what will be
    said, and a bad binding is visible rather than silently baked in.

    Longest descriptor first — binding both "suited man" and "man" must not let
    the shorter one match inside the longer one and produce "suited Mr Bean".
    """
    if not bindings:
        return beats

    ordered = sorted(bindings.items(), key=lambda item: -len(item[0]))
    # Swallow the article in the same pass as the descriptor: "A suited man" is
    # one match, replaced by "Mr Bean". Doing it in two passes would need to
    # know which names came from a substitution and which were always there.
    patterns = [
        (re.compile(r"\b(?:a|an|the)\s+" + re.escape(descriptor) + r"\b", re.IGNORECASE),
         re.compile(r"\b" + re.escape(descriptor) + r"\b", re.IGNORECASE),
         name)
        for descriptor, name in ordered
    ]

    # How the prose refers to these people, when scenes.py gave us a registry.
    prose = phrase_patterns(bindings, entity_details or {})

    def rename(text: str) -> str:
        for with_article, bare, name in patterns:
            text = with_article.sub(name, text)
            text = bare.sub(name, text)
        for pattern, name in prose:
            text = pattern.sub(name, text)
        return text

    renamed: list[dict[str, Any]] = []
    for beat in beats:
        event = str(beat.get("event") or "")
        entities = [str(entity) for entity in (beat.get("entities") or [])]
        new_event = rename(event)
        new_entities = [rename(entity) for entity in entities]
        if new_event == event and new_entities == entities:
            renamed.append(beat)
            continue
        copy = dict(beat)
        copy["event_described"] = event  # what was actually seen
        copy["event"] = new_event
        copy["entities"] = new_entities
        renamed.append(copy)
    return renamed


def _placement(
    beat: dict[str, Any],
    gap_start: float,
    gap_end: float,
    window_start: float,
    window_end: float,
) -> tuple[tuple[int, float], str] | None:
    """Where this beat sits relative to a window, and how strongly it belongs.

    The claim is a (inside, overlap) tuple compared lexicographically, so a
    window the beat actually happens in always outranks one that only reaches
    for it — no penalty constant to tune, and no way for a long reach to
    outbid a short direct hit.
    """
    start, end = float(beat["start"]), float(beat["end"])

    inside = _overlap(start, end, gap_start, gap_end)
    if inside > 0:
        return (1, inside), "during"

    reach = _overlap(start, end, window_start, window_end)
    if reach <= 0:
        return None
    return (0, reach), ("before" if end <= gap_start else "after")


def select(
    gaps: list[dict[str, Any]],
    beats: list[dict[str, Any]],
    *,
    language: str | None = None,
    cast: dict[str, str] | None = None,
    tone: str | None = None,
    max_segments: int = 4,
    min_confidence: float = MIN_CONFIDENCE,
    lookback: float = LOOKBACK,
    lookahead: float = LOOKAHEAD,
) -> list[dict[str, Any]]:
    """Pure function: gaps + beats -> narration windows. Unit-testable, no I/O."""
    usable = [
        (index, beat)
        for index, beat in enumerate(beats)
        if float(beat.get("confidence", 0.0)) >= min_confidence
    ]

    # 1. every (window, beat) pairing that overlaps at all
    windows: list[dict[str, Any]] = []
    for gap_index, gap in enumerate(gaps):
        gap_start, gap_end = float(gap["start"]), float(gap["end"])
        max_duration = round(gap_end - gap_start - TAIL_PADDING, 3)
        if max_duration < MIN_NARRATION:
            continue

        window_start, window_end = gap_start - lookback, gap_end + lookahead
        claims: list[tuple[int, tuple[int, float], str, dict[str, Any]]] = []
        for beat_index, beat in usable:
            placed = _placement(beat, gap_start, gap_end, window_start, window_end)
            if placed is None:
                continue
            claim, when = placed
            claims.append((beat_index, claim, when, beat))

        if claims:
            windows.append({
                "gap_index": gap_index,
                "gap": gap,
                "max_duration": max_duration,
                "claims": claims,
            })

    # 2. every beat belongs to exactly one window — the one with the strongest
    #    claim on it — so a long action crossing dialogue is described once
    owner: dict[int, tuple[int, tuple[int, float]]] = {}
    for window in windows:
        for beat_index, claim, _, _ in window["claims"]:
            current = owner.get(beat_index)
            if current is None or claim > current[1]:
                owner[beat_index] = (window["gap_index"], claim)

    # 3. hand each window everything it owns, in the order it happened
    segments: list[dict[str, Any]] = []
    for window in windows:
        owned = [
            (claim, when, beat)
            for beat_index, claim, when, beat in window["claims"]
            if owner[beat_index][0] == window["gap_index"]
        ]
        if not owned:
            continue
        owned.sort(key=lambda item: float(item[2]["start"]))

        gap = window["gap"]
        max_duration = window["max_duration"]
        confidences = [float(beat.get("confidence", 0.0)) for _, _, beat in owned]
        best = max(confidences)
        # Room to speak, weighted by how much we trust the best thing we saw.
        room = min(1.0, max_duration / AMPLE_WINDOW)
        during = sum(1 for _, when, _ in owned if when == "during")

        segments.append({
            "gap_index": window["gap_index"],
            "start": float(gap["start"]),
            "end": float(gap["end"]),
            "max_duration": max_duration,
            # Budgeted at the pace this tone will actually be spoken at. A
            # slower tone fits fewer characters in the same window, and sizing
            # at the base pace would push exactly those lines into the fit
            # loop's shorten-or-skip path.
            "char_budget": tone_char_budget(max_duration, language, tone),
            "language": language,
            "tone": normalize_tone(tone),
            # {name: visual description} for characters a human named. narrate
            # renders this so the MODEL can link a name to however the prose
            # happens to phrase it — the general case that string substitution
            # cannot cover ("the man in the bowler hat", or any other language).
            "cast": dict(cast or {}),
            "beats": [dict(beat, when=when) for _, when, beat in owned],
            "score": round(room * best, 3),
            "reason": (
                f"{max_duration:.1f}s to speak, "
                f"{len(owned)} visible beat{'' if len(owned) == 1 else 's'}"
                f"{'' if during == len(owned) else f' ({during} inside the silence)'}, "
                f"best confidence {best:.2f}"
            ),
        })

    # 4. rank, cap, then restore chronological order for playback
    segments.sort(key=lambda item: (-item["score"], item["start"]))
    chosen = segments[:max_segments]
    chosen.sort(key=lambda item: item["start"])
    return chosen


def plan(job: Path, cfg: dict) -> None:
    """Write segments.json into `job`.

    cfg keys used: output_language, max_segments, min_confidence,
                   lookback, lookahead
    """
    gaps = read_json(job / "gaps.json", default=[])
    scenes = read_json(job / "scenes.json", default={})
    beats = scenes.get("beats", []) if isinstance(scenes, dict) else []

    # Character names, if the `cast` stage bound any. Applied here rather than
    # in cast.py so this stage stays the single place beats are transformed,
    # and so the substitution itself is pure and unit-testable.
    cast = read_json(job / "cast.json", default={})
    bindings = cast.get("bindings", {}) if isinstance(cast, dict) else {}
    details = scenes.get("entity_details", {}) if isinstance(scenes, dict) else {}
    if bindings:
        beats = apply_cast(beats, bindings, details if isinstance(details, dict) else {})

    if not gaps:
        raise SystemExit(
            "No dialogue-free windows were found, so there is nowhere safe to "
            "speak. Check gaps.json — and never widen the threshold until quiet "
            "dialogue counts as silence."
        )
    if not beats:
        raise SystemExit(
            "No scene beats to describe. Check scenes.json, or drop a reviewed "
            "fallback from demo/scene_fallbacks/ into the job directory."
        )

    named = {
        name: str(details.get(entity_id) or "")
        for entity_id, name in bindings.items()
        if isinstance(details, dict) and details.get(entity_id)
    }

    segments = select(
        gaps,
        beats,
        language=cfg.get("output_language"),
        cast=named,
        tone=scenes.get("tone") if isinstance(scenes, dict) else None,
        max_segments=int(cfg.get("max_segments", 4)),
        min_confidence=float(cfg.get("min_confidence", MIN_CONFIDENCE)),
        lookback=float(cfg.get("lookback", LOOKBACK)),
        lookahead=float(cfg.get("lookahead", LOOKAHEAD)),
    )

    if not segments:
        raise SystemExit(
            f"Found {len(gaps)} quiet window(s) and {len(beats)} visible beat(s), "
            "but nothing confident enough lines up with a window long enough to "
            "speak in. Try --max-segments, a lower --min-confidence, or a longer "
            "--lookback."
        )

    write_json(job / "segments.json", segments)
    for segment in segments:
        log(
            f"  gap {segment['gap_index']} "
            f"{segment['start']:.2f}-{segment['end']:.2f}s "
            f"(<={segment['max_duration']:.2f}s, "
            f"{segment['char_budget']} chars): {segment['reason']}"
        )


if __name__ == "__main__":
    import sys

    plan(Path(sys.argv[1]), {"output_language": "en-IN"})
