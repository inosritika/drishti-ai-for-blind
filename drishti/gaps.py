"""Stage: gaps — OWNER: NISHANT

Find the windows where nobody is speaking, and identify the spoken language.
This is our core Sarvam-depth claim: Saaras answers "was speech transcribed
here?", which works through continuous background music, where energy-based
silence detection and WebRTC VAD both fail.

reads:
    audio.wav, meta.json

writes:
    gaps.json       [{"start": float, "end": float, "duration": float}, ...]
    chunks.json     [{"index": int, "start": float, "end": float,
                      "has_speech": bool, "transcript": str,
                      "language_code": str|null, "language_probability": float|null}, ...]
    transcript.txt  all chunk transcripts joined, in order
    language.json   {"source_language": "hi-IN"|"en-IN"|...|"unknown",
                     "confidence": float,
                     "evidence": [{"language_code": str, "seconds": float}, ...]}

Tested behaviour to keep (see drishti_e2e.py: detect_gaps_with_saaras):
  - 1.5s non-overlapping chunks, split with the `wave` module
  - POST /speech-to-text, model saaras:v3, language_code="unknown"
  - empty transcript == no speech; merge consecutive silent chunks
  - 150 ms edge padding removed from BOTH ends of every gap
  - drop gaps shorter than min_gap after padding
  - pass cache_ns="saaras_stt" so a rerun costs nothing

Language detection (do not skip — the whole pipeline routes on this):
  - keep Saaras's language_code and language_probability for every chunk that
    HAS speech
  - pick the dominant language weighted by spoken chunk duration
  - write "unknown" — never a guess — when any of these hold:
      * no speech at all
      * fewer than 2 usable speech chunks
      * confidence < 0.70
      * no clearly dominant language
  - you decide the SOURCE language only. The pipeline decides the OUTPUT
    language. Nothing in this file mentions hi-IN as a default.

Keep energy-based silencedetect available as a secondary mode for quiet
room-noise clips (cfg["detector"] == "silence"), but Saaras is the default.
"""

from __future__ import annotations

from pathlib import Path


def detect(job: Path, cfg: dict) -> None:
    """Write gaps.json, chunks.json, transcript.txt and language.json into `job`.

    cfg keys used: detector ("saaras"|"silence", default "saaras"),
                   chunk_seconds (default 1.5), min_gap (default 1.6),
                   edge_padding (default 0.15), noise_db (default -32.0)
    """
    raise NotImplementedError("NISHANT: gaps stage")


if __name__ == "__main__":
    import sys

    detect(Path(sys.argv[1]), {})
