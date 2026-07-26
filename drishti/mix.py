"""Stage: mix — OWNER: NISHANT

Duck the original soundtrack only while narration plays, and mux the result.

reads:
    input.mp4, narration.json, narration_XX.wav

writes:
    output.mp4

Tested filter chain to keep exactly (see drishti_e2e.py: mux):
    [i:a]adelay=<start_ms>:all=1[nN]        one per narration segment
    [n1][n2]...amix=inputs=N:duration=longest:normalize=0[narr_raw]
    [narr_raw]apad=whole_dur=<src>,atrim=0:<src>[narr]     <-- the truncation fix
    [narr]asplit=2[narr_sc][narr_mix]
    [0:a][narr_sc]sidechaincompress=threshold=0.015:ratio=8:attack=10:release=250[ducked]
    [ducked][narr_mix]amix=inputs=2:duration=first:normalize=0[mix]

  - video is stream-copied (-c:v copy); audio aac 192k; -movflags +faststart
  - apad + atrim are not optional: without them the output is truncated. A
    12-second synthetic mux smoke test caught this the first time.
  - if the source has no audio stream, build a silent one first
  - refuse to write output.mp4 if there are zero fitted segments — an
    unchanged copy is not a result
  - output duration must equal input duration within 0.05s

Smoke-test on a synthetic tone before touching real narration.
"""

from __future__ import annotations

from pathlib import Path


def render(job: Path, cfg: dict) -> None:
    """Write output.mp4 into `job`."""
    raise NotImplementedError("NISHANT: mix stage")


if __name__ == "__main__":
    import sys

    render(Path(sys.argv[1]), {})
