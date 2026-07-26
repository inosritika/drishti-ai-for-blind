"""Fetch and cut the public-domain test clips. See clips/README.md."""
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "clips" / "source"
SRC.mkdir(parents=True, exist_ok=True)


class Source:
    """One public-domain download and the 29s excerpts we cut out of it.

    Saaras REST rejects anything over 29.5s, so every excerpt is 29s. The start
    offsets are not guesses — they came out of a Saaras sweep over candidate
    windows, recorded in clips/README.md.
    """

    def __init__(self, local, item, remote, size_mb, excerpts):
        self.path = SRC / local
        self.url = f"https://archive.org/download/{item}/{urllib.parse.quote(remote)}"
        self.size_mb = size_mb
        self.excerpts = excerpts


SOURCES = [
    # The Immigrant (1917), Mutual short, out of US copyright.
    Source("the_immigrant_1917.mp4",
           "charliechaplintheimmigrant1917hd_201908",
           "Charlie Chaplin-The Immigrant (1917) HD.mp4", 206,
           [("chaplin_restaurant.mp4", 1146), ("chaplin_ship.mp4", 62)]),
    # Chalti Ka Naam Gaadi (1958). Indian films get 60 years, so anything
    # published up to 1965 is public domain in India from 1 Jan 2026. The DVD
    # is split into seven chunks; this is the third, ~27:30–55:00 of the film.
    Source("chalti_1958_part2.mp4", "chalti-ka-naam-ghadi", "VTS_01_2.mp4", 163,
           [("chalti_brawl.mp4", 1189), ("chalti_courtyard.mp4", 145)]),
]


def main() -> None:
    for source in SOURCES:
        if not source.path.is_file():
            print(f"downloading {source.path.name} (~{source.size_mb} MB)…")
            # archive.org 302s to a datanode; urlretrieve follows redirects.
            urllib.request.urlretrieve(source.url, source.path)
        print(f"have {source.path.name} ({source.path.stat().st_size / 1e6:.1f} MB)")

        for name, start in source.excerpts:
            out = ROOT / "clips" / name
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-y",
                 "-ss", str(start), "-t", "29", "-i", str(source.path),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                 "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100",
                 "-movflags", "+faststart", str(out)],
                check=True,
            )
            print(f"cut {name} from {start}s")


if __name__ == "__main__":
    sys.exit(main())
