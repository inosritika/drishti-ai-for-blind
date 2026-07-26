# NISHANT — Listening vertical (working notes)

Stages I own: `validate` (`media.py`) · `gaps` (`gaps.py`) · `mix` (`mix.py`).
Ownership, job-dir layout and file contracts live in `README.md` /
`IMPLEMENTATION_ARYAN.md` — not repeated here.

---

## Decisions I've made inside my own files

**Tuning knobs: `cfg` → env → default.** The stubs already declare the cfg keys
(`denoise`, `detector`, `chunk_seconds`, `min_gap`, `edge_padding`, `noise_db`).
I additionally read env vars so a value can be swept without anyone editing
`pipeline.py`, and I parse flags in my own `__main__` block for standalone runs.
Aryan changes nothing.

| Knob | cfg key | env | default |
|---|---|---|---|
| denoise on/off | `denoise` | `DRISHTI_DENOISE` | true |
| highpass Hz | `highpass_hz` | `DRISHTI_HIGHPASS_HZ` | 80 |
| afftdn nf | `afftdn_nf` | `DRISHTI_AFFTDN_NF` | -25 |
| raw filter chain | `audio_filter` | `DRISHTI_AUDIO_FILTER` | built from above |
| chunk length | `chunk_seconds` | `DRISHTI_CHUNK_S` | 1.5 |
| edge padding | `edge_padding` | `DRISHTI_GAP_PAD_MS` | 0.15 |
| min gap | `min_gap` | `DRISHTI_MIN_GAP_S` | 1.6 |
| STT concurrency | `stt_concurrency` | `DRISHTI_STT_CONCURRENCY` | TBD |
| duck threshold | `duck_threshold` | `DRISHTI_DUCK_THRESHOLD` | 0.015 |
| duck ratio | `duck_ratio` | `DRISHTI_DUCK_RATIO` | 8 |

Resolved values get echoed to stdout on every run so any result is reproducible.

**Sweep range gotcha:** ffmpeg accepts `afftdn nf` only between **-80 and -20**.
The default -25 is already near the gentle end — there is very little room to
denoise *less* without turning it off entirely (`--no-denoise`).

**Already solved by Aryan — do not rebuild:** response caching and stale-cache
safety (`cache_ns` is content-hashed in `common.py`), stage skip/`--force`
(idempotent runner), and the gate invariant checks (`config.verify_job`).

---

## Rules read off the fixture (`fixtures/jobs/english_sample`)

- **Edge padding is uniform**: gap `0.15 → 13.35` from silent chunks `0.0 → 13.5`.
  0.15s comes off both ends, including at t=0 where nothing precedes. No
  special-casing the clip boundary.
- **`confidence` = duration-weighted mean `language_probability` over the
  dominant language's speech chunks** — the fixture's 0.91 is exactly the mean of
  its 0.89 and 0.93. It is NOT a share of spoken time.
- **`evidence[].seconds`** = total spoken seconds attributed to that language.
- **`chunks.json.language_code` is already normalised** (`en-IN`). Saaras may
  return `en`; run it through `config.normalize_language`, keep raw only if that
  returns None.
- **`transcript.txt`** = non-empty chunk transcripts joined with a single space,
  one line, no timestamps.
- **`meta.fps` is a float** (`23.976`), not an ffprobe fraction.
- **`narration.start == gap.start`**, `max_duration = gap.duration − 0.25`.
  So `mix`'s `adelay` is exactly `start × 1000` ms — no extra offset.

## Resolved without needing anyone

- `gap_index` is the positional index into `gaps.json` (`config.py:198` indexes
  it directly) → I guarantee sorted-by-start and stable across reruns.
- `meta.duration` must be `common.media_duration(input.mp4)` — the same function
  `verify_job` uses on the output, or the duration invariant compares two clocks.
- Concurrent Saaras calls are safe: `_cache_write` is temp-file + atomic
  `replace()`, and requests share no state.
- Dropped my request for a 6th `meta.json` key. The WAV-vs-container duration
  drift check is worth doing, but as a stdout warning inside `media.py` — it is
  evidence, not a contract change.
- No preview tooling for M1: `gaps.json` + a video player IS the manual check.
  Revisit a gap-only WAV when clip B produces many scattered gaps.

---

## Still needs someone else

- [ ] **ARYAN — dominance threshold for mixed-language clips.** `confidence` is a
  probability mean, so a genuinely 50/50 Hindi/English clip can score 0.9 and
  sail past the 0.70 gate. "No clearly dominant language" needs a SEPARATE test:
  dominant seconds ÷ total spoken seconds. Real Hindi film dialogue is Hinglish,
  so where we set this decides whether clip A resolves or stalls asking a judge
  to pick a language. Proposal: resolve to the higher language when the top two
  are hi/en and their combined share > 0.85, and record `code_switched: true` in
  the evidence — rather than returning `unknown`.

## Tune at integration time, not now

- **`min_gap`** ships at the stub default 1.6s (`max_duration ≈ 1.35s`, ~4 words).
  Settable via cfg / `DRISHTI_MIN_GAP_S` / CLI, so no code change is needed to
  move it — and env works even if `pipeline.py` never grows a flag. Failure mode
  is soft: too small a gap means Tanishq's fit loop skips the line and
  `verify_job` tolerates `skipped`. Pick the real number with Tanishq once we can
  see actual narration against actual gaps.
- **`-map 0:a:0` / 5.1 downmix** — a 5.1 source sums the dialogue-carrying centre
  channel against music in L/R, which makes Saaras's job harder. Only decidable
  once a real clip exists.

## ⚠ ARYAN — Saaras returns `language_probability: 1.0`, always

Confirmed on real media (the boardroom clip) and on synthetic speech: every
spoken chunk comes back with probability exactly `1.0`. The `0.89` / `0.93` in
`fixtures/jobs/english_sample` are hand-written, not API values.

Consequence: **`confidence` is effectively a constant**, so the `< 0.70` gate in
`resolve_language` will never fire. The only thing that can actually distinguish
a clean single-language clip from a code-switched one is the dominant language's
**share of spoken seconds**, which `gaps.py` tests separately (`MIN_DOMINANCE`,
`TIE_MARGIN`). Don't rely on `confidence` for routing decisions.

## FYI, no action needed

- Clip A is `29.403991s` against the 29.5s `validate` guard — **96 ms of
  headroom**. Tanishq should know before re-trimming anything.
- I'm implementing `confidence` as the probability mean described above; shout if
  that isn't what `pipeline.py` expects.

---

## Run log

| time | clip | params changed | result |
|---|---|---|---|
