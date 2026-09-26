# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout note

Each branch of this repo is a separate hackathon project. This file describes **`adloop`**: a Python/FastAPI GenMedia ad studio for Problem Statement 3. Other branches are unrelated:
- `headshoot-app`: CastReel, zero-dependency Node
- `truthLens-PB1`: TruthLens, zero-dependency Node
- `master`: docs only

Check the branch before assuming which codebase you're in.

## Commands

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                                   # set GEMINI_API_KEY; every other var is optional and documented there
.venv/bin/uvicorn app.main:app --port 8000 --reload    # live if a key is set
ADLOOP_MOCK=1 .venv/bin/uvicorn app.main:app --port 8000   # fully offline, synthetic assets
```

There is no pytest suite and no linter config. The test is the end-to-end mock-mode check, which drives the real HTTP API and SSE stream and asserts the contract:

```bash
.venv/bin/python scripts/e2e_mock.py --inprocess --data-dir /tmp/adloop_e2e --mock-speed 0.3   # no TCP port needed
.venv/bin/python scripts/e2e_mock.py --base http://localhost:8000 --skip-rate-limit          # against a running ADLOOP_MOCK=1 server
```

Live model checks (need a key and network):

```bash
.venv/bin/python scripts/smoke_test.py                           # every model through the adapter, in pipeline order
.venv/bin/python scripts/smoke_test.py --only video,edit --raw   # steps: text,image,music,video,edit,transcribe; --raw bypasses GenMedia and prints raw SDK responses
.venv/bin/python scripts/bench_nb2.py -n 32 -c 16 --ref          # NB2 burst benchmark -> data/bench/*.csv
```

ffmpeg comes from `PATH` if present, otherwise from the binary bundled with `imageio-ffmpeg`. The Docker image (`Dockerfile`, python:3.11-slim) targets HF Spaces, Cloud Run and Render; see `deploy/DEPLOY.md`.

## Architecture

**`CONTRACT.md` is the interface spec.** It defines, with exact keys:
- the JSON shapes: Plan, Judgement, DirectionPlan, LocalizePlan
- the run-state JSON returned by `GET /api/runs/{id}`
- every SSE event type and its payload
- the HTTP API

The pipeline, the frontend and `scripts/e2e_mock.py` all depend on those keys. If you change a shape, update `CONTRACT.md` and every consumer together. Section 9 (the TTS voiceover addendum) is **specified but not implemented**: there is no speech generation, voiceover state, `/voiceover` route, captions or narrated presentation in the code, although `README.md` and `WRITEUP.md` describe them as if they exist. Only `config.py` defines `model_tts`/`tts_concurrency`. The file also assigns module ownership (A: models, B: orchestration, C: frontend, D: ship), which was used to split work between parallel builders.

The layers, strictly one-directional:

- **`app/genai_client.py` (`GenMedia`) is the only module that talks to Google.** Model IDs are unverified previews, so every method:
  - tries an ordered list of API paths: text/JSON and images use `generate_content` → Interactions API; video uses Interactions API → `generate_videos`; music uses the Interactions API
  - on HTTP 400 or an empty response, walks a "shape ladder" of progressively smaller requests (dropping optional fields); the level that worked is remembered too
  - retries 429/5xx with backoff (0.8/1.6/3.2s); safety refusals are final and never retried
  - video edits try: multi-turn (`previous_interaction_id`) → single-turn with the prior clip bytes → re-render from the keyframe
  - **remembers which API path worked per model**; `ADLOOP_<ROLE>_PATH` pins it
  - caps concurrency with per-modality semaphores

  **Mock mode is handled inside `GenMedia`**, which returns synthetic assets built by `app/mock.py`. Everything above this layer is mode-agnostic, so never branch on mock mode in the pipeline.
- **`app/prompts.py`**: the creative functions (`plan_campaign`, `judge_scene`, `plan_direction`, `interpret_clip_edit`, `localize_plan`, and the prompt builders). They call `GenMedia` and never touch the SDK directly.
- **`app/pipeline.py`** (`Run`, `RunManager`): the scheduler.
  - The plan immediately forks music v1 and the continuity anchor.
  - Each scene then runs as an **independent chain with no global barrier**: NB2 variants → judge → optional repair round → Omni clip, submitted the moment that scene's winner exists.
  - Superseded work is dropped via per-scene `render_token`s.
  - The final cut is a debounced, single-flight stitch loop (`request_stitch`), re-run after any later change.
  - User actions (`action_*`) return immediately. Background work goes through `Run.spawn`, which turns exceptions into `error`/`log` events instead of crashing.
  - The first cut waits only for every scene to *settle* (clip done or failed) and the music to settle, so a failed Omni render just leaves that scene out. Ken Burns clips are only produced in mock mode, despite the README.
- **`app/events.py`**: event sourcing. Every state change is an event stamped with `t` and a monotonically increasing `seq`, appended to `data/runs/<id>/events.jsonl` and fanned out over SSE: full history first, then live. `?replay=1` re-paces a finished run. `main.py` reloads existing `run.json` files on startup.
- **`app/media.py`**: ffmpeg helpers: Ken Burns (mock video), clip normalisation, crossfade stitch with the soundtrack fitted to the cut length.
- **`app/main.py`**: FastAPI routes, per-IP rate limiting, concurrent-run cap, path-traversal-safe `/media/{run_id}/{file}`.
- **`static/`**: vanilla ES-module UI, no build step. It rebuilds from `GET /api/runs/{id}`, then applies SSE events incrementally, deduplicating by `seq` (`ui.lastSeq`) so reconnects and replays never duplicate tiles. `#run=<id>` restores a run on refresh.

## Working notes

- Mock mode (`ADLOOP_MOCK=1`, or no key) must keep **every** feature working offline. Outbound network to Google has been blocked on dev machines, so verify changes in mock mode (`e2e_mock.py --inprocess`). `ADLOOP_MOCK_SPEED` scales the simulated latencies.
- The repo is public. The API key lives only in the git-ignored `.env`; `.dockerignore` also keeps it out of images.
- Run data and generated assets go to `data/` (git-ignored).
