# Fixture job — english_sample

The canonical shape of every file in a job directory. Build your stage to
produce exactly this, and integration is a non-event.

This is the real 29.4s boardroom clip from `HANDOFF.md`: real timings, real
detected gap, real scene beats, real spoken transcript. The clip is in English,
so under the auto-language rule the correct output is **English narration** —
which is exactly the invariant worth pinning down in a fixture.

There is no `input.mp4` or `output.mp4` here — media is gitignored and this
fixture exists to pin down JSON contracts, not to render a video. Check it with:

```bash
make fixture
```

That validates every invariant it can without media, and prints the language the
resolver picks. It should print `en-IN`.

## Who writes what

| File | Owner | Stage |
|---|---|---|
| `meta.json` | Nishant | validate |
| `gaps.json`, `chunks.json`, `transcript.txt`, `language.json` | Nishant | gaps |
| `scenes.json` | Ritika | scenes |
| `segments.json` | Aryan | align |
| `narration.json` | Tanishq | narrate (text) and tts_fit (wav fields) |
| `status.json` | Aryan | written by the pipeline only — no stage touches it |

`chunks.json` here is abridged to the boundary chunks. A real run has one entry
per 1.5s chunk, about twenty for a 30-second clip.

## The two fields that matter most

`language.json` carries **evidence**, not policy. Nishant reports what Saaras
heard; the pipeline decides the output language. Write `"unknown"` rather than
guessing when there are under two speech chunks, confidence is below 0.70, or
no language dominates.

`narration.json` gains its `wav`, `wav_duration` and `pace` fields during
`tts_fit`. Before that stage runs, an entry has only the text fields — which is
why `make fixture` verifies with tolerance for a partial run.

`segments.json` is what `narrate` actually reads. Note that its one segment
carries **all five** beats and a `char_budget` of 163: align hands over
everything that overlaps the window and leaves the editing to the model, since
merging beats into a clause is a language problem, not a timing one. Each beat
gains a `when` of `during`, `before` or `after` — `after` beats are look-ahead
and must never be narrated as though they already happened.

## Want a Hindi fixture too?

Copy this directory, set `source_language` to `hi-IN` in `language.json`, and
replace the transcript and narration text with Devanagari. The script validator
will hold you to it.
