# Localized Ad Engine
### Technical Architecture & Team Brief — Problem Statement 3 (Multimodal Creative Pipelines with GenMedia)

---

## 1. What We're Building

A pipeline that takes **one product + one campaign brief** and outputs **5 fully localized ad videos** for 5 different markets — each with region-appropriate visuals, culturally fitting gestures/context, and locally suited music — generated end-to-end in a single run.

**One-line pitch:** *"Type your ad brief once. Get five market-ready ad videos, each localized in visuals, motion, and sound, before your coffee gets cold."*

### Why this satisfies PS3's bar
The challenge explicitly rejects "a prompt box that spits out one image or clip." Our pipeline is a genuine chain:

```
Text Brief → NB2 Lite (visuals) → Omni (video + motion) → Lyria (music) → Final Ad
```

...repeated **5 times in parallel** (one per market), with each stage's output feeding the next stage's input. Speed and cross-modal continuity are load-bearing: if NB2 Lite were slow, we couldn't generate 5 variants live; if Omni couldn't take an image as a seed, we'd lose visual consistency between the still frame and the video; if Lyria weren't fast/steerable, music would be an afterthought bolted on at the end instead of matching each market's vibe.

---

## 2. Product Requirements (What "Done" Looks Like)

**Input (from user):**
- Product name + short description
- Product image(s) (optional — can also be generated)
- Campaign brief / key message (e.g., "affordable, family-friendly, everyday use")
- Target markets (e.g., India, USA, Japan, Brazil, Nigeria — pick 5 with genuinely distinct visual/cultural/musical identities for demo impact)

**Output (per market):**
- 1 short video ad (5–15 seconds), showing:
  - A regionally appropriate model/setting/context using the product
  - A culturally fitting gesture or interaction (e.g., a nod, a specific greeting, a locally common use-case)
  - A background music track in a genre/instrumentation fitting that region
- A side-by-side gallery view of all 5 outputs for easy comparison (this is your demo's money shot)

**Non-goals (say no to these to protect your timeline):**
- Perfect lip-sync / voiceover localization (out of scope — visuals + music only, no spoken dialogue, unless time allows as a stretch)
- User-uploaded video editing — we generate from scratch, we don't edit existing footage
- More than 5 markets in the live demo (can be configurable in the code, but demo 5)

---

## 3. System Architecture

### 3.1 High-Level Flow

```
┌─────────────────┐
│   User Input     │  Product + Brief + Target Markets
│  (Web UI Form)   │
└────────┬─────────┘
         │
         ▼
┌─────────────────────────────┐
│   Orchestrator (Backend)     │  Fans out one job per market
│   - Validates input           │
│   - Builds per-market prompts │
│   - Tracks job state          │
└────────┬─────────────────────┘
         │  (parallel, 1 branch per market)
         ▼
┌───────────────────────────────────────────────────────┐
│  Per-Market Branch (x5, running concurrently)           │
│                                                           │
│  Step A: Prompt Localization                             │
│    → Gemini 3.8 Flash rewrites the generic brief into a  │
│      market-specific visual/cultural prompt               │
│                                                           │
│  Step B: Image Generation (NB2 Lite)                     │
│    → generates 1–2 candidate hero frames                 │
│    → (1K image, <2s — fast enough to show live)           │
│                                                           │
│  Step C: Video Animation (Gemini Omni Flash)              │
│    → takes the NB2 Lite frame as a seed/reference          │
│    → animates it into a short clip with physically        │
│      consistent motion + locally fitting gesture           │
│    → conversational edit pass if needed                    │
│                                                           │
│  Step D: Music Scoring (Lyria 3.5)                        │
│    → generates a short instrumental track matching the     │
│      market's genre/instrumentation                        │
│                                                           │
│  Step E: Assembly                                          │
│    → merges video + audio track                            │
│    → outputs final .mp4                                    │
└────────┬──────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────┐
│   Results Gallery UI      │  5 videos, side-by-side, playable
└─────────────────────────┘
```

This whole system breaks cleanly into **three independent layers of work** — Frontend, Backend/Orchestrator, and the Models Engine — so three people (or three sub-teams) can build in parallel with a fixed contract between them, and only need to sync at the boundaries. That's what Sections 3.2–3.5 define.

---

### 3.2 Layer 1 — Frontend (Client)

**Owns:** everything the user and judges actually see. Talks *only* to the Backend API — never calls any generation model directly.

**Responsibilities:**
- Input form: product name/description, optional product image upload, campaign brief text box, market multi-select (default 5 pre-picked, editable)
- "Generate" trigger → calls Backend `POST /campaigns`
- Job status polling/streaming → calls Backend `GET /campaigns/{id}/status` (or subscribes via WebSocket/SSE if time allows)
- **Progressive results gallery** — 5 cards, one per market, each independently flipping from `pending → generating → done/error` as the backend reports it. This is the single most important UI behavior for the demo (Section 8) — do not build a "spinner until all 5 are done" screen.
- Video/audio playback per card (native `<video>` element, muxed file from backend)
- Error state per card (so one failed market doesn't block the other four from displaying)

**Suggested folder structure:**
```
/frontend
  /src
    /components
      CampaignForm.jsx       — input form
      MarketCard.jsx         — one gallery card (status + player)
      ResultsGallery.jsx     — grid of MarketCards
    /api
      client.js              — thin wrapper around Backend REST/WS calls
    App.jsx
```

**Explicitly NOT the frontend's job:** prompt writing, calling NB2 Lite/Omni/Lyria directly, ffmpeg muxing, cultural-cue logic. If a teammate building frontend ever needs to know *what a market's prompt looks like*, that's a sign of a layer violation — it should be opaque to them.

---

### 3.3 Layer 2 — Backend / Orchestrator

**Owns:** turning one campaign request into 5 concurrent market jobs, tracking their state, and serving results to the frontend. This is the layer that makes the demo feel fast — **concurrency lives here, not in the Models Engine.**

**Responsibilities:**
- `POST /campaigns` — validates input, creates a job record, kicks off 5 concurrent market branches (`asyncio.gather` / `Promise.all` / a lightweight queue — pick whatever your team is fastest with, don't over-engineer this)
- Per-market branch = one call into the **Models Engine** (Layer 3) treated as a single black-box function: `run_market_branch(product, brief, market) → {video_url, status}`. The Backend does not know or care *how* that function talks to Flash/NB2 Lite/Omni/Lyria — that's Layer 3's problem entirely.
- Job/state store — even an in-memory dict is fine for a hackathon; each market branch updates its own status independently so partial completion is visible
- `GET /campaigns/{id}/status` — returns current state of all 5 branches (or push updates via SSE/WebSocket for the progressive reveal)
- Error handling/retry — if one market's branch throws, catch it, mark that card `error`, and do **not** let it take down the other 4 branches
- Serves finished `.mp4` files (static file serving or signed URLs, whichever is faster to wire up)

**Suggested folder structure:**
```
/backend
  /api
    routes.py                — POST /campaigns, GET /campaigns/{id}/status
  /orchestrator
    job_manager.py            — fan-out to 5 branches, state tracking
    market_branch.py          — wraps one call into the Models Engine
  /storage
    files.py                  — save/serve generated .mp4s
main.py
```

**Explicitly NOT the backend's job:** deciding *what* a Japan-market prompt should contain, or how to phrase a Flash call — that logic is owned entirely by Layer 3 so it can be tested/tuned independently of the API plumbing.

---

### 3.4 Layer 3 — Models Engine (the actual GenMedia pipeline)

**Owns:** everything creative and model-specific. This layer exposes exactly **one function** to the Backend: given a product, brief, and a single market, produce one finished, muxed video file. Everything below is internal to this layer.

```
run_market_branch(product, brief, market):

  Step A — Prompt Localization (Gemini 3.8 Flash)
    input:  generic brief + market name
    output: market-specific visual prompt + gesture/context prompt
            (pulls from the cue table in Section 4 as grounding context,
             not from scratch each time)

  Step B — Image Generation (NB2 Lite)
    input:  localized visual prompt (+ product image, if provided)
    output: 1 hero frame (1K image)

  Step C — Video Animation (Gemini Omni Flash)
    input:  the exact image from Step B as reference/seed
            + localized gesture/motion prompt
    output: short animated clip, visually consistent with Step B's frame

  Step D — Music Scoring (Lyria 3.5)
    input:  market's genre/instrumentation descriptor (from cue table)
    output: short instrumental track matching clip length

  Step E — Assembly (ffmpeg, server-side)
    input:  Step C video + Step D audio
    output: final .mp4 for this market

  return: { market, video_path, status }
```

**Suggested folder structure:**
```
/models_engine
  /prompts
    market_cues.py            — the cue table from Section 4, as data
    localize.py                — builds the Flash call, parses its output
  /generation
    image_gen.py               — NB2 Lite call wrapper
    video_gen.py                — Omni call wrapper (takes image ref)
    music_gen.py                — Lyria call wrapper
  /assembly
    mux.py                      — ffmpeg video+audio merge
  pipeline.py                   — run_market_branch(), ties A→B→C→D→E together
```

**This is where your actual differentiation lives** (Section 4's cue table, the Flash-as-localizer trick, the image→video consistency handoff). Whoever owns this layer should be the strongest at prompt design and comfortable debugging "why doesn't the video match the image" — treat it as the highest-risk, highest-value layer and staff/schedule it accordingly (see Section 7, hours 0–6 are almost entirely about proving this layer works for one market before anything else).

---

### 3.5 Contract Between Layers (what each side can assume)

| Boundary | Frontend ⇄ Backend | Backend ⇄ Models Engine |
|---|---|---|
| Call shape | REST (`POST /campaigns`, `GET /campaigns/{id}/status`) + optional SSE/WS for live updates | Direct in-process function call: `run_market_branch(product, brief, market)` |
| Frontend/Backend knows about markets | Yes — as a list of names/labels only | N/A |
| Who knows prompt content | Nobody outside Layer 3 | Layer 3 only |
| Who knows model API keys/SDKs | Nobody outside Layer 3 | Layer 3 only |
| Failure unit | One market card can independently show `error` | One branch throwing must not crash the other 4 |
| What crosses the boundary | JSON status + a video URL once ready | Product/brief/market strings in; a file path + status out |

Keeping this contract strict is what lets 3 people build simultaneously without blocking each other — the Frontend dev can build against a **fake/mocked** Backend response from hour 0, and the Models Engine dev can build and test `run_market_branch()` in complete isolation (even from the command line) before the Backend ever calls it for real.

### 3.6 Data Flow Between Models Inside Layer 3 (the part judges will probe)

This is the trickiest part to get right and the part most teams will get wrong — **make sure the video actually looks like the generated image**, not a random new generation:

- NB2 Lite outputs an image → **that exact image is passed as an input/reference to Omni**, not just its text description. Omni's strength per the challenge brief is "generate and conversationally edit video with real-world physics" — we lean on it being able to take a visual seed and animate *that specific scene*, preserving product appearance, model, and setting consistency between the still and the motion.
- If Omni's API only accepts text + optionally a reference image, structure the call as: `{reference_image: nb2_output, prompt: "animate this scene: [locally-adapted motion/gesture description]"}`.
- Lyria receives a **short text mood/genre descriptor** per market (not the video itself, unless Lyria supports audio-to-video sync in this version) — e.g., "upbeat Afrobeat-influenced instrumental, family-friendly, 10 seconds" for the Nigeria variant.

---

## 4. Per-Market Prompt Strategy (This *Is* the Product)

The core IP of this project isn't the API calls — it's the **prompt/context design per market**. Prep this table before the hackathon starts so you're not improvising cultural details under time pressure:

| Market | Visual cues | Gesture/context | Music genre |
|---|---|---|---|
| India | Multi-generational household, bright colors, festival-adjacent warmth | Namaste-adjacent greeting, shared family meal context | Bollywood-influenced upbeat instrumental |
| USA | Suburban/urban casual setting, individualistic framing | Thumbs-up / casual wave, solo or small-group use | Pop/indie acoustic |
| Japan | Minimalist setting, clean composition | Slight bow, precise/considerate handling of product | Lo-fi / city pop instrumental |
| Brazil | Vibrant outdoor setting, communal energy | Warm handshake/hug-adjacent greeting, group context | Samba/bossa-influenced instrumental |
| Nigeria | Bold patterns, marketplace or family-gathering setting | Expressive greeting, communal sharing gesture | Afrobeat instrumental |

⚠️ **Team note:** Treat this table as a first draft, not gospel — have at least one teammate sanity-check each market's cues before the demo so nothing reads as a stereotype rather than a genuine cultural nod. This is a real risk with this idea and worth 15 minutes of team discussion up front.

---

## 5. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Frontend | React (simple form + gallery grid) | Keep it minimal — input form, "Generate" button, 5-card results gallery with video players |
| Backend/Orchestrator | Node.js or Python (FastAPI) | Handles fan-out to 5 parallel market jobs, job status polling |
| Prompt Localization | Gemini 3.8 Flash API | One call per market to expand brief → localized prompt |
| Image Gen | NB2 Lite (`gemini-3.1-flash-lite-image`) | 1 call per market for hero frame |
| Video Gen | Gemini Omni Flash (`gemini-omni-1.1-flash`) | 1 call per market, image-seeded |
| Music Gen | Lyria 3.5 (`lyria-3.5`) | 1 call per market |
| Assembly | ffmpeg (server-side) | Mux video + audio into final .mp4 |
| Job orchestration | Simple async queue (or just `Promise.all` / `asyncio.gather` if time-constrained) | 5 branches run concurrently, not sequentially — this is what makes the demo fast |

**Concurrency matters more than any individual model's speed.** Running all 5 markets sequentially would make the demo feel slow even with fast models. Structure the orchestrator to fire all 5 branches at once and just render results into the gallery as each one completes (progressive reveal — a card flips from "generating..." to "done" independently), which also gives you a nice visual "watch them land one by one" demo moment instead of one long blocking wait.

---

## 6. Suggested Team Split (Mapped to the 3 Layers)

| Role | Owns | Responsibilities |
|---|---|---|
| **Frontend Engineer** | Layer 1 (Section 3.2) | Input form, progressive results gallery, per-card status/playback, error states — builds against a mocked Backend response first |
| **Backend Engineer** | Layer 2 (Section 3.3) | Job fan-out/concurrency, state tracking, REST/SSE API, file serving, retry/error isolation between the 5 branches |
| **Models/Prompt Engineer(s)** | Layer 3 (Section 3.4) | The market cue table (Section 4), the Flash localization call, NB2 Lite/Omni/Lyria wiring, ffmpeg assembly, image→video consistency — highest-risk layer, ideally 2 people if headcount allows |
| **Demo/Narrative** | Cuts across all 3 | Prepares the live demo script (Section 8), picks the actual product + brief used on stage, has a backup pre-generated set in case live generation hiccups |

If your team is only 3 people, merge Backend + Frontend into one person (the contract in Section 3.5 keeps this manageable) and keep Models Engine as its own dedicated owner — that layer is where the actual win condition lives.

---

## 7. Build Plan (Rough Timeline for a ~24hr Hackathon)

1. **Hours 0–2:** Lock the 5 markets, draft the cue table, get raw API access/keys working for all 3 models with a single hardcoded test call each.
2. **Hours 2–6:** Build the orchestrator — one market branch end-to-end (Flash → NB2 Lite → Omni → Lyria → ffmpeg mux) working for **one** market before parallelizing to 5.
3. **Hours 6–10:** Parallelize to all 5 markets, add progressive UI updates.
4. **Hours 10–14:** Build the frontend gallery, wire it to the backend, get one full clean run working end-to-end.
5. **Hours 14–18:** Cultural review pass on outputs, refine prompts per market, fix anything that looks generic or off.
6. **Hours 18–22:** Polish, error handling, pre-generate a backup "known good" set of 5 videos in case live generation fails on stage.
7. **Hours 22–24:** Rehearse the demo, tighten the script, sleep if humanly possible.

---

## 8. Demo Script (90 Seconds, the Part Judges Actually Remember)

1. **(10s)** "We built a pipeline that takes one ad brief and outputs five fully localized ads — different visuals, different gestures, different music — for five different markets, in one run."
2. **(10s)** Type in a real product + brief live on stage (something simple and universal — e.g., a water bottle, a phone case).
3. **(15s)** Hit generate. While it's running, narrate the pipeline out loud: "Right now, Flash is writing five culturally-specific creative briefs, NB2 Lite is generating five hero images in parallel, and Omni is about to animate each one."
4. **(30s)** Watch the gallery populate live, card by card, as each market's video completes — this is your visual proof that it's real and parallel, not pre-recorded.
5. **(15s)** Play 2 of the 5 finished videos side by side, pointing out the specific localized details (gesture, setting, music genre) so judges see it's not just "same video, different color grade."
6. **(10s)** Close: "One brief, five markets, one pipeline run — and it's fast enough that we just did it live."

**Backup plan:** if live generation is slow or flaky under conference wifi, have the same product's 5 outputs **pre-generated and cached**, and be upfront that you're showing a cached run of the same pipeline you just triggered live — judges respect honesty here far more than a demo that visibly breaks.

---

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Cultural cues read as stereotypes, not authenticity | Team review pass (Section 4 note); when in doubt, favor broadly recognized+respectful cues over anything edgy |
| Omni doesn't preserve visual consistency from the NB2 Lite seed | Test this specific handoff FIRST (hour 0–2), before building anything else — if it doesn't work as expected, this is the idea's single biggest risk and needs a fallback (e.g., re-prompt Omni with the image description instead of raw image reference) |
| Live demo network/API flakiness | Pre-generated backup set, rehearsed fallback narration |
| 5 parallel branches overwhelm rate limits | Test concurrency limits early; add basic retry/backoff; consider staggering by ~1-2s if needed |
| Runs generic — judges have seen "localized ad" ideas before | Lean hard into the **live, parallel, progressive-reveal demo** and the **cultural-cue engineering** as your differentiators, not just "we called 3 APIs" |

---

## 10. Stretch Goals (Only If Core Pipeline Is Solid Early)

- Add a 6th "custom market" the audience picks live on stage (real-time proof it's not hardcoded to 5 fixed markets)
- Simple voiceover/tagline localization layer (text-to-speech per market) if time allows
- A "brand consistency score" — Flash re-checks all 5 outputs against the original brief and flags any that drifted off-message
- Let users tweak one market's video conversationally after generation ("make the Japan version more minimalist") to show Omni's conversational-edit capability explicitly, since the brief calls that out

---

**Bottom line for the team:** the win condition here isn't "we called three GenMedia APIs" — it's "we built a system where cultural localization is a first-class design decision, generation is genuinely parallel and fast enough to demo live, and the video-image-audio handoff is visibly consistent, not three disconnected outputs stitched together."
