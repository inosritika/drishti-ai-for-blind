"""Fetch and cut the public-domain Chaplin test clips. See clips/README.md."""
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "clips" / "source"
SRC.mkdir(parents=True, exist_ok=True)

ITEM = "charliechaplintheimmigrant1917hd_201908"
REMOTE = "Charlie Chaplin-The Immigrant (1917) HD.mp4"
FULL = SRC / "the_immigrant_1917.mp4"

# Saaras REST rejects anything over 29.5s, so every excerpt is 29s.
EXCERPTS = [("chaplin_restaurant.mp4", 1146), ("chaplin_ship.mp4", 62)]


def main() -> None:
    if not FULL.is_file():
        url = f"https://archive.org/download/{ITEM}/{urllib.parse.quote(REMOTE)}"
        print(f"downloading {FULL.name} (~206 MB)…")
        urllib.request.urlretrieve(url, FULL)
    print(f"have {FULL.name} ({FULL.stat().st_size / 1e6:.1f} MB)")

    for name, start in EXCERPTS:
        out = ROOT / "clips" / name
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y",
             "-ss", str(start), "-t", "29", "-i", str(FULL),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
             "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100",
             "-movflags", "+faststart", str(out)],
            check=True,
        )
        print(f"cut {name} from {start}s")


if __name__ == "__main__":
    sys.exit(main())
