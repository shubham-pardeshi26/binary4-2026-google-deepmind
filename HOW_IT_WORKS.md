# How AdLoop works, end to end

This follows **one ad run** through the code, from the moment someone clicks **Launch loop** to the finished, scored film and every edit afterwards. File and function names are given so you can jump straight to the code.

> **Heads-up: the docs promise more than the code does.** `README.md` / `WRITEUP.md` (and CONTRACT §9) describe voiceover narration (Flash TTS), WebVTT captions, a narrated presentation, per-market narrated animatics, a `/voiceover` endpoint, and a Ken Burns fallback when Omni fails. **None of that is in the code.** The only trace is the `model_tts`/`tts_concurrency` settings in `app/config.py`. Everything below describes what actually runs. See [§12](#12-docs-vs-code-gaps) before you submit or pitch.

---

## 0. The shape of the system

```
browser (static/app.js)
   │  POST /api/runs (multipart brief)          GET /api/runs/{id}/events (SSE)
   ▼                                             ▲
app/main.py  ──► RunManager.create_run ──► Run.start ──► Run._pipeline (background task)
                                                  │
                         app/prompts.py  (creative logic: plan, judge, direction, prompts)
                                                  │
                         app/genai_client.py  GenMedia  ← the ONLY code that calls Google
                                                  │        (or app/mock.py in mock mode)
                         app/media.py  (ffmpeg: normalise, crossfade, fit music, stitch)
                                                  │
                         app/events.py  EventBus → events.jsonl + SSE fan-out
```

Three ideas hold it together:
1. **Pipelined, not barriered.** Every scene is its own async chain. Scene 1 can be rendering video while scene 4 is still being judged.
2. **Event-sourced.** Every state change is an event. The browser never polls; it rebuilds from a snapshot plus the event stream, and a finished run can be replayed from its log.
3. **One model adapter.** `GenMedia` hides preview-API uncertainty (path fallbacks, retries, concurrency caps, mock mode), so the pipeline code is identical whether the models are real or mocked.

---

## 1. Launch: browser → server

**`static/app.js`, brief panel.** The user types a brief (or records one: MediaRecorder → `POST /api/transcribe` → `GenMedia.transcribe`, which fills the textarea). They set brand, aspect (16:9 / 9:16), scenes (3–6), variants per scene (2–6), an optional product photo, and markets, then click **Launch loop**. That sends a multipart `POST /api/runs`.

**`app/main.py`, `create_run`.** It validates everything (brief length, aspect, ranges, the image sniffed as PNG/JPEG/WebP, ≤12MB), then applies two guards:
- **concurrent-run cap** (`ADLOOP_MAX_CONCURRENT_RUNS`, default 3) → 429 "studio is busy"
- **per-IP hourly limit** (`RateLimiter`, `ADLOOP_RUNS_PER_IP_PER_HOUR`) → 429

It then calls `RunManager.create_run(...)` and returns `{"run_id"}` **immediately**. All real work happens in the background.

**`app/pipeline.py`, `RunManager.create_run` → `Run(...)` → `Run.start()`.**
- **Setup:** it creates the run folder `data/runs/<8-hex-id>/`, the initial state skeleton (the exact JSON shape in CONTRACT §6), and an `EventBus`.
- **Start:** `start()` spawns `_pipeline()` through `Run.spawn`, which wraps every background coroutine so **any exception becomes `stage:error` + `error` + `log` events** instead of crashing the server.

**Browser, `openRun` → `connect`.** It sets `#run=<id>` in the URL, fetches `GET /api/runs/{id}` (a snapshot), then opens an `EventSource` on `/api/runs/{id}/events`. From here on, the UI just applies events (§8).

---

## 2. Creative director: brief → plan

**`Run._pipeline` → `prompts.plan_campaign(gm, brief, brand, aspect, n_scenes, markets, product_image)`**
- **The call:** `plan_campaign` sends `GenMedia.generate_json(parts, PLAN_SCHEMA, system=DIRECTOR_SYSTEM, temperature=0.9)`, with `gemini-3.8-flash` as the creative director. The product photo, if any, is included as an image part.
- **The plan it returns:**
  - campaign name, tagline, CTA
  - a **brand bible** (palette hexes, visual style, mood, typography, product, hero)
  - an `anchor_prompt`
  - N scenes, each with beat (hook/build/reveal/cta), duration, image prompt, motion prompt, camera, mood, energy and on-screen text
  - a music brief (genre, bpm, key, instruments, arc)
- **Validation:** `normalize_plan` repairs anything missing or out of range, so downstream code can trust the shape.

**`Run._install_plan`.** It normalises scene ids to `s1..sN`, builds per-scene state (variants, judge, winner, clip) and creates the in-memory `_SceneRuntime` for each scene:
- two locks: `board` for storyboard rounds and `clip` for video renders
- a `winner_ready` event
- a variant index counter
- a `render_token`

It then emits `plan` and `stage director done`.

---

## 3. Immediate fork: music starts, anchor starts

The plan already contains durations and moods, so the slowest asset (music) starts **right away**, in parallel with the images:

```python
self.spawn(self.render_music(reason="initial"), stage="music")   # Lyria, never waits for images
await self._make_anchor()                                          # NB2 continuity frame
chains = [self.spawn(self._scene_chain(sid)) for each scene]       # one independent chain per scene
```

**`_make_anchor`.** NB2 generates one clean hero/product **continuity reference frame** from the plan's `anchor_prompt`, brand style and palette. If a product photo was uploaded, it's passed as a reference. If the anchor fails, the run logs a warning and continues without it.

**Continuity rule (`Run.refs`).** Every later NB2 call gets `[base image if editing] + anchor + product photo` as reference images, plus the brand bible inside the prompt. That's what keeps the hero and product looking the same across scenes.

---

## 4. Per-scene chain: storyboard → judge → repair → winner

**`_scene_chain(scene_id)`** takes the scene's `board` lock, runs `_storyboard_round(kind="initial")`, and if that produces a winner, goes straight to `render_clip`. Every scene does this **independently and concurrently**.

**`_storyboard_round`:**
1. **Prompt:** `prompts.scene_image_prompt(plan, scene)` builds the full NB2 prompt (scene prompt + brand bible + continuity rules + composition for the aspect ratio).
2. **Fan-out, `_gen_variants`:** K NB2 calls run in parallel via `asyncio.gather`, each with a deterministic seed (`crc32(run:scene) + idx`). **Each variant is emitted the moment it lands**, as a `variant` event carrying its URL, latency and API path, which is why tiles pop in one by one. A failed variant emits `variant_error`; the others carry on.
3. **Judge, `_judge` → `prompts.judge_scene`:** Flash vision gets all K images plus the anchor and scores each 0–10 on five axes: brief fit (25%), brand consistency (20%), composition (20%), continuity (15%) and artefact-free (20%). `normalize_judgement` recomputes the weighted overall score. The judge also returns `winner_index`, a rationale and `fix_instructions`. Scores are mapped back to global variant indices, then the `judge` event is emitted.
4. **Self-repair:** if the best score is below `ADLOOP_JUDGE_THRESHOLD` (default 7.0) and there are repair rounds left (default 1), NB2 generates **2 edits of the winner** with the judge's fix instructions (base image first in refs). The judge then re-scores **winner vs repairs**. This is draft → verify → fix for images.
5. **`set_winner`** stores the winner, sets `winner_ready` and emits `winner` (`by: "judge"`).

---

## 5. Motion: winner → Omni clip

**`render_clip(scene_id, token, reason)`:**
- **Queue:** it emits `clip_status: queued`, then takes the scene's `clip` lock, so renders and edits of *the same* clip run in order while different clips run in parallel.
- **Stale check:** if `token != render_token`, a newer select/regenerate superseded this request and it's dropped.
- **Render:** it reads the winning keyframe and builds `prompts.scene_motion_prompt` (camera move, physics, mood, "keep keyframe identity"), then calls `GenMedia.generate_video(prompt, image=keyframe, aspect, seconds)`. `seconds` is the planned duration, capped by `ADLOOP_VIDEO_SECONDS`.
- **Progress:** `_progress_cb` turns Omni's polling into throttled `clip_status: rendering, elapsed_ms` events, which drive the live timer on the clip card.
- **Success:** it saves `s1_clip_v1.mp4` and adds a clip version storing **`interaction_id`**, which later edits need. It emits `clip`, then calls `request_stitch()`.
- **Failure:** it emits the error. The clip is marked `error`, or stays `done` if an older version exists.

---

## 6. Music: Lyria

**`render_music(reason, instruction)`:**
- **Coalescing:** a music token plus a lock mean that if three re-scores are requested while one is rendering, only the newest one runs next.
- **Prompt:** `prompts.music_prompt(plan, scenes_for_prompts(), total_seconds)` builds a **timed prompt** from the scene plan, e.g. "0:00–0:06 hook — <mood>, <energy>…", plus genre, bpm, key and instruments, "instrumental, no vocals", and a resolved ending for the CTA. Live moods (after edits) are merged in via `plan_scene`.
- **Call:** `GenMedia.generate_music(prompt, seconds)` goes to Lyria 3.5 via the Interactions API. It saves `music_v{n}`, emits `music`, then calls `request_stitch()`.
- **Failure:** the music is marked error, but `request_stitch()` is still called, so a missing soundtrack never blocks the first cut.

---

## 7. Final cut: debounced, single-flight stitch

**`request_stitch()`.** Before the first cut, it does nothing until `_stitch_ready()` holds: every scene has settled (clip done **or** failed), at least one clip exists, and music has settled. After that, any change (new clip version, new music version, winner override) marks the cut dirty.

**`_stitch_loop` / `_stitch_once`.** One consumer waits for a quiet window (0.25s before the first cut, 1.5s after), so bursts of changes collapse into one stitch. It then calls `media.stitch(clips, music, final_v{n}.mp4, aspect)`:
- each clip is scaled/padded to 1280×720 (or 720×1280) at 30fps and trimmed to its exact probed duration
- clips are joined with short crossfades
- the music is trimmed or padded to the film's length, with a fade-out and loudness normalisation
- clip audio is dropped unless `ADLOOP_KEEP_CLIP_AUDIO=1`
- the file is written with faststart

It then emits `final`. On the first cut it also sets `status: done` and emits **`run_done`**, whose `wall_ms` is the headline time-to-final.

ffmpeg comes from `PATH`, or from the binary bundled with `imageio-ffmpeg` (`media.ffmpeg_exe`).

---

## 8. How the UI stays in sync

**Server, `app/events.py` `EventBus.emit`.** It stamps each event with `type, run_id, t` (ms since run start) and a monotonically increasing **`seq`**. It then:
- appends the event to `data/runs/<id>/events.jsonl`
- pushes it to every SSE subscriber (slow subscribers are dropped, never blocking the pipeline)

`run.json` is also written atomically after every change.

**`GET /api/runs/{id}/events`** sends the **full history first, then live events**, with a `: ping` heartbeat. With `?replay=1&speed=4` it re-paces a finished run using the original `t` values; that's what powers **Watch sample run**.

**Browser, `handleEvent` → `REDUCERS[e.type]`.** There is one **idempotent reducer** per event type. Events at or below `ui.lastSeq` are skipped, so EventSource auto-reconnects and replays never duplicate tiles. Reducers mutate one store, `markDirty()` batches work into a `requestAnimationFrame`, and `renderAll` redraws each panel:
- **Model chips and pipeline rail:** `renderModelChips`, `renderRail`
- **Plan card:** `renderPlan`
- **Storyboard grid:** `renderStoryboard`
- **Motion lab:** `renderMotion`
- **Soundtrack:** `renderMusic`
- **Final player:** the final cut, with presentation mode
- **Localize panel and telemetry drawer**

**Metrics.** `Run.touch_metrics` recomputes at most twice a second:
- images generated, NB2 p50/p95, images/min (from the union of busy intervals, so it's honest under concurrency)
- judge calls, repair rounds
- Omni clips and edits, music versions
- in-flight counts per modality (live from `GenMedia`)
- time to first image / first clip / final
- which API path worked per model

---

## 9. After the first cut: directing the ad

Every action returns `{"ok": true}` immediately, and progress arrives as events.

| UI action | Endpoint | Code path |
|---|---|---|
| Click a different tile | `POST …/scenes/{sid}/select` | `action_select`: bumps `render_token` → `set_winner(by="user")` → `render_clip` (a fresh Omni render from that keyframe) |
| ↻ Regenerate a scene (+ instruction) | `…/regenerate` | `action_regenerate`: new NB2 round → judge → (repair) → winner → `render_clip` |
| Chat on a clip ("golden hour light") | `…/edit` | `edit_clip`: **Omni edit** and **`interpret_clip_edit` (Flash)** run *in parallel*. If Flash says `mood_changed`, then `update_mood` → **Lyria re-score** ("scene s2 → moody"). |
| **Direct the whole ad** (one sentence) | `POST …/direct` | `_direct` → `plan_direction` (Flash) returns a DirectionPlan (per-scene Omni instructions, mood updates, optional keyframe restyle, music instruction). **All clip edits and the re-score run concurrently.** With `restyle_keyframes`, each scene is first NB2-edited, then re-rendered. |
| Re-score | `POST …/music` | `render_music(reason="re-score…")` |
| Localize | `POST …/localize` | per market, in parallel: `localize_plan` (Flash: language, tagline, CTA, per-scene NB2 instructions, music style) → an NB2 edit of **every winning keyframe** in parallel (base image first, composition kept) + a regional Lyria variant. **Keyframes and music only, no localized video.** |
| Force re-stitch | `POST …/final` | `request_stitch(force=True)` |

**How Omni edits chain (`GenMedia.edit_video`).** Each clip version stores the `interaction_id` it came from. An edit tries three things in turn:
1. **Multi-turn:** `interactions.create(previous_interaction_id=…, input=instruction, task="edit")`. The model remembers the previous take, so "now add rain" builds on "golden hour".
2. **Single-turn:** the prior clip's bytes plus the instruction.
3. **Re-render:** image-to-video from the keyframe with the instruction merged in (`meta.fallback = "rerender"`).

The version pill shows which one happened.

---

## 10. The model adapter: `app/genai_client.py`

Every modality is one async method returning a `GenResult(data, mime_type, latency_ms, model, api_path, text, meta)`. The execution engine (`_execute`) gives each method:

- **Ordered API paths with memory:**
  - text/JSON and images: `generate_content` → Interactions
  - video: Interactions → `generate_videos`
  - music: Interactions

  The first path that works is remembered per model. `ADLOOP_<ROLE>_PATH` pins it.
- **Shape ladder:** on 400/422 or an empty response, the same path is retried with progressively smaller requests (dropping `image_size`, `resolution`, `duration`, JSON schema, thinking config…). The working level is remembered.
- **Backoff:** 429/5xx and transport errors are retried at 0.8 / 1.6 / 3.2s. **Safety refusals are final**, never retried.
- **Concurrency caps:** a semaphore per modality (`ADLOOP_IMAGE_CONCURRENCY=8`, `VIDEO=4`, `TEXT=6`, `MUSIC=3`) and live in-flight counters, which feed the pulsing model chips.
- **Downloads:** URI-delivered media is fetched with the SDK Files API or an authenticated GET.
- **UI-safe errors:** `"<model> via <path>: <reason>"`, with keys redacted.
- **Mock mode** (`ADLOOP_MOCK=1` or no key): each method sleeps a realistic jittered time and returns synthetic assets from `app/mock.py`:
  - Pillow gradient keyframes with the scene title and variant number
  - Ken Burns MP4s via `media.ken_burns`
  - chord-progression WAVs for music
  - brief-keyed fake plans, judgements and edit plans

  `ADLOOP_MOCK_SPEED` scales the delays. Because this lives inside the adapter, **the pipeline, API and UI are identical in both modes**.

`scripts/smoke_test.py` probes each model through the adapter, or with `--raw` directly via the SDK. `scripts/bench_nb2.py` measures NB2 burst latency and throughput for the README numbers.

---

## 11. Persistence, restart, replay

- **On disk:** everything for a run lives in `data/runs/<id>/`: `run.json`, `events.jsonl` and every asset (`anchor.png`, `s1_r0_v0.png`, `s1_clip_v2.mp4`, `music_v1.mp3`, `final_v3.mp4`, `loc_<market>_s1.png`…), served by `GET /media/{run_id}/{file}` (path-traversal safe).
- **Restart:** on startup, `main.lifespan` → `RunManager.load_existing()` reloads every `run.json` plus its event history, so past runs can be browsed and replayed after a restart.
- **Showcase:** `GET /api/showcase` returns `ADLOOP_SHOWCASE_RUN`, or the newest finished run, for the **Watch sample run** button.

---

## 12. Docs vs code: gaps

Before submitting, either implement these or remove them from `README.md` / `WRITEUP.md`. Judges may read the repo and run it.

| Claimed in README/WRITEUP | Reality in code |
|---|---|
| Flash TTS voiceover per scene, narrator voice, "re-voice" | Not implemented: no `generate_speech`, no voiceover state or events, no route |
| Narration on scene timecodes, music ducking, WebVTT captions | `media.stitch` takes clips + music only |
| Slide-by-slide *narrated* presentation | Presentation mode exists, but has no voiceover |
| Localized **narrated animatic MP4** per market | Localization = NB2 keyframes + a regional Lyria track only |
| "A failed Omni render falls back to a Ken Burns move" | Only in **mock** mode. Live, a failed render leaves that scene out of the cut |
| "Every method tries the Interactions API first" | Text and image try `generate_content` first |
| Measured-performance table | Still has `[[placeholders]]`. Run `bench_nb2.py` and a live run to fill it |
