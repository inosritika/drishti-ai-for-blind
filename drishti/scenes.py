"""Stage: scenes — OWNER: RITIKA.

Create factual scene beats across the full video timeline.

The downstream contract is deliberately small and frozen:

    scenes.json
      {"summary": str, "tone": str,
       "entity_details": {entity_id: description},
       "beats": [{"start": float, "end": float, "event": str,
                  "entities": [str], "intensity": int,
                  "confidence": float, "uncertain_details": [str]}]}

Experiment controls and evidence are written separately so Tanishq can keep
consuming ``scenes.json`` without coordinating schema changes:

    scenes-param.json      resolved, reproducible analysis settings
    scenes-evidence.json   sampled-frame references for every beat

Vision output is language-neutral. Narration language is resolved elsewhere.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from .common import (
    OPENAI_URL,
    env_key,
    http_json,
    log,
    media_duration,
    read_json,
    require_binary,
    run,
    write_json,
)

PARAM_FILE = "scenes-param.json"
EVIDENCE_FILE = "scenes-evidence.json"
FRAMES_DIR = "scene_frames"

# These are fallback values, not hidden tuning constants. Every value can be
# overridden in scenes-param.json, through cfg, or with its documented env var.
DEFAULT_PARAMS: dict[str, Any] = {
    "provider": "openai",
    "model": "gpt-5.6",
    "frame_fps": 1.0,
    "refinement_frame_fps": 2.0,
    "max_frames": 45,
    "max_refinement_frames": 32,
    "frame_width": 768,
    "jpeg_quality": 4,
    "image_detail": "low",
    "min_beat_seconds": 0.25,
    "max_beat_seconds": 6.0,
    "refine_long_beats": True,
    "max_refinement_passes": 1,
    "max_refinement_windows": 3,
    "intensity_min": 1,
    "intensity_max": 5,
    "confidence_min": 0.0,
    "confidence_max": 1.0,
    "tone_max_words": 4,
    "max_entity_details": 20,
    "entity_description_max_words": 18,
    "timestamp_tolerance_seconds": 0.20,
    "require_evidence": True,
    "max_output_tokens": 6000,
    "reasoning_effort": "low",
    "prompt_version": "scene-beats-v4",
}

ENV_PARAMS: dict[str, str] = {
    "provider": "DRISHTI_SCENES_PROVIDER",
    "model": "OPENAI_MODEL",
    "frame_fps": "DRISHTI_SCENES_FRAME_FPS",
    "refinement_frame_fps": "DRISHTI_SCENES_REFINEMENT_FRAME_FPS",
    "max_frames": "DRISHTI_SCENES_MAX_FRAMES",
    "max_refinement_frames": "DRISHTI_SCENES_MAX_REFINEMENT_FRAMES",
    "frame_width": "DRISHTI_SCENES_FRAME_WIDTH",
    "jpeg_quality": "DRISHTI_SCENES_JPEG_QUALITY",
    "image_detail": "DRISHTI_SCENES_IMAGE_DETAIL",
    "min_beat_seconds": "DRISHTI_SCENES_MIN_BEAT_SECONDS",
    "max_beat_seconds": "DRISHTI_SCENES_MAX_BEAT_SECONDS",
    "refine_long_beats": "DRISHTI_SCENES_REFINE_LONG_BEATS",
    "max_refinement_passes": "DRISHTI_SCENES_MAX_REFINEMENT_PASSES",
    "max_refinement_windows": "DRISHTI_SCENES_MAX_REFINEMENT_WINDOWS",
    "intensity_min": "DRISHTI_SCENES_INTENSITY_MIN",
    "intensity_max": "DRISHTI_SCENES_INTENSITY_MAX",
    "confidence_min": "DRISHTI_SCENES_CONFIDENCE_MIN",
    "confidence_max": "DRISHTI_SCENES_CONFIDENCE_MAX",
    "tone_max_words": "DRISHTI_SCENES_TONE_MAX_WORDS",
    "max_entity_details": "DRISHTI_SCENES_MAX_ENTITY_DETAILS",
    "entity_description_max_words": "DRISHTI_SCENES_ENTITY_DESCRIPTION_MAX_WORDS",
    "timestamp_tolerance_seconds": "DRISHTI_SCENES_TIMESTAMP_TOLERANCE_SECONDS",
    "require_evidence": "DRISHTI_SCENES_REQUIRE_EVIDENCE",
    "max_output_tokens": "DRISHTI_SCENES_MAX_OUTPUT_TOKENS",
    "reasoning_effort": "DRISHTI_SCENES_REASONING_EFFORT",
    "prompt_version": "DRISHTI_SCENES_PROMPT_VERSION",
}

INT_PARAMS = {
    "max_frames",
    "max_refinement_frames",
    "frame_width",
    "jpeg_quality",
    "max_refinement_passes",
    "max_refinement_windows",
    "intensity_min",
    "intensity_max",
    "tone_max_words",
    "max_entity_details",
    "entity_description_max_words",
    "max_output_tokens",
}
FLOAT_PARAMS = {
    "frame_fps",
    "refinement_frame_fps",
    "min_beat_seconds",
    "max_beat_seconds",
    "confidence_min",
    "confidence_max",
    "timestamp_tolerance_seconds",
}
BOOL_PARAMS = {"refine_long_beats", "require_evidence"}

CANONICAL_BEAT_KEYS = (
    "start",
    "end",
    "event",
    "entities",
    "intensity",
    "confidence",
    "uncertain_details",
)

ENTITY_ID_RE = re.compile(r"[a-z][a-z0-9_]*[0-9]")


def _coerce_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


def _coerce_param(name: str, value: Any) -> Any:
    try:
        if name in BOOL_PARAMS:
            return _coerce_bool(value, name)
        if name in INT_PARAMS:
            return int(value)
        if name in FLOAT_PARAMS:
            return float(value)
        return str(value).strip()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid scene parameter {name}={value!r}") from exc


def resolve_params(job: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve settings with predictable precedence.

    Highest priority first:
      1. values passed in ``cfg`` by the pipeline or direct callers
      2. ``job/scenes-param.json`` (easy per-video experimentation)
      3. documented environment variables
      4. ``DEFAULT_PARAMS``
    """
    existing = read_json(job / PARAM_FILE, default={})
    if not isinstance(existing, dict):
        raise ValueError(f"{PARAM_FILE} must contain one JSON object")
    if isinstance(existing.get("params"), dict):
        existing = existing["params"]

    cfg_aliases = dict(cfg)
    if "openai_model" in cfg_aliases and "model" not in cfg_aliases:
        cfg_aliases["model"] = cfg_aliases["openai_model"]

    resolved: dict[str, Any] = {}
    for name, fallback in DEFAULT_PARAMS.items():
        env_name = ENV_PARAMS[name]
        raw = cfg_aliases.get(
            name,
            existing.get(name, os.getenv(env_name, fallback)),
        )
        resolved[name] = _coerce_param(name, raw)

    unknown = sorted(set(existing) - set(DEFAULT_PARAMS))
    if unknown:
        raise ValueError(
            f"unknown keys in {PARAM_FILE}: {', '.join(unknown)}; "
            "remove them so experiment settings cannot be silently ignored"
        )
    _validate_params(resolved)
    return resolved


def _validate_params(params: dict[str, Any]) -> None:
    positive = (
        "frame_fps",
        "refinement_frame_fps",
        "max_frames",
        "max_refinement_frames",
        "frame_width",
        "jpeg_quality",
        "min_beat_seconds",
        "max_beat_seconds",
        "max_output_tokens",
        "tone_max_words",
        "max_entity_details",
        "entity_description_max_words",
    )
    for name in positive:
        if params[name] <= 0:
            raise ValueError(f"{name} must be greater than zero")
    if params["max_beat_seconds"] < params["min_beat_seconds"]:
        raise ValueError("max_beat_seconds must be >= min_beat_seconds")
    if params["max_refinement_passes"] < 0:
        raise ValueError("max_refinement_passes cannot be negative")
    if params["max_refinement_windows"] < 0:
        raise ValueError("max_refinement_windows cannot be negative")
    if params["intensity_min"] > params["intensity_max"]:
        raise ValueError("intensity_min must be <= intensity_max")
    if params["confidence_min"] > params["confidence_max"]:
        raise ValueError("confidence_min must be <= confidence_max")
    if not 0 <= params["confidence_min"] <= 1:
        raise ValueError("confidence_min must be within 0..1")
    if not 0 <= params["confidence_max"] <= 1:
        raise ValueError("confidence_max must be within 0..1")
    if params["timestamp_tolerance_seconds"] < 0:
        raise ValueError("timestamp_tolerance_seconds cannot be negative")
    if params["provider"] != "openai":
        raise ValueError(
            f"provider {params['provider']!r} is not implemented yet; choose 'openai'"
        )
    if params["image_detail"] not in {"low", "high", "original", "auto"}:
        raise ValueError("image_detail must be low, high, original, or auto")
    if params["reasoning_effort"] not in {"none", "minimal", "low", "medium", "high"}:
        raise ValueError(
            "reasoning_effort must be none, minimal, low, medium, or high"
        )


def _clear_generated_frames(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for old_frame in directory.glob("frame_*.jpg"):
        old_frame.unlink()


def extract_frames(
    video: Path,
    output_dir: Path,
    *,
    fps: float,
    width: int,
    jpeg_quality: int,
    max_frames: int,
    start: float = 0.0,
    end: float | None = None,
) -> list[dict[str, Any]]:
    """Extract timestamped JPEG evidence using ffmpeg."""
    _clear_generated_frames(output_dir)
    # -nostdin matters when the CLI is piped through `tee`: without it ffmpeg
    # may try to read terminal controls from a background pipeline process
    # group, causing macOS to suspend both ffmpeg and its Python parent.
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start > 0:
        command.extend(["-ss", f"{start:.6f}"])
    command.extend(["-i", str(video)])
    if end is not None:
        command.extend(["-t", f"{max(0.0, end - start):.6f}"])
    command.extend(
        [
            "-vf",
            f"fps={fps:.8f},scale={width}:-2",
            "-q:v",
            str(jpeg_quality),
            "-frames:v",
            str(max_frames),
            str(output_dir / "frame_%04d.jpg"),
        ]
    )
    run(command)

    frames: list[dict[str, Any]] = []
    for index, path in enumerate(sorted(output_dir.glob("frame_*.jpg"))):
        timestamp = start + (index / fps)
        if end is not None:
            timestamp = min(timestamp, end)
        frames.append({"timestamp": round(timestamp, 3), "path": path})
    if not frames:
        raise RuntimeError(f"ffmpeg extracted no scene frames from {video}")
    return frames


def _image_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _beat_schema(params: dict[str, Any], start: float, end: float) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [*CANONICAL_BEAT_KEYS, "evidence_frame_times"],
        "properties": {
            "start": {"type": "number", "minimum": start, "maximum": end},
            "end": {"type": "number", "minimum": start, "maximum": end},
            "event": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "intensity": {
                "type": "integer",
                "minimum": params["intensity_min"],
                "maximum": params["intensity_max"],
            },
            "confidence": {
                "type": "number",
                "minimum": params["confidence_min"],
                "maximum": params["confidence_max"],
            },
            "uncertain_details": {
                "type": "array",
                "items": {"type": "string"},
            },
            "evidence_frame_times": {
                "type": "array",
                "items": {"type": "number", "minimum": start, "maximum": end},
            },
        },
    }


def _analysis_schema(
    params: dict[str, Any], start: float, end: float
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "tone", "entity_details", "beats"],
        "properties": {
            "summary": {"type": "string"},
            "tone": {"type": "string"},
            # Strict Structured Outputs cannot express arbitrary object keys
            # safely. The model returns rows; _canonical converts them to the
            # requested {"woman1": "description"} map in scenes.json.
            "entity_details": {
                "type": "array",
                "maxItems": params["max_entity_details"],
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "description"],
                    "properties": {
                        "id": {
                            "type": "string",
                            "pattern": r"^[a-z][a-z0-9_]*[0-9]$",
                        },
                        "description": {"type": "string"},
                    },
                },
            },
            "beats": {
                "type": "array",
                "minItems": 1,
                "items": _beat_schema(params, start, end),
            },
        },
    }


def _prompt(
    params: dict[str, Any],
    start: float,
    end: float,
    *,
    refinement: bool,
    known_entity_details: dict[str, str] | None = None,
) -> str:
    purpose = (
        "This is a targeted second look at a beat that may contain multiple "
        "visible changes."
        if refinement
        else "Build scene beats across the complete supplied timeline."
    )
    known_entities = (
        json.dumps(known_entity_details, ensure_ascii=False)
        if known_entity_details
        else "(none — create stable IDs for visually distinct people or characters)"
    )
    return f"""You are the visual evidence stage of an audio-description system.
{purpose}

Analyze only the timestamp-labelled frames from {start:.3f}s through {end:.3f}s.
Return factual, language-neutral visual scene beats covering that whole interval.

Rules:
- tone describes the overall visible mood in at most
  {params['tone_max_words']} words, such as "tense and suspenseful", "funny",
  "relaxing", or "somber". Return a short label, never a sentence or explanation.
- entity_details identifies visually distinct people or character-like figures.
  Give each one a stable lowercase ID ending in a number, with no spaces, such
  as woman1, woman2, man1, child1, or shadow1. Reuse a known ID only when the
  visible appearance matches that known description; otherwise create a new ID.
  Descriptions must contain only visible appearance and clothing, use at most
  {params['entity_description_max_words']} words, and never infer a name,
  relationship, personality, or inner emotion. Prefer visible evidence such as
  "wide eyes and tense expression" over "is frightened".
- Return at most {params['max_entity_details']} entity_details. Use these IDs in
  each beat's entities list for people and character-like figures; ordinary
  objects and settings may remain descriptive strings. Return a detail row for
  every ID used in this response, including reused known IDs, and do not return
  detail rows for entities absent from the supplied frames. Across known and
  newly created IDs, do not exceed {params['max_entity_details']} total IDs.
- Entity IDs are machine references for the entities lists only. Write summary
  and event text in natural language such as "the woman walks"; never write
  identifiers such as woman1 or shadow1 inside summary or event text.
- Known full-video entity IDs for this analysis: {known_entities}
- Start the first beat at {start:.3f} and end the final beat at {end:.3f}.
- Keep beats chronological and contiguous, with no gaps or overlaps.
- Start a new beat when the visible action, setting, subject, or shot context changes.
- Aim for beats no longer than {params['max_beat_seconds']:.3f}s when the frames
  contain meaningful visual changes. Do not invent changes just to hit a duration.
- Describe only directly visible action and objects.
- Never infer identity, intent, relationships, causation, dialogue, what is being
  discussed, or off-screen events. Do not write phrases such as "a meeting begins"
  unless the beginning itself is directly visible and unambiguous.
- uncertain_details is only for a visually ambiguous detail that another frame
  could resolve. It is not for missing audio, dialogue, or thoughts.
- intensity is an integer from {params['intensity_min']} to
  {params['intensity_max']}: the minimum means visually static, the maximum means
  rapid or dangerous visible action.
- confidence is from {params['confidence_min']} to {params['confidence_max']} and
  reflects visual support for the event.
- evidence_frame_times must list one or more supplied frame timestamps that visibly
  support that beat. Never invent a timestamp.
- If the visual evidence is insufficient, use a conservative event, lower
  confidence, and record only the visible ambiguity.

Prompt version: {params['prompt_version']}."""


def _request_content(
    frames: list[dict[str, Any]],
    prompt: str,
    detail: str,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for index, frame in enumerate(frames, start=1):
        content.append(
            {
                "type": "input_text",
                "text": f"Frame {index}, t={float(frame['timestamp']):.3f}s",
            }
        )
        content.append(
            {
                "type": "input_image",
                "image_url": _image_data_url(Path(frame["path"])),
                "detail": detail,
            }
        )
    return content


def _extract_response_json(response: dict[str, Any]) -> dict[str, Any]:
    for output in response.get("output", []):
        if output.get("type") != "message":
            continue
        for item in output.get("content", []):
            if item.get("type") == "refusal":
                raise RuntimeError(
                    f"OpenAI vision refused the request: {item.get('refusal')}"
                )
            if item.get("type") == "output_text":
                try:
                    parsed = json.loads(item["text"])
                except (KeyError, json.JSONDecodeError) as exc:
                    raise RuntimeError("OpenAI returned invalid structured JSON") from exc
                if not isinstance(parsed, dict):
                    raise RuntimeError("OpenAI scene response was not a JSON object")
                return parsed
    raise RuntimeError(
        f"OpenAI response had no output_text (status={response.get('status')!r})"
    )


def _call_openai(
    frames: list[dict[str, Any]],
    params: dict[str, Any],
    start: float,
    end: float,
    *,
    refinement: bool,
    known_entity_details: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": params["model"],
        "input": [
            {
                "role": "user",
                "content": _request_content(
                    frames,
                    _prompt(
                        params,
                        start,
                        end,
                        refinement=refinement,
                        known_entity_details=known_entity_details,
                    ),
                    params["image_detail"],
                ),
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "drishti_scene_analysis",
                "strict": True,
                "schema": _analysis_schema(params, start, end),
            }
        },
        "max_output_tokens": params["max_output_tokens"],
    }
    if params["reasoning_effort"] != "none":
        payload["reasoning"] = {"effort": params["reasoning_effort"]}

    response = http_json(
        OPENAI_URL,
        payload,
        {"Authorization": f"Bearer {env_key('OPENAI_API_KEY')}"},
        cache_ns="openai_vision",
    )
    return _extract_response_json(response)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _nearest_frame(
    timestamp: float,
    frames: list[dict[str, Any]],
    tolerance: float,
) -> dict[str, Any] | None:
    if not frames:
        return None
    nearest = min(frames, key=lambda frame: abs(float(frame["timestamp"]) - timestamp))
    if abs(float(nearest["timestamp"]) - timestamp) > tolerance:
        return None
    return nearest


def normalize_analysis(
    analysis: dict[str, Any],
    *,
    start: float,
    end: float,
    params: dict[str, Any],
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate model semantics that JSON Schema alone cannot guarantee."""
    if not isinstance(analysis, dict):
        raise ValueError("scene analysis must be a JSON object")
    summary = analysis.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("scene summary must be a non-empty string")
    tone = analysis.get("tone")
    if not isinstance(tone, str) or not tone.strip():
        raise ValueError("scene tone must be a non-empty string")
    tone = " ".join(tone.strip().split())
    if len(tone.split()) > params["tone_max_words"]:
        raise ValueError(
            f"scene tone has {len(tone.split())} words; maximum is "
            f"{params['tone_max_words']}"
        )
    entity_rows = analysis.get("entity_details")
    if not isinstance(entity_rows, list):
        raise ValueError("scene entity_details must be a list in model output")
    if len(entity_rows) > params["max_entity_details"]:
        raise ValueError(
            f"scene has {len(entity_rows)} entity details; maximum is "
            f"{params['max_entity_details']}"
        )
    entity_details: dict[str, str] = {}
    for index, row in enumerate(entity_rows):
        label = f"entity_details[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{label} must be an object")
        entity_id = row.get("id")
        description = row.get("description")
        if not isinstance(entity_id, str) or not ENTITY_ID_RE.fullmatch(entity_id):
            raise ValueError(
                f"{label}.id must be lowercase, start with a letter, end in "
                "a number, and contain no spaces"
            )
        if entity_id in entity_details:
            raise ValueError(f"duplicate entity id {entity_id!r}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"{label}.description must be a non-empty string")
        description = " ".join(description.strip().split())
        description_words = len(description.split())
        if description_words > params["entity_description_max_words"]:
            raise ValueError(
                f"{label}.description has {description_words} words; maximum is "
                f"{params['entity_description_max_words']}"
            )
        entity_details[entity_id] = description
    source_beats = analysis.get("beats")
    if not isinstance(source_beats, list) or not source_beats:
        raise ValueError("scene analysis must contain at least one beat")

    tolerance = params["timestamp_tolerance_seconds"]
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(source_beats):
        label = f"beats[{index}]"
        if not isinstance(source, dict):
            raise ValueError(f"{label} must be a JSON object")
        beat_start = _number(source.get("start"), f"{label}.start")
        beat_end = _number(source.get("end"), f"{label}.end")
        if beat_end <= beat_start:
            raise ValueError(f"{label} must end after it starts")
        if beat_start < start - tolerance or beat_end > end + tolerance:
            raise ValueError(
                f"{label} spans {beat_start:.3f}..{beat_end:.3f}, outside "
                f"{start:.3f}..{end:.3f}"
            )

        if index == 0:
            if abs(beat_start - start) > tolerance:
                raise ValueError(
                    f"first beat starts at {beat_start:.3f}s, expected {start:.3f}s"
                )
            beat_start = start
        else:
            previous_end = normalized[-1]["end"]
            if abs(beat_start - previous_end) > tolerance:
                relation = "gap" if beat_start > previous_end else "overlap"
                raise ValueError(
                    f"{relation} between beats[{index - 1}] and {label}: "
                    f"{previous_end:.3f}s vs {beat_start:.3f}s"
                )
            beat_start = previous_end

        if index == len(source_beats) - 1:
            if abs(beat_end - end) > tolerance:
                raise ValueError(
                    f"final beat ends at {beat_end:.3f}s, expected {end:.3f}s"
                )
            beat_end = end
        if beat_end - beat_start < params["min_beat_seconds"]:
            raise ValueError(
                f"{label} is only {beat_end - beat_start:.3f}s; minimum is "
                f"{params['min_beat_seconds']:.3f}s"
            )

        event = source.get("event")
        if not isinstance(event, str) or not event.strip():
            raise ValueError(f"{label}.event must be a non-empty string")
        entities = source.get("entities")
        if not isinstance(entities, list) or not all(
            isinstance(item, str) and item.strip() for item in entities
        ):
            raise ValueError(f"{label}.entities must be a list of non-empty strings")
        uncertain = source.get("uncertain_details")
        if not isinstance(uncertain, list) or not all(
            isinstance(item, str) and item.strip() for item in uncertain
        ):
            raise ValueError(
                f"{label}.uncertain_details must be a list of non-empty strings"
            )

        intensity = source.get("intensity")
        if isinstance(intensity, bool) or not isinstance(intensity, int):
            raise ValueError(f"{label}.intensity must be an integer")
        if not params["intensity_min"] <= intensity <= params["intensity_max"]:
            raise ValueError(
                f"{label}.intensity {intensity} is outside "
                f"{params['intensity_min']}..{params['intensity_max']}"
            )
        confidence = _number(source.get("confidence"), f"{label}.confidence")
        if not params["confidence_min"] <= confidence <= params["confidence_max"]:
            raise ValueError(
                f"{label}.confidence {confidence} is outside "
                f"{params['confidence_min']}..{params['confidence_max']}"
            )

        evidence_values = source.get("evidence_frame_times", [])
        if not isinstance(evidence_values, list):
            raise ValueError(f"{label}.evidence_frame_times must be a list")
        evidence: list[dict[str, Any]] = []
        for value in evidence_values:
            timestamp = _number(value, f"{label}.evidence_frame_times[]")
            frame = _nearest_frame(timestamp, frames, tolerance)
            if frame is None:
                raise ValueError(
                    f"{label} cites {timestamp:.3f}s, which is not a sampled frame"
                )
            frame_time = float(frame["timestamp"])
            if frame_time < beat_start - tolerance or frame_time > beat_end + tolerance:
                raise ValueError(
                    f"{label} cites frame {frame_time:.3f}s outside its own interval"
                )
            if frame not in evidence:
                evidence.append(frame)
        if params["require_evidence"] and not evidence:
            raise ValueError(f"{label} has no evidence-frame reference")

        normalized.append(
            {
                "start": round(beat_start, 3),
                "end": round(beat_end, 3),
                "event": event.strip(),
                "entities": [item.strip() for item in entities],
                "intensity": intensity,
                "confidence": round(confidence, 4),
                "uncertain_details": [item.strip() for item in uncertain],
                "evidence_frames": evidence,
            }
        )

    referenced_ids = {
        entity
        for beat in normalized
        for entity in beat["entities"]
        if ENTITY_ID_RE.fullmatch(entity)
    }
    missing_details = sorted(referenced_ids - set(entity_details))
    if missing_details:
        raise ValueError(
            "entity IDs used in beats have no entity_details row: "
            + ", ".join(missing_details)
        )
    unused_details = sorted(set(entity_details) - referenced_ids)
    if unused_details:
        raise ValueError(
            "entity_details rows are not referenced by any beat: "
            + ", ".join(unused_details)
        )

    return {
        "summary": summary.strip(),
        "tone": tone,
        "entity_details": entity_details,
        "beats": normalized,
    }


def _reconcile_entity_details(
    normalized: dict[str, Any],
    params: dict[str, Any],
) -> None:
    """Keep the full-video entity map consistent after beat refinement."""
    referenced_ids = {
        entity
        for beat in normalized["beats"]
        for entity in beat["entities"]
        if ENTITY_ID_RE.fullmatch(entity)
    }
    missing_details = sorted(referenced_ids - set(normalized["entity_details"]))
    if missing_details:
        raise ValueError(
            "final beats reference entity IDs without descriptions: "
            + ", ".join(missing_details)
        )
    normalized["entity_details"] = {
        entity_id: description
        for entity_id, description in normalized["entity_details"].items()
        if entity_id in referenced_ids
    }
    if len(normalized["entity_details"]) > params["max_entity_details"]:
        raise ValueError(
            f"final scene has {len(normalized['entity_details'])} entity details; "
            f"maximum is {params['max_entity_details']}"
        )


def _refinement_candidates(
    beats: list[dict[str, Any]],
    params: dict[str, Any],
) -> list[int]:
    longest_first = sorted(
        (
            (index, float(beat["end"]) - float(beat["start"]))
            for index, beat in enumerate(beats)
            if float(beat["end"]) - float(beat["start"])
            > params["max_beat_seconds"]
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    return [
        index
        for index, _ in longest_first[: params["max_refinement_windows"]]
    ]


def _relative_frame(frame: dict[str, Any], job: Path) -> dict[str, Any]:
    path = Path(frame["path"])
    try:
        relative = path.relative_to(job)
    except ValueError:
        relative = path
    return {"timestamp": round(float(frame["timestamp"]), 3), "path": str(relative)}


def _canonical(normalized: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": normalized["summary"],
        "tone": normalized["tone"],
        "entity_details": normalized["entity_details"],
        "beats": [
            {key: beat[key] for key in CANONICAL_BEAT_KEYS}
            for beat in normalized["beats"]
        ],
    }


def _evidence_sidecar(
    normalized: dict[str, Any],
    all_frames: list[dict[str, Any]],
    params: dict[str, Any],
    job: Path,
    elapsed: float,
) -> dict[str, Any]:
    return {
        "version": 1,
        "source_video": "input.mp4",
        "provider": params["provider"],
        "model": params["model"],
        "prompt_version": params["prompt_version"],
        "analysis_seconds": round(elapsed, 3),
        "sampled_frames": [_relative_frame(frame, job) for frame in all_frames],
        "beats": [
            {
                "beat_index": index,
                "start": beat["start"],
                "end": beat["end"],
                "frames": [
                    _relative_frame(frame, job)
                    for frame in beat["evidence_frames"]
                ],
            }
            for index, beat in enumerate(normalized["beats"])
        ],
    }


def understand(job: Path, cfg: dict[str, Any]) -> None:
    """Analyze ``job/input.mp4`` and write the frozen scene contract."""
    job = Path(job)
    video = job / "input.mp4"
    if not video.is_file():
        raise FileNotFoundError(f"missing scene input: {video}")
    params = resolve_params(job, cfg)
    meta = read_json(job / "meta.json", default={})
    measured_duration = media_duration(video)
    duration = _number(meta.get("duration", measured_duration), "meta.duration")
    tolerance = params["timestamp_tolerance_seconds"]
    if abs(duration - measured_duration) > tolerance:
        raise ValueError(
            f"meta.duration is {duration:.3f}s but input.mp4 is "
            f"{measured_duration:.3f}s"
        )
    duration = measured_duration

    write_json(job / PARAM_FILE, params)
    require_binary("ffmpeg")
    require_binary("ffprobe")

    started = time.time()
    log(f"  scenes: input={video.name}, duration={duration:.3f}s")
    log(f"  scenes: settings saved to {job / PARAM_FILE}")
    # Never let max_frames mean "analyze only the beginning". For long clips,
    # lower the sampling rate uniformly so evidence still spans the full video.
    base_fps = min(
        params["frame_fps"],
        params["max_frames"] / duration,
    )
    log(
        f"  scenes: extracting up to {params['max_frames']} full-timeline "
        f"frames at {base_fps:.3f} fps"
    )
    base_frames = extract_frames(
        video,
        job / FRAMES_DIR / "base",
        fps=base_fps,
        width=params["frame_width"],
        jpeg_quality=params["jpeg_quality"],
        max_frames=params["max_frames"],
        start=0.0,
        end=duration,
    )
    log(
        f"  scenes: extracted {len(base_frames)} frames; sending them to "
        f"{params['provider']} model {params['model']}"
    )
    raw = _call_openai(
        base_frames,
        params,
        0.0,
        duration,
        refinement=False,
    )
    log(f"  scenes: model returned {len(raw.get('beats', []))} base beat(s); validating")
    normalized = normalize_analysis(
        raw,
        start=0.0,
        end=duration,
        params=params,
        frames=base_frames,
    )

    all_frames = list(base_frames)
    if params["refine_long_beats"]:
        for pass_index in range(params["max_refinement_passes"]):
            candidates = _refinement_candidates(normalized["beats"], params)
            if not candidates:
                break
            log(
                f"  scenes: refinement pass {pass_index + 1} will inspect "
                f"{len(candidates)} long beat(s) above "
                f"{params['max_beat_seconds']:.3f}s"
            )
            for candidate_index in sorted(candidates, reverse=True):
                candidate = normalized["beats"][candidate_index]
                window_start = float(candidate["start"])
                window_end = float(candidate["end"])
                log(
                    f"  scenes: refining beat {candidate_index} "
                    f"({window_start:.3f}s..{window_end:.3f}s) at "
                    f"{params['refinement_frame_fps']:.3f} fps"
                )
                frame_dir = (
                    job
                    / FRAMES_DIR
                    / f"refine_{pass_index + 1}_{candidate_index:03d}"
                )
                refinement_frames = extract_frames(
                    video,
                    frame_dir,
                    fps=params["refinement_frame_fps"],
                    width=params["frame_width"],
                    jpeg_quality=params["jpeg_quality"],
                    max_frames=params["max_refinement_frames"],
                    start=window_start,
                    end=window_end,
                )
                all_frames.extend(refinement_frames)
                refined_raw = _call_openai(
                    refinement_frames,
                    params,
                    window_start,
                    window_end,
                    refinement=True,
                    known_entity_details=dict(normalized["entity_details"]),
                )
                refined = normalize_analysis(
                    refined_raw,
                    start=window_start,
                    end=window_end,
                    params=params,
                    frames=refinement_frames,
                )
                log(
                    f"  scenes: refined beat {candidate_index} into "
                    f"{len(refined['beats'])} beat(s)"
                )
                for entity_id, description in refined["entity_details"].items():
                    normalized["entity_details"].setdefault(entity_id, description)
                normalized["beats"][candidate_index : candidate_index + 1] = refined[
                    "beats"
                ]

    _reconcile_entity_details(normalized, params)

    # A long beat after targeted review can be legitimate continuous action.
    # Record it visibly rather than mechanically inventing scene boundaries.
    unresolved_long = _refinement_candidates(normalized["beats"], params)
    if unresolved_long:
        log(
            "  scenes: targeted review kept "
            f"{len(unresolved_long)} long continuous beat(s)"
        )

    write_json(job / "scenes.json", _canonical(normalized))
    write_json(
        job / EVIDENCE_FILE,
        _evidence_sidecar(
            normalized,
            all_frames,
            params,
            job,
            time.time() - started,
        ),
    )
    log(f"  scenes: validation passed; wrote {job / 'scenes.json'}")
    log(f"  scenes: evidence saved to {job / EVIDENCE_FILE}")


def _parse_override(raw: str) -> tuple[str, Any]:
    name, separator, value = raw.partition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError("--set must look like name=value")
    name = name.strip()
    if name not in DEFAULT_PARAMS:
        raise argparse.ArgumentTypeError(f"unknown scene parameter {name!r}")
    return name, _coerce_param(name, value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m drishti.scenes",
        description="Create full-timeline scenes.json from an MP4 or existing job",
    )
    parser.add_argument(
        "job",
        type=Path,
        nargs="?",
        help="existing job directory containing input.mp4",
    )
    parser.add_argument(
        "--video",
        type=Path,
        help="any source MP4; creates a fresh runs/dev job automatically",
    )
    parser.add_argument(
        "--label",
        help="optional readable name for the job created with --video",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        type=_parse_override,
        default=[],
        metavar="NAME=VALUE",
        help=f"override any scene parameter; names: {', '.join(DEFAULT_PARAMS)}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if bool(args.job) == bool(args.video):
        raise SystemExit("provide exactly one input: an existing JOB or --video VIDEO.mp4")
    if args.video:
        from .config import get_profile, new_job

        job = new_job(get_profile("dev"), args.video, args.label)
        log(f"DRISHTI scenes · new job: {job}")
    else:
        job = args.job
    understand(job, dict(args.overrides))
    log(f"DRISHTI scenes · done: {job}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
