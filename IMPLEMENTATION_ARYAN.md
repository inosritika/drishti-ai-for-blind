# Aryan — Integration Implementation Plan

Build order is chosen so that **the first 15 minutes unblock the other three**,
and nothing you build later ever blocks them again.

---

## 0. Directory layout — dev and demo are separate worlds

```
drishti-ai-for-blind/
  drishti/                 # the pipeline package
    __init__.py
    common.py              # ARYAN — http, ffprobe, json, cache      [frozen additive-only]
    config.py              # ARYAN — profiles, job dirs, strict checks
    pipeline.py            # ARYAN — idempotent stage runner
    media.py               # NISHANT
    gaps.py                # NISHANT
    mix.py                 # NISHANT
    scenes.py              # RITIKA
    memory.py              # RITIKA        (Increment 3)
    narrate.py             # TANISHQ
    speak.py               # TANISHQ
  api/                     # ARYAN         (after the 12:30 gate)
  web/                     # ARYAN shell + TANISHQ components
  fixtures/
    jobs/hindi_sample/     # committed, always-green reference job
  runs/                    # gitignored — all working output
    dev/<job_id>/          # everyone's scratch, throwaway
    demo/<job_id>/         # curated runs that feed the stage
  demo/
    clips/                 # locked source clips
    outputs/               # WRITE-ONCE locked fallback MP4s
  .cache/                  # gitignored — global content-hashed API cache
  .env                     # gitignored
  .env.example
  Makefile
```

**`.gitignore` from minute one:** `runs/`, `.cache/`, `.env`, `__pycache__/`,
`*.wav`, `node_modules/`, `.DS_Store`. Do not let a 5 MB MP4 into git history
on a venue Wi-Fi connection.

### The two profiles

| | `dev` (default) | `demo` |
|---|---|---|
| Job root | `runs/dev/<id>/` | `runs/demo/<id>/` |
| Cache | read + write | read + write (shared with dev — a dev run warms the demo) |
| Invariant checks | warn and continue | **enforce — run fails, no output written** |
| Logging | verbose per stage | summary + `run_report.json` |
| Final output | stays in the job dir | also copied to `demo/outputs/`, **write-once** |
| Selection | automatic | requires explicit `--profile demo` |

The cache is deliberately **shared** between profiles. It's keyed by a content
hash of the request, so it can't serve a wrong answer, and it means all the dev
iteration you do makes the demo run instant.

**Strict mode is the 12:30 gate checklist, in code.** In `demo` profile the run
fails loudly rather than producing a subtly broken MP4:

- every narration WAV duration ≤ its `max_duration`
- every narration sits fully inside its gap, after edge padding
- at most one narration per `gap_index`
- output duration equals input duration within 0.05 s
- output MP4 has both a video and an audio stream
- narration passed script validation for `output_language`
- in `auto` mode, `output_language` equals `source_language`

Run it in `dev` all morning. Run `demo` before you call the gate, and again
before every recording.

---

## Step 1 — Skeleton and stubs · 15 min · **DO THIS FIRST**

Three people are idle until this lands. Nothing else you build matters as much.

Create the tree above, `.gitignore`, `.env.example`, and a `Makefile`
(`make dev` = uvicorn + vite, `make run CLIP=… ` = pipeline). Then write every
teammate's module as a stub containing the real signature, a docstring naming
exactly which files it reads and writes, `raise NotImplementedError`, and a
standalone CLI block.

Uniform signature — no negotiation needed with anyone:

```python
def prepare(job: Path, cfg: dict) -> None:
    """validate stage.
    reads:  input.mp4
    writes: meta.json {duration, has_audio, width, height, fps}, audio.wav
    """
```

| Stage | Function | Reads | Writes |
|---|---|---|---|
| `validate` | `media.prepare` | `input.mp4` | `meta.json`, `audio.wav` |
| `gaps` | `gaps.detect` | `audio.wav`, `meta.json` | `gaps.json`, `chunks.json`, `transcript.txt`, `language.json` |
| `scenes` | `scenes.understand` | `input.mp4`, `meta.json` | `scenes.json` |
| `narrate` | `narrate.write` | `gaps.json`, `scenes.json`, `transcript.txt`, `cfg["output_language"]` | `narration.json` |
| `tts_fit` | `speak.synthesize` | `narration.json` | `narration_XX.wav`, updates `narration.json` |
| `mix` | `mix.render` | `input.mp4`, `narration.json`, wavs | `output.mp4` |

Every module ends with:

```python
if __name__ == "__main__":
    import sys
    prepare(Path(sys.argv[1]), {})
```

so anyone can run `python -m drishti.gaps runs/dev/test1/` with no orchestrator,
no API, and none of anyone else's code.

**Push, then post the contract in the team chat** (copy-paste block at the
bottom of this file). Done when three people say "got it."

---

## Step 2 — `common.py` and `config.py` · 30 min

### `common.py` — lift from the reference, add caching

`run`, `require_binary`, `media_duration`, `read_json`, `write_json`,
`http_json`, `http_multipart`, and `env_key(name)` that raises a clear error
when a key is missing.

**Make caching a parameter, not a decorator** — teammates adopt it by adding one
argument instead of restructuring their code:

```python
http_json(url, payload, headers, cache_ns="sarvam_tts")
http_multipart(url, fields, "file", wav_path, headers, cache_ns="saaras_stt")
```

When `cache_ns` is set, key on `sha256(url + canonical_json(payload))` — plus
the file bytes for multipart — and store the response at
`.cache/<cache_ns>/<hash>.json`. Namespaces: `saaras_stt`, `sarvam_chat`,
`sarvam_tts`, `openai_vision`.

Keep the reference's retry behavior exactly: retry on 429 and 5xx with
exponential backoff, raise with the response body truncated on anything else.

Then **freeze it, additive-only** — new functions may be appended, existing ones
never modified or renamed. Announce that rule when you push.

### `config.py`

```python
@dataclass(frozen=True)
class Profile:
    name: str; jobs_root: Path; cache_dir: Path; strict: bool

def get_profile(name: str | None = None) -> Profile   # env DRISHTI_PROFILE, default "dev"
def new_job(profile: Profile, label: str) -> Path      # runs/<profile>/20260726_1142_clipA/
def verify_job(job: Path, cfg: dict) -> list[str]      # the invariant list; [] means clean
```

`verify_job` returns failure strings. `pipeline` warns on them in dev and aborts
on them in demo. Write it once, use it everywhere — this is also what your
transparency panel reads later.

---

## Step 3 — One Hindi fixture job · 10 min

`fixtures/jobs/hindi_sample/` pre-seeded with `meta.json`, `gaps.json`,
`language.json`, `transcript.txt`, `scenes.json`, `narration.json` — real values
copied out of the HANDOFF run report (gap `0.15–13.35`, the Devanagari narration
line, the scene beats). One language is enough for now.

No `input.mp4` in the fixture; stages that need real media are simply never
re-run because their outputs already exist. That's the whole trick.

Ask each owner to eyeball their own file for shape. They know it best, and it
stays one author per file.

---

## Step 4 — `pipeline.py` · 40 min · green by 11:00

**The one design decision: an idempotent stage runner.** Each stage declares its
outputs; the runner skips any stage whose outputs already exist.

```python
STAGES = [
    ("validate", media.prepare,      ["meta.json", "audio.wav"]),
    ("gaps",     gaps.detect,        ["gaps.json", "language.json", "transcript.txt"]),
    ("scenes",   scenes.understand,  ["scenes.json"]),
    ("narrate",  narrate.write,      ["narration.json"]),
    ("tts_fit",  speak.synthesize,   ["narration.json"]),   # see note
    ("mix",      mix.render,         ["output.mp4"]),
]
```

That single property gives you four things at once:

- **fixtures need no special code path** — a fixture job is a job dir whose
  outputs are already present
- **substitution is automatic** — delete a stage's outputs, re-run, and only
  that stage recomputes
- **resume after a pause** works for both language selection and Increment 2's
  narration approval, with no second mechanism
- **re-running is free**, which is what makes the on-stage demo instant

Add `--force <stage>` (and `--force all`) to delete a stage's outputs and redo
it. Note `tts_fit` both reads and updates `narration.json`, so give it a real
sentinel instead — skip when every entry has a `wav` key with a file on disk.

Also in this step:

1. **Write `status.json` after every stage** — `{stage, pct, stage_timings,
   api_calls, source_language, output_language, awaiting?, error?}`. The timings
   and counts cost nothing now and are exactly what the transparency panel and
   your "about two minutes" demo claim need later.
2. **Resolve the language immediately after `gaps`.** Read `language.json`; with
   `--language auto` set `output_language = source_language`; normalize to a
   Sarvam code; check it against `SUPPORTED_TTS` (Tanishq gives you the verified
   list). Unknown, mixed, or unsupported → stop with a clear message in M1, and
   later become a pause. Never fall back to `hi-IN`.
3. **Design for one pause, build zero.** Shape the state as
   `status.awaiting = {type, payload}` now so Increment 2 adds an endpoint
   rather than a mechanism. Don't implement pausing today.

Acceptance: `python -m drishti.pipeline fixtures/jobs/hindi_sample --language auto`
runs every stage green, resolves `hi-IN`, and skips everything (all outputs
present). That's your first end-to-end.

---

## Step 5 — Substitution · 11:00 → 12:15

Copy clip A into a fresh dev job. As each teammate's module lands, delete that
stage's outputs and re-run; everything upstream stays cached. Expected landing
order: `gaps` → `scenes` → `narrate`/`tts_fit` → `mix`.

Keep `fixtures/jobs/hindi_sample` untouched as your always-green reference. When
something breaks at 11:40, the question "is it my runner or their module?" is
answered in ten seconds by running the fixture.

## Step 6 — Gate · 12:15

Clean job dir, clip A, `--profile demo`. Strict mode either passes or tells you
exactly which invariant failed. You call pass or fail; on a fail the owner gets
twenty minutes, otherwise restore that stage's fixture output and move on.

---

## After the gate — not before

`api/` (upload → background thread → `status.json` polling → artifact serving),
then the React shell. The API's job endpoints are thin wrappers over
`new_job` + `pipeline.run`; resist putting logic there. Then, in order:
transparency panel, accessible-UI pass, radio mode.

**Do not build today:** a database, auth, websockets or SSE, Docker, deployment,
or a queue. Filesystem plus a background thread is correct at this scale.

---

## Paste this into the team chat

> **Contracts are frozen — go.** Your module is stubbed with its real signature
> in `drishti/<yours>.py`. Read and write **only** the files named in your
> docstring, inside the job dir you're handed.
>
> Run yours alone: `python -m drishti.gaps runs/dev/test1/`
>
> Stage order is now `validate → gaps → scenes → narrate → tts_fit → mix`.
> **`transcript` is no longer a stage** — Nishant writes `transcript.txt` inside
> `gaps.py` from the Saaras chunks we already pay for. Do not add a second STT call.
>
> Use `common.py` for HTTP and ffprobe, and pass `cache_ns=` on every API call so
> reruns are free. `common.py` is frozen: you may append new functions, never
> change existing ones.
>
> Work in `runs/dev/…`. `runs/demo/…` is for curated runs and enforces the full
> checklist — use `--profile demo` before any recording.
>
> Language: Nishant writes `language.json` (evidence). I resolve one
> `output_language` and pass it to narrate and speak. Nobody else decides
> language, and nothing anywhere hardcodes Hindi.
>
> **Tanishq — I need the verified list of Bulbul v3 languages with a working
> speaker for each, as early as you can. My resolver checks against it.**
