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
    _canonical,
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
            "beats": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "event": "A person walks across a room.",
                    "entities": ["person", "room"],
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
            self.assertEqual(tuple(scenes["beats"][0]), CANONICAL_BEAT_KEYS)
            self.assertTrue((job / "scenes-param.json").is_file())
            self.assertTrue((job / "scenes-evidence.json").is_file())


if __name__ == "__main__":
    unittest.main()
