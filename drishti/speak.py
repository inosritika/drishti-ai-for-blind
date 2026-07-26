"""Stage: tts_fit — OWNER: TANISHQ

Synthesise each narration line with Bulbul and make it FIT its gap. Never trust
a promised duration: a line requested for "at most four seconds" rendered at
6.23s. Synthesise, measure, then adapt.

reads:
    narration.json

writes:
    narration_XX.wav   one per fitted segment
    narration.json     updated in place — each entry gains
                       "wav": str, "wav_duration": float, "pace": float,
                       and "skipped": true if it could not be made to fit

Tested behaviour to keep (see drishti_e2e.py: sarvam_tts, fit_tts_segments):
  - POST /text-to-speech, model bulbul:v3, base pace 1.05,
    speech_sample_rate 24000, output_audio_codec wav, temperature 0.35
  - response audio is base64 in audios[0]
  - measure the written WAV with ffprobe — never estimate
  - fit loop, in order:
        1. re-pace: pace = min(1.5, pace * actual / max_duration * 1.04)
        2. shorten once via sarvam-30b to
           floor(len(text) * max_duration / actual * 0.82) characters
        3. accept if actual <= max_duration + 0.08
        4. otherwise SKIP the segment and mark it — a segment that overruns
           its gap talks over dialogue, which is the one thing we never do
  - pass cache_ns="sarvam_tts"

LANGUAGE:
  - use the same cfg["output_language"] the narrate stage used, with a speaker
    that actually supports it. Bulbul supports fewer languages than sarvam-30b
    text generation.
  - EARLY TASK: verify which languages bulbul:v3 supports and which speaker
    works for each, then give Aryan that list — the pipeline's resolver checks
    against it before it commits to a language.
"""

from __future__ import annotations

from pathlib import Path


def synthesize(job: Path, cfg: dict) -> None:
    """Write narration_XX.wav files and update narration.json in `job`.

    cfg keys used: output_language (REQUIRED), speaker (default "anand"),
                   pace (default 1.05)
    """
    raise NotImplementedError("TANISHQ: tts_fit stage")


if __name__ == "__main__":
    import sys

    synthesize(Path(sys.argv[1]), {"output_language": "hi-IN", "speaker": "anand"})
