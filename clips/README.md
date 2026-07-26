# Test clips

Media is gitignored — this file records where each clip came from and how to
rebuild it. `source/` holds the full downloads, the top level holds the 29s
excerpts we actually run.

**Everything must be ≤29.5s.** Saaras REST rejects longer clips and `validate`
stops the run before it wastes an API call.

## What we test on

| Clip | Source | Language | Speech chunks | Gaps |
|---|---|---|---|---|
| `chaplin_restaurant.mp4` | *The Immigrant* (1917) @19:06 | none — pass `--language` | 0 / 20 | one, 28.70s |
| `chaplin_ship.mp4` | *The Immigrant* (1917) @1:02 | none — pass `--language` | — | — |
| `chalti_brawl.mp4` | *Chalti Ka Naam Gaadi* (1958) @44:49 | **hi-IN, conf 1.00** | 12 / 20 | 8.70s, 2.70s |
| `chalti_courtyard.mp4` | *Chalti Ka Naam Gaadi* (1958) @29:45 | **hi-IN, conf 1.00** | 6 / 20 | 10.20s + one more, 18.9s total |
| `bean_drive.mp4` | Mr Bean — "Drive Like a Boss" | en-IN | 2 / 20 | 17.70s, 7.70s |
| `bean_coffee.mp4` | Mr Bean — "Coffee" | en-IN | 8 / 20 | 4 gaps, 2.7–7.2s |

Four regimes on purpose. Chaplin is the capability proof: twenty chunks of
continuous piano and Saaras reports speech in **none** of them, where an
energy threshold or WebRTC VAD would call the whole clip speech. `bean_coffee`
is the realistic case — narration has to weave between actual dialogue.
`chalti_brawl` is both at once and in Hindi: a continuous 1958 orchestral score
*plus* real dialogue, where Saaras separates the two and the pipeline routes
the whole chain — narration written by Sarvam-30B in Devanagari, spoken by
Bulbul in hi-IN — without anyone passing a language flag.

## The Hindi clip is the one that proves the routing

Until this clip existed, every clip we had was English or silent, so
`--language auto` had never once been exercised against a non-English source.
It works:

```
speech in 12/20 chunks · 2 usable gap(s)
language: hi-IN (confidence 1.00)
narrate: 2 segment(s) in hi-IN
  gap 0 (66/114 chars): तीन पुरुष द्वार के पास एक दूसरे को पकड़कर…
tts_fit: 2 line(s) in hi-IN, speaker anand
```

## Provenance

*The Immigrant* (1917) is a Mutual short, published in the US in 1917 and long
out of copyright. Downloaded from the Internet Archive, item
`charliechaplintheimmigrant1917hd_201908`. Chaplin's later features are **not**
public domain — Roy Export holds those — so stay on the 1914–1918 shorts.

*Chalti Ka Naam Gaadi* (1958) is public domain in India. The Copyright Act
gives a cinematograph film 60 years from the beginning of the year following
publication, so everything published up to 1965 fell out of copyright by
1 January 2026. Downloaded from the Internet Archive, item
`chalti-ka-naam-ghadi`, file `VTS_01_2.mp4` — the DVD is split into seven
chunks and this is the third, covering roughly 27:30–55:00 of the film. The
transfer carries a small distributor bug in the top-left corner; it is faint at
640×480 and we have left the framing alone rather than crop the shot.

Same rule as Chaplin: stay on the pre-1966 films. *Sholay*, *Guide* and
anything later is still owned by somebody.

The Mr Bean clips are Aryan's own downloads (Tiger Aspect / Rowan Atkinson),
kept local and gitignored.

**`whatsapp_clip.mp4` must not be demoed.** It is a ripped commercial
Hollywood feature with a burned-in piracy watermark, and it is a bad test clip
besides — talking heads in a boardroom, nothing in the gaps worth describing.
Keep it out of the submission.

## Rebuild

```bash
python3 scripts/fetch_clips.py
```

Downloads the Chaplin and Chalti sources and cuts the excerpts. The Mr Bean
clips are not fetched — copy them in by hand.

## How to pick a window out of a feature film

Do not eyeball it. The first window we cut from *Chalti* was the film's most
famous shot — Kishore Kumar at the garage with the bonnet up — and it was
useless: **19 of 20 chunks were speech and there were zero gaps.** A beautiful
scene with nowhere to put narration is not a demo.

`silencedetect` cannot help you here either. A 1958 film has an orchestral
score running under every frame, so energy-based detection reports **1.2s** as
the longest silence in 27 minutes and no window ever passes. That is the same
blind spot the Chaplin clip was chosen to expose, and it means the only thing
that can find a gap is Saaras.

So sweep candidate windows through the real detector — 20 STT calls and about
three seconds each, and every call is cached, so a rerun is free:

```python
media.prepare(job, cfg)          # cut a 29s window into job/input.mp4 first
gaps.detect(job, cfg)
```

What the sweep found across nine windows of *Chalti* part 2:

| start | speech | gaps | longest | narratable | language |
|---:|---:|---:|---:|---:|---|
| 145 | 6/20 | 2 | 10.2s | 18.9s | hi-IN 1.00 |
| 1189 | 12/20 | 2 | 8.7s | 11.4s | hi-IN 1.00 |
| 435 | 14/20 | 1 | 5.7s | 5.7s | hi-IN 1.00 |
| 1508 | 4/20 | 2 | 20.7s | 23.4s | **unknown** |
| 1595 | 20/20 | 0 | — | — | hi-IN 1.00 |

Note the trade-off in that table, because it is not obvious: the windows with
the *most* narration room are the ones with too little speech left for Saaras
to identify a language, and they come back `unknown`. A clip that shows off the
routing needs speech in roughly **half** its chunks — enough to be sure of the
language, sparse enough to leave somewhere to talk. `t=1189` sits exactly
there, which is why it is the clip we demo.

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
