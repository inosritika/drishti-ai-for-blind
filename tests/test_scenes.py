from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drishti.scenes import (
    CANONICAL_BEAT_KEYS,
    DEFAULT_PARAMS,
    _analysis_schema,
    _canonical,
    _prompt,
    _request_content,
    understand,
    normalize_analysis,
    resolve_params,
)


class SceneContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.params = dict(DEFAULT_PARAMS)
        self.frames = [
            {"timestamp": 0.0, "path": Path("/tmp/frame_0001.jpg")},
            {"timestamp": 1.0, "path": Path("/tmp/frame_0002.jpg")},
            {"timestamp": 2.0, "path": Path("/tmp/frame_0003.jpg")},
        ]
        self.raw = {
            "summary": "A person walks through a room.",
            "tone": "quiet and tense",
            "entity_details": [
                {
                    "id": "woman1",
                    "description": "Dark-haired woman wearing a white dress with wide eyes.",
                }
            ],
            "beats": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "event": "A person walks across a room.",
                    "entities": ["woman1", "room"],
                    "intensity": 2,
                    "confidence": 0.9,
                    "uncertain_details": [],
                    "evidence_frame_times": [0.0, 1.0, 2.0],
                }
            ],
        }

    def test_canonical_output_has_aryans_exact_beat_keys(self) -> None:
        normalized = normalize_analysis(
            self.raw,
            start=0.0,
            end=2.0,
            params=self.params,
            frames=self.frames,
        )
        result = _canonical(normalized)
        self.assertEqual(
            tuple(result),
            ("summary", "tone", "entity_details", "beats"),
        )
        self.assertEqual(result["tone"], "quiet and tense")
        self.assertEqual(
            result["entity_details"],
            {
                "woman1": "Dark-haired woman wearing a white dress with wide eyes."
            },
        )
        self.assertEqual(tuple(result["beats"][0]), CANONICAL_BEAT_KEYS)
        self.assertNotIn("evidence_frame_times", result["beats"][0])
        self.assertNotIn("evidence_frames", result["beats"][0])

    def test_confidence_is_constrained_to_zero_one(self) -> None:
        self.raw["beats"][0]["confidence"] = 1.2
        with self.assertRaisesRegex(ValueError, "confidence"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_tone_is_kept_short(self) -> None:
        self.raw["tone"] = "dark tense suspenseful frightening mysterious"
        with self.assertRaisesRegex(ValueError, "tone has 5 words"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_entity_ids_are_unique(self) -> None:
        self.raw["entity_details"].append(
            {"id": "woman1", "description": "A second description."}
        )
        with self.assertRaisesRegex(ValueError, "duplicate entity id"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_entity_id_must_be_stable_numbered_identifier(self) -> None:
        self.raw["entity_details"][0]["id"] = "Woman 1"
        with self.assertRaisesRegex(ValueError, "end in a number"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_entity_description_respects_configured_word_limit(self) -> None:
        self.params["entity_description_max_words"] = 4
        with self.assertRaisesRegex(ValueError, "description has 9 words"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_beat_entity_id_requires_matching_description(self) -> None:
        self.raw["beats"][0]["entities"].append("shadow1")
        with self.assertRaisesRegex(ValueError, "no entity_details row: shadow1"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_entity_description_must_be_used_by_a_beat(self) -> None:
        self.raw["entity_details"].append(
            {"id": "shadow1", "description": "Tall dark human-like silhouette."}
        )
        with self.assertRaisesRegex(ValueError, "not referenced.*shadow1"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_entity_count_respects_configured_limit(self) -> None:
        self.params["max_entity_details"] = 1
        self.raw["entity_details"].append(
            {"id": "shadow1", "description": "Tall dark human-like silhouette."}
        )
        self.raw["beats"][0]["entities"].append("shadow1")
        with self.assertRaisesRegex(ValueError, "2 entity details; maximum is 1"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_openai_schema_uses_strict_rows_for_dynamic_entity_map(self) -> None:
        schema = _analysis_schema(self.params, 0.0, 2.0)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["required"],
            ["summary", "tone", "entity_details", "beats"],
        )
        entity_schema = schema["properties"]["entity_details"]
        self.assertEqual(entity_schema["type"], "array")
        self.assertEqual(entity_schema["maxItems"], self.params["max_entity_details"])
        self.assertFalse(entity_schema["items"]["additionalProperties"])
        self.assertEqual(
            entity_schema["items"]["required"],
            ["id", "description"],
        )

    def test_refinement_prompt_receives_known_entity_descriptions(self) -> None:
        prompt = _prompt(
            self.params,
            0.0,
            2.0,
            refinement=True,
            known_entity_details={"woman1": "Dark-haired woman in a white dress."},
        )
        self.assertIn('"woman1": "Dark-haired woman in a white dress."', prompt)
        self.assertIn("including reused known IDs", prompt)
        self.assertIn("absent from the supplied frames", prompt)
        self.assertIn("machine references for the entities lists only", prompt)
        self.assertIn("never write", prompt)

    def test_timestamp_must_match_video_window(self) -> None:
        self.raw["beats"][0]["end"] = 2.5
        with self.assertRaisesRegex(ValueError, "outside"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_evidence_must_reference_a_sampled_frame(self) -> None:
        self.raw["beats"][0]["evidence_frame_times"] = [0.5]
        with self.assertRaisesRegex(ValueError, "not a sampled frame"):
            normalize_analysis(
                self.raw,
                start=0.0,
                end=2.0,
                params=self.params,
                frames=self.frames,
            )

    def test_direct_config_overrides_job_parameter_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            (job / "scenes-param.json").write_text(
                json.dumps({"frame_fps": 2.5}), encoding="utf-8"
            )
            params = resolve_params(job, {"frame_fps": 1.0})
        self.assertEqual(params["frame_fps"], 1.0)

    def test_frame_label_is_immediately_before_its_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"jpeg")
            content = _request_content(
                [{"timestamp": 1.25, "path": frame}],
                "prompt",
                "low",
            )
        self.assertEqual(content[1]["text"], "Frame 1, t=1.250s")
        self.assertEqual(content[2]["type"], "input_image")

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg required",
    )
    def test_offline_stage_writes_canonical_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            video = job / "input.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=320x180:d=2",
                    "-pix_fmt",
                    "yuv420p",
                    str(video),
                ],
                check=True,
            )

            def fake_analysis(frames, params, start, end, *, refinement):
                return {
                    "summary": "A blue frame remains on screen.",
                    "tone": "calm",
                    "entity_details": [],
                    "beats": [
                        {
                            "start": start,
                            "end": end,
                            "event": "A blue frame fills the screen.",
                            "entities": ["blue frame"],
                            "intensity": 1,
                            "confidence": 0.99,
                            "uncertain_details": [],
                            "evidence_frame_times": [frames[0]["timestamp"]],
                        }
                    ],
                }

            with patch("drishti.scenes._call_openai", side_effect=fake_analysis):
                understand(job, {"frame_fps": 1.0, "max_frames": 4})

            scenes = json.loads((job / "scenes.json").read_text(encoding="utf-8"))
            self.assertEqual(
                tuple(scenes),
                ("summary", "tone", "entity_details", "beats"),
            )
            self.assertEqual(scenes["tone"], "calm")
            self.assertEqual(scenes["entity_details"], {})
            self.assertEqual(tuple(scenes["beats"][0]), CANONICAL_BEAT_KEYS)
            self.assertTrue((job / "scenes-param.json").is_file())
            self.assertTrue((job / "scenes-evidence.json").is_file())

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg required",
    )
    def test_refinement_reuses_and_merges_stable_entity_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            video = job / "input.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x180:d=8",
                    "-pix_fmt",
                    "yuv420p",
                    str(video),
                ],
                check=True,
            )
            known_values: list[dict[str, str] | None] = []

            def fake_analysis(
                frames,
                params,
                start,
                end,
                *,
                refinement,
                known_entity_details=None,
            ):
                known_values.append(known_entity_details)
                if not refinement:
                    return {
                        "summary": "A woman faces a dark doorway.",
                        "tone": "tense",
                        "entity_details": [
                            {
                                "id": "woman1",
                                "description": "Dark-haired woman wearing a white dress.",
                            }
                        ],
                        "beats": [
                            {
                                "start": start,
                                "end": end,
                                "event": "A woman faces a dark doorway.",
                                "entities": ["woman1", "doorway"],
                                "intensity": 2,
                                "confidence": 0.9,
                                "uncertain_details": [],
                                "evidence_frame_times": [frames[0]["timestamp"]],
                            }
                        ],
                    }

                midpoint = (start + end) / 2
                midpoint_frame = min(
                    frames,
                    key=lambda frame: abs(frame["timestamp"] - midpoint),
                )
                return {
                    "summary": "A woman faces a doorway as a shadow appears.",
                    "tone": "tense",
                    "entity_details": [
                        {
                            "id": "woman1",
                            "description": "Woman in a white dress seen from behind.",
                        },
                        {
                            "id": "shadow1",
                            "description": "Tall dark human-like silhouette.",
                        },
                    ],
                    "beats": [
                        {
                            "start": start,
                            "end": midpoint,
                            "event": "A woman faces a dark doorway.",
                            "entities": ["woman1", "doorway"],
                            "intensity": 2,
                            "confidence": 0.9,
                            "uncertain_details": [],
                            "evidence_frame_times": [frames[0]["timestamp"]],
                        },
                        {
                            "start": midpoint,
                            "end": end,
                            "event": "A tall shadow appears in the doorway.",
                            "entities": ["woman1", "shadow1", "doorway"],
                            "intensity": 4,
                            "confidence": 0.88,
                            "uncertain_details": [],
                            "evidence_frame_times": [midpoint_frame["timestamp"]],
                        },
                    ],
                }

            with patch("drishti.scenes._call_openai", side_effect=fake_analysis):
                understand(
                    job,
                    {
                        "frame_fps": 1.0,
                        "max_frames": 8,
                        "max_beat_seconds": 3.0,
                        "refinement_frame_fps": 2.0,
                        "max_refinement_frames": 20,
                    },
                )

            scenes = json.loads((job / "scenes.json").read_text(encoding="utf-8"))
            self.assertIsNone(known_values[0])
            self.assertEqual(
                known_values[1],
                {"woman1": "Dark-haired woman wearing a white dress."},
            )
            self.assertEqual(
                scenes["entity_details"],
                {
                    "woman1": "Dark-haired woman wearing a white dress.",
                    "shadow1": "Tall dark human-like silhouette.",
                },
            )
            self.assertEqual(
                scenes["beats"][1]["entities"],
                ["woman1", "shadow1", "doorway"],
            )


if __name__ == "__main__":
    unittest.main()
