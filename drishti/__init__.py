"""DRISHTI — audio description for blind and low-vision viewers.

Every stage is a function with the same signature:

    def stage(job: Path, cfg: dict) -> None

It reads only the files named in its docstring from the job directory, writes
only the files named in its docstring back into the job directory, and raises on
failure. No stage returns data, imports another stage, or writes status.json —
the pipeline owns that.

Run any stage on its own:

    python -m drishti.gaps runs/dev/test1/
"""

__version__ = "0.1.0"

# Fixed stage order. `transcript` is deliberately NOT a stage: in Saaras chunk
# mode the transcript falls out of gap detection, so gaps.py writes it from
# calls we already paid for. Never add a second STT pass.
STAGE_ORDER = ["validate", "gaps", "scenes", "cast", "align", "narrate", "tts_fit", "mix"]
