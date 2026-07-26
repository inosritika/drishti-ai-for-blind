"""Stage: validate — OWNER: NISHANT

Probe the clip and produce the mono WAV every downstream audio stage uses.

reads:
    input.mp4

writes:
    meta.json   {"duration": float, "has_audio": bool, "width": int,
                 "height": int, "fps": float}
    audio.wav   mono, 16 kHz, pcm_s16le

Tested behaviour to keep (see drishti_e2e.py: extract_audio, media_duration,
has_audio_stream):
  - mono / 16000 Hz / pcm_s16le, because that is what Saaras chunking expects
  - optional denoise `highpass=f=80,afftdn=nf=-25` for steady room hiss ONLY.
    It does not remove background music and must never be used to fight music.
  - if the source has no audio stream, synthesise a silent track of the same
    duration (anullsrc) so downstream stages have something to read
  - Saaras REST rejects clips over ~29.5s: raise a clear error above that
"""

from __future__ import annotations

from pathlib import Path


def prepare(job: Path, cfg: dict) -> None:
    """Write meta.json and audio.wav into `job`.

    cfg keys used: denoise (bool, default True)
    """
    raise NotImplementedError("NISHANT: validate stage")


if __name__ == "__main__":
    import sys

    prepare(Path(sys.argv[1]), {})
