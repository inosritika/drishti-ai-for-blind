# Test clips

Media is gitignored — this file records where each clip came from and how to
rebuild it. `source/` holds the full downloads, the top level holds the 29s
excerpts we actually run.

**Everything must be ≤29.5s.** Saaras REST rejects longer clips and `validate`
stops the run before it wastes an API call.

## What we test on

| Clip | Source | Speech chunks | Gaps |
|---|---|---|---|
| `chaplin_restaurant.mp4` | *The Immigrant* (1917) @19:06 | 0 / 20 | one, 28.70s |
| `chaplin_ship.mp4` | *The Immigrant* (1917) @1:02 | — | — |
| `bean_drive.mp4` | Mr Bean — "Drive Like a Boss" | 2 / 20 | 17.70s, 7.70s |
| `bean_coffee.mp4` | Mr Bean — "Coffee" | 8 / 20 | 4 gaps, 2.7–7.2s |

Three regimes on purpose. Chaplin is the capability proof: twenty chunks of
continuous piano and Saaras reports speech in **none** of them, where an
energy threshold or WebRTC VAD would call the whole clip speech. `bean_coffee`
is the realistic case — narration has to weave between actual dialogue.

## Provenance

*The Immigrant* (1917) is a Mutual short, published in the US in 1917 and long
out of copyright. Downloaded from the Internet Archive, item
`charliechaplintheimmigrant1917hd_201908`. Chaplin's later features are **not**
public domain — Roy Export holds those — so stay on the 1914–1918 shorts.

The Mr Bean clips are Aryan's own downloads (Tiger Aspect / Rowan Atkinson),
kept local and gitignored.

## Rebuild

```bash
python3 scripts/fetch_clips.py
```

Downloads the Chaplin source and cuts the excerpts. The Mr Bean clips are not
fetched — copy them in by hand.

## Silent film needs an explicit language

A clip with no speech gives Saaras nothing to identify, so language resolution
correctly refuses to guess and stops the run:

```
language: unknown (confidence 0.00) — not enough speech to identify a language
```

That is the rule working, not a bug. Pass the language yourself:

```bash
python3 -m drishti.pipeline --clip clips/chaplin_restaurant.mp4 --language en-IN
```

## One gotcha for `validate`

Many silent-film transfers carry **no audio stream at all** — the Internet
Archive copy of *Easy Street* (1917) is one. `ffprobe` shows video only, so
there is nothing for `validate` to extract and nothing for Saaras to hear. The
copy of *The Immigrant* we use does have its piano score, so this does not
block us, but `media.py` should say so plainly rather than fail obscurely.
