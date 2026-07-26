# DRISHTI — Sarvam Epoch Buildathon Battle Plan (FINAL)

**Team: Aryan (Integration) · Nishant (Listening/STT) · Ritika (Seeing/Vision) · Tanishq (Speaking/TTS + Demo)**
**6-hour build sprint (10:30 AM – 4:30 PM) · Milestone 1 = complete working demo by 12:30 PM · everything after is incremental**

DRISHTI turns a short video into an audio-described video for blind and
low-vision viewers — narration written in natural Hindi/Tamil by Sarvam-30B,
spoken by Bulbul, placed only in dialogue-free windows detected by Saaras,
mixed with automatic ducking. India has ~34M blind/low-vision people and
essentially zero audio-described content in Indian languages.

`drishti_e2e.py` + `DRISHTI_E2E_README.md` in this folder are the **validated
reference implementation** — every API call, ffmpeg filter, and safety margin
has been tested. Tomorrow each person rebuilds their own vertical from it
(function map in §4), which is what makes 12:30 realistic.

---

## 1. Team Structure — Four Independent Verticals

The last hackathon's killer was merging. This plan is built so that **merging
is structurally impossible to get wrong**: four verticals that never touch
each other's files and communicate ONLY through JSON files in a job directory.

```
            ┌─────────────────────────────────────────────┐
            │  jobs/<id>/  — the ONLY shared surface      │
            │  gaps.json · scenes.json · narration.json   │
            └─────────────────────────────────────────────┘
                 ▲               ▲               ▲
   ┌─────────────┴──┐   ┌────────┴───────┐   ┌───┴────────────┐
   │ NISHANT        │   │ RITIKA         │   │ TANISHQ        │
   │ Listening      │   │ Seeing         │   │ Speaking       │
   │ audio extract, │   │ frames, OpenAI │   │ 30B narration, │
   │ Saaras gaps,   │   │ scene beats,   │   │ Bulbul TTS +   │
   │ ducking/mux    │   │ entity memory  │   │ fit loop, Tamil│
   └────────────────┘   └────────────────┘   └────────────────┘
                 ▲               ▲               ▲
            ┌────┴───────────────┴───────────────┴────┐
            │ ARYAN — Integration                     │
            │ contracts+fixtures, orchestrator,       │
            │ FastAPI, web app shell, demo laptop     │
            └─────────────────────────────────────────┘
```

| Person | Vertical | Owns (nobody else ever edits these) |
|---|---|---|
| **Nishant** | **Listening** — everything audio-in and audio-out | `drishti/media.py`, `drishti/gaps.py`, `drishti/mix.py` |
| **Ritika** | **Seeing** — everything visual understanding | `drishti/scenes.py`, `drishti/memory.py`, `demo/scene_fallbacks/` |
| **Tanishq** | **Speaking** — everything language generation + the demo itself | `drishti/narrate.py`, `drishti/speak.py`, `web/src/components/`, `demo/` (clips, IDEA_SCOPE.md, recordings, script) |
| **Aryan** | **Integration** — the product that wraps the verticals | `drishti/pipeline.py`, `drishti/common.py`, `api/`, `web/` shell (`App.tsx`, `api.ts`, state), `fixtures/`, repo/Makefile/.env.example |

Workloads are balanced across the day: Nishant and Ritika are pipeline-heavy
early and shift to reliability/features later; Tanishq is pipeline-heavy early
(narrate+speak have complete reference code) and demo-heavy late; Aryan is
steady integration all day.

---

## 2. Zero-Merge-Conflict Working Agreement (the answer to last time)

1. **One owner per path.** The table above is law. If you need a change in
   someone else's file, message them; you never edit it yourself. Git
   conflicts only happen when two people touch one file — so we make that
   impossible.
2. **Contracts are frozen fixtures, committed first.** At 10:15 Aryan commits
   `fixtures/` — hand-written sample `gaps.json`, `scenes.json`,
   `narration.json`, `status.json` with real values copied from the HANDOFF
   run report. From that moment those shapes ARE the interface. Anyone who
   needs a shape change asks Aryan, who updates the fixture and announces it
   out loud. No silent contract drift.
3. **Every module runs standalone on a job dir.** Each module ends with a
   CLI entry point:
   `python -m drishti.gaps jobs/test1/` reads what it needs from the job dir
   (or the raw clip), writes its own JSON there, prints a summary. You build
   and test your whole vertical all day without the orchestrator, the API, or
   anyone else's code. Sample job dirs with fixture inputs live in
   `fixtures/jobs/` so Speaking can develop before Listening/Seeing finish.
4. **Integration by substitution (Aryan's whole job).** `pipeline.py` runs
   end-to-end from 11:00 with ALL stages faked by fixtures. As each real
   module lands, Aryan swaps it in one at a time and re-runs. A late module
   never blocks integration or the 12:30 gate — its fixture just stays in
   place a little longer.
5. **Trunk-based, tiny commits, rebase-pull.** Everyone commits straight to
   `main` every 20–30 minutes; `git pull --rebase` before every push. Because
   files are disjoint, every rebase is automatic. No feature branches, no
   PRs, no end-of-day mega-merge.
6. **No shared utils dumping ground.** `common.py` (HTTP helpers with retry,
   `run()`, `media_duration()`, `write_json()` — lifted from the reference) is
   written by Aryan in M0 and **frozen at 10:30**. After that, if your module
   needs a helper, copy it into your own file. In a 6-hour event duplication
   is free; a shared utils file is a merge magnet.
7. **The frontend splits by file, not by feature.** Aryan owns the app shell,
   routing, state, and API client; Tanishq owns presentational components in
   separate files (`Timeline.tsx`, `NarrationCard.tsx`, `Player.tsx`,
   `ApprovalCard.tsx`) that receive everything via props. Prop shapes come
   from the same fixtures.

**Job directory layout — the only shared surface:**

```
jobs/<id>/
  input.mp4
  status.json      # {stage, pct, error?, awaiting_approval?}   (Aryan writes)
  gaps.json        # [{start, end, duration}]                   (Nishant writes)
  chunks.json      # raw Saaras chunk cache                     (Nishant writes)
  transcript.txt   # assembled dialogue transcript              (Nishant writes)
  scenes.json      # scene beats                                (Ritika writes)
  narration.json   # [{gap_index, start, end, max_duration,
                   #   text, wav, wav_duration, pace}]          (Tanishq writes)
  narration_XX.wav #                                            (Tanishq writes)
  output.mp4       #                                            (Nishant writes)
```

**API contract** (fixed at 10:15): `POST /jobs` (video + `{language}`) →
`{job_id}` · `GET /jobs/{id}` → status + artifact links ·
`POST /jobs/{id}/narration/{n}` `{text}` (approve/edit) ·
`GET /jobs/{id}/artifacts/...` · stage names fixed:
`validate → gaps → scenes → transcript → narrate → tts_fit → mix → done`.

---

## 3. Winning Strategy — Map Everything to the Rubric

Judges rate **one Sarvam capability** (L1–L5) plus **five product parameters**
(L1–L5). "Depth on one capability beats breadth across several." They score
the demonstrated product, not the pitch.

### Declared capability: **Voice (Speech)** — Saaras v3 + Bulbul v3

Not "Dubbing" (invites comparison to Sarvam's own studio), not vision (that's
OpenAI, a sensor only). Our depth story is L4/L5 material:

- **Saaras as a dialogue detector, not just a transcriber** — chunked STT
  finds narration-safe windows *through continuous background music*, where
  energy-based silence detection and WebRTC VAD both provably fail (on the
  real movie clip, silencedetect found nothing at safe thresholds; Saaras
  found the exact 0–13.5s dialogue-free region).
- **Bulbul with a measure-and-fit loop** — synthesize, measure with ffprobe,
  re-pace, shorten, or skip. Narration provably never overlaps dialogue.
- **Sarvam-30B writes narration natively in Devanagari/Tamil script**, with
  script validation.

### Product parameter game plan

| Parameter | How we score it |
|---|---|
| Job-to-be-done | Upload video → watchable described video. End-to-end. |
| **Memory and Context** | (a) Narration uses the dialogue transcript as context — never narrates what dialogue already says. (b) **Entity memory** (Ritika, Increment 3): clip 2 of the same film remembers "the man in the grey suit" is Rohan. A scored rubric line, not a stretch goal. |
| Creativity | STT-as-VAD insight; duration-fitting loop; human-in-the-loop approval. |
| Impact | 34M+ blind/low-vision Indians; manual AD needs scriptwriter + voice artist + engineer, days and lakhs per hour; we do a clip in ~2 minutes. Lead the demo with this. |
| Delight | Evidence timeline UI, Original ⇄ Described A/B toggle, Hindi ⇄ Tamil switch, Smart Context Pauses finale. |

### Compliance (disqualification risk)

- **All submitted code is written on-site.** `drishti_e2e.py` stays on our
  laptops as reference; it is never copied into the submission repo. AI
  coding assistants are explicitly permitted, and the modular
  package + API + UI rewrite is a genuinely new build.
- **Flag prior exploration in `IDEA_SCOPE.md`**: "We validated Sarvam API
  behaviors (Saaras chunking, Bulbul duration behavior, 30B prompting) in a
  prior CLI experiment; the submitted product was built during the event."
  Hiding origins = auto-DQ; flagging costs nothing.
- **Rotate the exposed Sarvam key tonight.**
- **Clips**: licensed/public/team-owned only for the submission demo.
- One submission per team, fixed window, plus `IDEA_SCOPE.md`.

### Tech stack

Python 3.11+ stdlib + ffmpeg/ffprobe (as the reference — zero dependency
risk) · FastAPI + uvicorn, filesystem job store, 1s polling (no SSE, no DB) ·
Vite + React + Tailwind SPA · everything runs on ONE demo laptop; nothing on
venue Wi-Fi except the (cached) API calls.

---

## 4. Reference Map — What Each Person Rebuilds From `drishti_e2e.py`

Every number below is battle-tested. Change nothing without a reason.

### Nishant (Listening)
| Module | Reference functions | Keep exactly |
|---|---|---|
| `media.py` | `media_duration`, `has_audio_stream`, `extract_audio` | Mono 16kHz WAV; `highpass=f=80,afftdn=nf=-25` denoise (room hiss only — never fights music); silent-track synthesis for no-audio videos |
| `gaps.py` | `detect_gaps_with_saaras`, `detect_gaps`, `sarvam_stt` | `POST /speech-to-text`, `saaras:v3`, `language_code=unknown`; 1.5s chunks via `wave`; empty transcript = no speech; merge consecutive; **150ms edge padding**; cache to `chunks.json`; also writes `transcript.txt`. Keep energy `silencedetect` as secondary mode for quiet clips |
| `mix.py` | `mux` | `adelay` per segment → `amix` → **`apad=whole_dur` + `atrim`** (the truncation-bug fix) → `asplit` → `sidechaincompress threshold=0.015:ratio=8:attack=10:release=250` → `amix duration=first`; video stream copied; aac 192k `+faststart`. Smoke-test on a synthetic tone before real narration |

### Ritika (Seeing)
| Module | Reference functions | Keep exactly |
|---|---|---|
| `scenes.py` | `understand_scenes`, `SCENE_SCHEMA`, `data_url`, `response_output_text`, `extract_frames` | Frames `fps=1, scale=768:-2, q:v 4`; OpenAI Responses API, default `gpt-5.6`, `detail: low`, timestamp text before each image; strict schema with `confidence` + `uncertain_details`; "only visible action, never infer intent/identity/causality" |
| `memory.py` (Increment 3) | — new | Flat per-session JSON registry `{descriptor, name?, first_seen}`; extracted by 30B after each job; injected into the next job's narration prompt |

### Tanishq (Speaking)
| Module | Reference functions | Keep exactly |
|---|---|---|
| `narrate.py` | `generate_narrations`, `select_gap_candidates`, `sarvam_chat_text`, language-name map | `sarvam-30b`, temp 0.1, **`reasoning_effort: None`** (null returns content; "low" burns budget reasoning); **plain-text output** (JSON mode truncated twice on the real clip); "Hindi written in Devanagari" phrasing; beats filtered at confidence ≥ 0.55; `max_spoken_seconds = gap − 0.25`; markdown-fence stripping. **One narration per gap — invariant.** NEW: Devanagari/Tamil-ratio validator + loanword avoidance |
| `speak.py` | `sarvam_tts`, `fit_tts_segments` | `POST /text-to-speech`, `bulbul:v3`, base pace 1.05, 24kHz wav, base64 `audios[0]`; fit loop: re-pace `min(1.5, pace × actual/max × 1.04)` → shorten to `len × max/actual × 0.82` chars → accept at ≤ max+0.08s else **skip** |

### Aryan (Integration)
| Module | Reference functions | Notes |
|---|---|---|
| `common.py` | `http_json`, `http_multipart`, `run`, `require_binary`, `media_duration`, `write_json` | Written in M0, frozen at 10:30 |
| `pipeline.py` | `main` flow + `--segments-json` reviewed mode | The reviewed-segments validator (refuses any human line not fully inside a detected gap) becomes the human-in-the-loop backend in Increment 2 — already designed |
| `api/`, `web/` shell | — new | Background-thread job, `status.json` after every stage, artifact serving; React shell + API client vs fixtures |

Constraints carried over: Saaras REST needs clips **< 29.5s** · never reuse a
job dir for a different clip (stale chunk cache) · Bulbul supports fewer
languages than 30B text — **Tanishq verifies the exact Tamil speaker before we
promise Tamil on stage** · add response caching to 30B and TTS calls too
(reference only caches Saaras) so reruns are free.

---

## 5. Milestone 0 — Setup, 10:00–10:30 (all four)

Repo up with skeleton + module stubs (signatures from §4) · Aryan commits
`common.py` and `fixtures/` (10:15 — contracts frozen) · `.env` with rotated
`SARVAM_API_KEY` + `OPENAI_API_KEY`, proven with one curl each · ffmpeg
checked on all laptops · Tanishq starts trimming the shortlisted clips
(< 29.5s each): **A** dialogue + continuous score with a ≥ 8s dialogue-free
stretch (hero clip) · **B** action-heavy, little speech · **C** two parts of
the same content (entity-memory demo). Locked in `demo/clips/` by 11:00.

---

## 6. Milestone 1 — Basic Demo Ready by 12:30 PM

**One goal: at 12:30 we can stand up and demo DRISHTI end-to-end** — a video
goes in, an audio-described video comes out. If the rest of the day caught
fire, we'd still have a submittable product. Everything later is incremental.

Fully parallel, no cross-dependencies (fixtures stand in for anything
unfinished):

| Who | 10:30–12:15 | Definition of done |
|---|---|---|
| Nishant | `media.py` + `gaps.py`, then `mix.py` smoke test on a synthetic tone | Clip A gap windows printed AND audibly verified with headphones; chunk cache works; tone mux preserves duration |
| Ritika | **Live OpenAI vision call at 10:30 sharp** (the one never-tested path — know by 11:00 if we need fallbacks), then harden `scenes.py`; write reviewed `scene_fallbacks/` JSON for all 3 clips | Factual, timestamp-valid beats for clip A from the real API — or reviewed fallback JSONs ready (vision is a sensor, not the scored capability) |
| Tanishq | `narrate.py` + `speak.py` against `fixtures/jobs/` (real gaps/scenes from HANDOFF values) | One Devanagari-validated Hindi line per gap; fit loop lands WAV ≤ max_duration on clip-A-shaped fixtures |
| Aryan | `pipeline.py` running all-fixtures by 11:00, then substitute real modules as they land; start `api/` | `python -m drishti jobs/clip_a --lang hi-IN` runs every stage with ≥ 3 real modules swapped in and writes `output.mp4` |

**Gate 12:15–12:30 — all four stop and watch the output together** on the demo
laptop, against the checklist: narration fully inside padded gap · zero
dialogue overlap · real Devanagari · one narration per gap · source duration
preserved · both streams present. Copy the MP4 to `demo/` — **fallback demo
#1**, never touched again.

If a module is late, its fixture stays in the pipeline and the gate still
passes — that module lands during Increment 1 instead. Nobody waits on anybody.

---

## 7. Incremental Improvements (12:30 → 4:30) — each one demo-ready

### Increment 1 — Web product (12:30–13:45)
- Aryan: FastAPI job flow live (upload → background thread → per-stage
  status → artifacts) + React shell wired to it.
- Tanishq: presentational components — **evidence timeline** (audio bar:
  dialogue red, narration-safe windows green, scene beats beneath, narration
  card), result `Player` with **Original ⇄ Described toggle**.
- Nishant: clips B and C through his standalone modules; tune padding; fix
  what breaks.
- Ritika: scene quality on clips B and C; prompt tweaks for fast action
  (frame-fps 2 for clip B if needed).

**Check (13:45): live browser upload of clip A → progress → play. Tanishq
screen-records the flow — fallback demo #2.**

### Increment 2 — Human-in-the-loop approval (13:45–14:30)
Pipeline pauses at `narrate`; UI shows the line; presenter edits/approves →
TTS continues. Backend = the reference `--segments-json` validator (refuses
any line not fully inside a detected gap).
- Aryan: pause/resume + approval endpoint. Tanishq: `ApprovalCard.tsx`.
- Nishant: begins reliability runs. Ritika: begins `memory.py`.
- Why it wins: real product feature (broadcasters must approve AD scripts),
  hallucination safety net, and stage insurance — we never play a bad line to
  judges.

### Increment 3 — Memory & Context + Tamil (14:30–15:15)
- Ritika: **entity memory** live — clip C part 1 → part 2, narration says
  "Rohan," not "a man in a grey suit." **A scored rubric parameter.**
- Tanishq: Hindi ⇄ Tamil — native 30B Tamil vs Translate-then-TTS, pick
  whichever sounds better, build only one; Tamil speaker verified in Bulbul.
- Nishant: three consecutive fresh-run successes (clean job dirs, all three
  clips, zero manual recovery) + per-stage latency log (the "~2 minutes"
  claim needs a real number).
- Aryan: language toggle in the shell; keeps integration green.

**Check (15:15): full product. Tanishq records the POLISHED fallback video
and verifies playback + volume on the demo laptop.**

### Increment 4 — Smart Context Pauses (15:15–16:00, stretch — only now)
The agreed exciting feature, built only because the product is already done.
This is real professional practice — **extended audio description** —
name-drop that to judges.
- Ritika: 30B decision prompt — beats + gaps + transcript in, at most **1–2**
  `{pause_at, reason, narration}` out, only where upcoming context is
  essential AND no adequate gap exists. Hard cap of 2.
- Nishant: render, pure ffmpeg — split at `pause_at` → freeze-frame segment
  (`-loop 1` on the extracted frame + narration WAV, source audio silent) for
  `wav_duration + 0.3s`, measured as always → concat.
- Aryan: UI toggle + pipeline wiring. Tanishq: rehearsal + demo script final.
- Ships only if it passes the same gate checklist. Demo moment: video freezes
  as the boardroom door opens, Bulbul explains who is waiting inside, video
  resumes. Judges remember this.

### Submission lockdown — 16:00–16:30 (all four, non-negotiable)
Finalize `IDEA_SCOPE.md` (problem, user, job, outcome + prior-work flag) ·
submit EARLY, never at 16:29 · rehearse the 3-minute demo twice, timed, on
the demo laptop · confirm both fallback recordings play with sound on the
actual demo setup.

---

## 8. The 3-Minute Demo (handbook-mandated structure)

Presenter: **Tanishq**. Laptop: **Aryan**. Rehearsed twice.

- **0:00–0:30 — Problem, plain language.** "India has 34 million blind and
  low-vision people. Almost no Indian-language content has audio
  description — the narration that tells them what's happening on screen.
  [5s of clip A audio-only:] This is what a movie is for a blind viewer."
- **0:30–1:00 — Current workflow.** Manual AD needs a scriptwriter, a voice
  artist, and a sound engineer — days of turnaround, lakhs per hour. So in
  Hindi and Tamil it simply doesn't get made.
- **1:00–3:00 — Live demo.** Upload clip A → evidence timeline ("Saaras is
  finding where nobody speaks — the music never stops; energy detection
  can't do this") → approve the Hindi line on screen → play the result.
  Boosters, one line each: flip to Tamil; clip C part 2 remembering "Rohan";
  if Increment 4 shipped, one context pause. **Close on outcome:** "One clip,
  about two minutes, any of ten Indian languages."
- Fallback recording one keypress away. If Wi-Fi or an API stalls, narrate
  over the recording without apologizing.

**Cut order if time runs short** (last-first): Increment 4 pauses → Tamil
toggle → entity memory → human-in-loop. **Never cut:** fallback recordings,
three-clip verification, rehearsal.

---

## 9. Risk Register

| Risk | Mitigation |
|---|---|
| Merge conflicts (last time's killer) | §2: one owner per path, frozen fixture contracts, standalone modules, integration by substitution, rebase-pull, frozen `common.py` |
| A module runs late | Its fixture stays in the pipeline; the 12:30 gate passes anyway; module lands next increment |
| OpenAI vision misbehaves (never live-tested) | Ritika tests 10:30 sharp; reviewed `scene_fallbacks/` per clip; vision is a sensor, not the scored capability |
| Rate limits / venue Wi-Fi | Disk caching on every API call; all three clips processed + cached by 15:15; two fallback recordings |
| 30B outputs English/Hinglish or truncated JSON | Plain-text output + explicit script phrasing + script-ratio validator + approval step |
| TTS overruns gap | Fit loop (pace → shorten → skip); clips chosen with ≥ 8s gaps |
| Tamil speaker unsupported in Bulbul | Tanishq verifies in Increment 3 before we promise it on stage |
| Stale chunk cache | New job dir per upload, enforced by design |
| Demo laptop audio fails on stage | Test on venue speakers at lunch; phone recording as last resort |
| Disqualification over prior work | Reference script never enters the submission repo; prior exploration flagged in `IDEA_SCOPE.md` |

---

## 10. Tonight (prep only — NO code, per the rules)

1. Rotate the Sarvam API key; confirm the OpenAI key exists and has credit.
2. All four read `HANDOFF.md`, `drishti_e2e.py`, and this plan; confirm the
   §1 verticals (swap only tonight, never mid-event); pick the demo laptop.
3. Verify ffmpeg/ffprobe, Python 3.11+, Node 20+ on all four laptops.
4. Shortlist licensed/owned candidate clips so Tanishq only trims tomorrow
   (A: dialogue + continuous score + ≥ 8s dialogue-free stretch; C: two parts
   with recurring characters).
5. Everyone skims the Sarvam docs for THEIR vertical: Nishant — Saaras REST;
   Tanishq — Bulbul v3 params + Tamil speakers, 30B chat; Ritika — OpenAI
   Responses vision; Aryan — quickstart + `llms.txt` (point Claude Code at it
   during the event).
6. Charge everything; pack headphones (all-day audible gap verification) and
   a small speaker for volume self-tests; IDs; confirm all four registrations
   approved.
7. Double-check the event date/time — handbook says Sunday July 26, 10:00 AM,
   Razorpay Arena, submissions close 4:30 PM.
