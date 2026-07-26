# DRISHTI

Audio description for blind and low-vision viewers. A short video goes in; a
video with spoken description in the language actually spoken in the clip comes
out — narration placed only where nobody is talking, with the original
soundtrack ducked underneath.

Sarvam Epoch Buildathon. Plan: [PLAN.md](PLAN.md) · board: `plan.html`

## Start here

```bash
cp .env.example .env      # then paste the keys in
make doctor               # checks ffmpeg, python, keys
```

## How the code is organised

One owner per file. Nobody edits anyone else's — that is the whole merge
strategy. Stages talk to each other **only** through JSON files in a job
directory.

| File | Owner | Stage |
|---|---|---|
| `drishti/media.py` | Nishant | `validate` — probe + mono WAV |
| `drishti/gaps.py` | Nishant | `gaps` — Saaras dialogue-free windows + language ID |
| `drishti/mix.py` | Nishant | `mix` — ducking + mux |
| `drishti/scenes.py` | Ritika | `scenes` — visual beats |
| `drishti/memory.py` | Ritika | entity registry (Increment 3) |
| `drishti/narrate.py` | Tanishq | `narrate` — one line per segment |
| `drishti/speak.py` | Tanishq | `tts_fit` — Bulbul + fit loop |
| `drishti/align.py` | Aryan | `align` — match beats to windows |
| `drishti/common.py` · `config.py` · `pipeline.py` | Aryan | helpers, profiles, runner |

Stage order: `validate → gaps → scenes → align → narrate → tts_fit → mix`

`align` is the join between the two halves of the pipeline: Nishant says where
it is quiet, Ritika says what is visible, and align decides which beats are
available in which window and how many characters fit there. It does **not**
decide what is worth saying — every beat it finds goes to `narrate` intact.

`transcript` is **not** a stage — `gaps.py` writes `transcript.txt` from the
Saaras chunks we already pay for. Never add a second STT call.

## Run just your own stage

```bash
python3 -m drishti.gaps runs/dev/test1/
```

No orchestrator, no API, none of anyone else's code.

## Job directory — the only shared surface

```
runs/dev/<job_id>/
  input.mp4
  meta.json         Nishant
  audio.wav         Nishant
  gaps.json         Nishant
  chunks.json       Nishant
  transcript.txt    Nishant
  language.json     Nishant
  scenes.json       Ritika
  segments.json     Aryan
  narration.json    Tanishq
  narration_XX.wav  Tanishq
  output.mp4        Nishant
  status.json       Aryan (pipeline only — no stage writes this)
```

## Script from a PDF (side pathway, not a stage)

`drishti/script_doc.py` sends a screenplay PDF to Sarvam Document Intelligence
and writes the written script out as text. It is deliberately **not** in
`STAGE_ORDER`, no stage imports it, and it never writes into a job directory —
so it cannot affect a run.

```bash
make script PDF=path/to/screenplay.pdf     # -> runs/scripts/<name>/script.md
```

Output lands in `runs/scripts/<name>/`: `script.md` (the script), `pages.json`
(the API's structured page data), `script.json` (manifest) and `raw.zip`. In the
UI it is the “Have the script? Add it too” card — `POST /api/script`, then poll
`GET /api/script/<id>`. Nothing consumes the text yet; it just lands on disk.

## dev vs demo

```bash
make run  CLIP=demo/clips/clip_a.mp4     # runs/dev/…  — permissive, verbose
make demo CLIP=demo/clips/clip_a.mp4     # runs/demo/… — STRICT
```

`demo` enforces the gate checklist in code and refuses to write output if
anything fails: narration inside its gap, one per gap, WAV within its window,
source duration preserved, both streams present, script validated, and output
language matching the detected source language in auto mode.

The API response cache is shared between profiles and keyed by a content hash,
so all your dev iteration makes the demo run instant.

`demo/outputs/` is write-once. Locked fallback videos live there and are never
overwritten.

## Language

The clip's own language wins. Hindi clip, Hindi description; English clip,
English description. `gaps.py` reports the detected source language as evidence;
the pipeline resolves exactly one output language and passes it to narration and
TTS. Nothing anywhere defaults to Hindi — unknown or unsupported input stops and
asks.
