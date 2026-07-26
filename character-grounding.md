# Character grounding & silent film — research notes

Scope: extending DRISHTI to silent or near-silent comedy (Chaplin, Mr Bean) and
making narration say **"Mr Bean reaches for the teapot"** instead of **"a man
reaches for the teapot"**.

Status: **research only, nothing built.** This file exists so whoever picks it
up does not repeat the reading. Owner of the affected stage is Ritika
(`scenes.py`, `memory.py`); language routing touches Nishant (`gaps.py`) and
Aryan (`pipeline.py`).

Fixed constraints: **vision and narration stay on OpenAI GPT, TTS stays on
Sarvam Bulbul.** Nothing below changes that.

---

## 1. What breaks today

A silent film does not run through the current pipeline at all. The failure is
exact and reproducible:

1. `gaps.py` finds zero speech chunks, so `detect_language` returns
   `"unknown"` with reason *"not enough speech to identify a language"*.
2. `pipeline.resolve_language` raises `SystemExit`:
   *"Could not determine the spoken language… We never guess a language, and we
   never default to Hindi."*

That guard is correct for its intended case and wrong for this one. `gaps.py`
currently conflates two different situations:

| Situation | Right response |
|---|---|
| There IS speech, and we cannot identify it | Stall and ask — a wrong language ruins the output |
| There is NO speech at all | Not ambiguous. A silent film has no source language |

Only the first should stop the pipeline. The second should carry a distinct
signal (`no_speech: true`, or similar) that the resolver can act on.

**The upside:** a silent film has no source language, so it can be described in
*any* language. Chaplin with Hindi audio description is a strong accessibility
story — the content is already language-agnostic and we are only adding the
missing channel.

Workaround that already works today: `--language en-IN` (or `hi-IN`) bypasses
detection entirely. Good enough to run experiments; not a design.

### 1b. Gap detection inverts

One 90-second silent stretch currently produces **one gap**, which `align` turns
into **one narration line for the whole film**. Useless.

With no dialogue the problem changes from *finding* windows to *pacing* them.
But "narrate anywhere" is also wrong: in Chaplin and Mr Bean **the sound effects
and musical stings are the joke**. Talking over the sting ruins the gag exactly
as talking over dialogue ruins a scene.

This suggests a genuinely nice use of the existing secondary detector: Saaras
reports "no speech anywhere", and energy-based `silencedetect` finds the
**sound-effect peaks to protect**. Dialogue-free windows minus comedic-sound
windows. Same code, inverted purpose.

Chopping one long quiet region into several speakable windows belongs in
`gaps.py` (it is a question about the audio timeline, not about beat selection)
— something like a `max_window_seconds` knob.

---

## 2. What the research does

Lineage is Oxford VGG's AutoAD series.

- **[AutoAD II](https://www.robots.ox.ac.uk/~vgg/research/autoad/v2.html)**
  introduces a **character bank**: for the principal cast, store the character
  name, the actor, and a CLIP feature of their face. Narration then refers to
  characters by name. Characters can be referenced many ways — first name,
  title, profession, relationship, nickname.
- **[AutoAD-Zero](https://arxiv.org/html/2407.15850v1)**
  ([code](https://github.com/Jyxarthur/AutoAD-Zero), ACCV 2024) is the
  training-free version and the most relevant to us: **visual prompting**. An
  off-the-shelf face detector plus face matching produces raw character
  predictions, which are drawn onto the frame as **coloured circles**, with the
  names supplied in the text prompt. The VLM then associates characters with
  actions. Two stages: dense description, then summarisation.
- Reception research with blind and partially sighted viewers
  ([study](https://www.academia.edu/42661118/Film_language_film_emotions_and_the_experience_of_blind_and_partially_sighted_viewers_a_reception_study))
  found conventional objective AD gives satisfactory access to the *story*, but
  an **interpretative** approach conveys *emotion* far more effectively. This
  matters disproportionately for silent comedy, where the emotion IS the
  content. Worth weighing against our current "only visible evidence, never
  infer intent" rule in `scenes.py`.
- AD practice: introduce a character **once** by description, then use the name
  thereafter.
- There is existing AD work on Chaplin's *City Lights*
  ([DCMP](https://dcmp.org/learn/static-assets/nadh211.pdf)) — this is not
  uncharted territory.

---

## 3. Why we do NOT need those models

AutoAD-Zero uses **VideoLLaMA2-7B** for description and **LLaMA3-8B** for
summarisation — small open models that genuinely cannot recognise a character on
sight. The whole face-recognition subsystem exists to compensate for that.

We use a frontier model for vision. **GPT-5.6 very likely recognises Mr Bean and
the Tramp directly** — they are among the most visually iconic characters ever
filmed. The subsystem compensates for a limitation we do not have.

### Licensing, for the record

| Component | Code | Weights |
|---|---|---|
| InsightFace (RetinaFace + ArcFace) | MIT | **`buffalo_l` is non-commercial research only**; commercial use needs a separate licence from InsightFace |
| CLIP (AutoAD II character bank) | MIT | Open |
| AutoAD-Zero | Research code on GitHub | Depends on 7B/8B models run locally |

Two practical blockers beyond licensing:

- `requirements.txt` is ~5 lines and the pipeline is stdlib + ffmpeg, chosen
  deliberately for zero dependency risk. InsightFace pulls onnxruntime, numpy,
  opencv plus model downloads — hundreds of MB over venue Wi-Fi.
- Running a 7B video model locally on a laptop, during an event, is not viable.

---

## 4. Recommended design — cast list + descriptor binding

Keep the model doing what it already does well and put the naming in our code.

1. `scenes.py` keeps describing **only visible evidence** — "a thin man in a
   brown tweed jacket and red tie" — exactly as it does now. No identity claims
   from the model.
2. A small per-title `cast.json` supplies the mapping:
   `{"Mr Bean": ["thin man", "brown tweed jacket", "red tie"], ...}`
3. **Our code** binds descriptor → name and hands the narration prompt
   "this character is Mr Bean".

Why this shape:

- **No refusal risk.** Vision models often decline to identify people in images,
  because that is face recognition of real individuals. Asking "who is this?"
  about Rowan Atkinson's face may be refused. Here the model never asserts an
  identity.
- **No hallucinated names.** A name only appears if a human put it in a cast
  list. A wrong name is far worse than no name: a blind viewer has no way to
  catch it. A failed match degrades to the descriptor, which is still perfectly
  good audio description.
- **It is already in the architecture.** `memory.py` specifies exactly this —
  *"bind 'man in a grey suit' to 'Rohan'"*. Character grounding for silent film
  is the same mechanism applied *within* a clip rather than across clips, and
  `scenes.json` beats already carry an `entities: [str]` field to hold it.

Fallback tiers, in cost order, only if the above fails:

- **Tier A** — permit the model to name characters directly, with a confidence
  gate and descriptor fallback. Cheapest; may be blocked by policy.
- **Tier B** — AutoAD-Zero style visual prompting (coloured circles + names).
  Needs a face detector. Real work, new dependencies, licence questions.
- **Tier C** — unsupervised face clustering into "Person A/B", consistent
  descriptors, names bound later. Most general, most work.

---

## 5. Strategic risk — read before making this the hero demo

Our declared Sarvam capability is **Voice (Saaras + Bulbul)**, and the depth
claim is *"Saaras as a dialogue detector that works through continuous music,
where energy detection and VAD fail."*

**A truly silent film uses Saaras for nothing.** If Chaplin becomes the headline
demo, we have quietly abandoned the capability we are scored on.

Mr Bean is the better choice: it has mumbles, occasional words and a continuous
score, so Saaras still does real work. Recommendation: silent film is the
**second** demo, proving generality — not the lead.

---

## 6. Experiments to run, in cost order

1. **Does GPT-5.6 name Mr Bean unprompted** when the scenes prompt permits it?
   One API call. Settles the whole design question.
2. **If it refuses or hedges — are its descriptors stable across frames?** Is
   the same man described the same way in beat 1 and beat 12? Descriptor
   binding depends entirely on this, and it is the thing most likely to quietly
   not work.
3. **Confirm the language stall** on a real silent clip: one giant gap plus
   `source_language: "unknown"`.
4. **Does `--language hi-IN` carry the rest of the pipeline through?** If yes,
   "Chaplin in Hindi" is demoable almost immediately.
5. **Pacing**: how many windows does a 25s Mr Bean clip need before narration
   feels continuous rather than sparse? Informs `max_window_seconds`.

---

## 7. Open questions

- Where does descriptor → name binding live? `memory.py` is Ritika's and already
  scoped for it, but it is currently specified as a cross-clip registry.
- Does `scenes.py`'s "never infer intent/identity/causality" rule need relaxing
  for silent comedy, given the reception research on interpretative AD? This is
  a real trade-off, not an oversight.
- Who writes `cast.json`, and is a hand-written cast list acceptable for the
  submission, or does it read as cheating? (Prior art says no — AutoAD II's
  character bank is also externally supplied.)
- Sound-effect protection: is energy detection enough to find comedic stings, or
  does that need the vision track too?
