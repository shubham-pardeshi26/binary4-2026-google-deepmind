# AdLoop — Build Contract (single source of truth for all builders)

**Product:** AdLoop — a one-loop GenMedia ad studio (Kaggle GDM Hyderabad Hackathon, Problem Statement 3).
Brief (voice or text) → Gemini 3.8 Flash creative director → **Nano Banana 2 Lite** storyboard fan-out
(N scenes × K variants, parallel, continuity-anchored) → Flash **vision judge tournament** with self-repair
→ **Gemini Omni Flash** image-to-video per winning keyframe (pipelined, starts the instant a scene's winner is
picked) → multi-turn **conversational video editing** (per-clip chat + one-sentence "Direct the whole ad"
that fans out to every modality) → **Lyria 3.5** adaptive soundtrack (starts right after the plan, re-scores
when edits change mood) → ffmpeg final cut → presentation mode → NB2 localization fan-out to markets.

Speed and cross-modal continuity must be *visible*: every asset shows its latency, a live telemetry panel
shows throughput / p50 / p95 / in-flight counts, and the pipeline is **pipelined, not barriered**.

Project root: `/Users/anishvijayvergiya/Desktop/adloop` · Python 3.11 venv at `.venv` (all deps installed:
fastapi, uvicorn[standard], google-genai==2.25.0, pillow, imageio-ffmpeg, python-multipart, python-dotenv, httpx).
Run: `.venv/bin/uvicorn app.main:app --port 8000 --reload`. **Mock mode** (`ADLOOP_MOCK=1` or no API key)
must make the entire app work offline with synthetic assets — builders test in mock mode.
NOTE: outbound network to Google is currently blocked on the dev laptop, so ALL local testing is mock mode.

## 0. Ownership (do not write files you don't own)

| Owner | Files |
|---|---|
| A (models) | `app/__init__.py`, `app/config.py`, `app/genai_client.py`, `app/prompts.py`, `app/mock.py` |
| B (orchestration) | `app/events.py`, `app/media.py`, `app/pipeline.py`, `app/main.py` |
| C (frontend) | `static/index.html`, `static/app.js`, `static/styles.css` |
| D (ship) | `README.md`, `WRITEUP.md`, `Dockerfile`, `requirements.txt`, `.env.example`, `.dockerignore`, `deploy/*`, `scripts/smoke_test.py` |

Never write the API key into any file except the existing gitignored `.env`. The repo will be PUBLIC.

## 1. Models (env-overridable; defaults below)

| Role | Env var | Default id |
|---|---|---|
| Creative director / judge / edit interpreter | `ADLOOP_MODEL_TEXT` | `gemini-3.8-flash` |
| Storyboard + localization images (**focus**) | `ADLOOP_MODEL_IMAGE` | `gemini-3.1-flash-lite-image` |
| Image-to-video + conversational edit (**focus**) | `ADLOOP_MODEL_VIDEO` | `gemini-omni-1.1-flash` |
| Soundtrack (**focus**) | `ADLOOP_MODEL_MUSIC` | `lyria-3.5` |
| Voice brief transcription | `ADLOOP_MODEL_TRANSCRIBE` | `gemini-3.5-transcribe` |

API key: `GEMINI_API_KEY` (fallback `GOOGLE_API_KEY`), loaded from `.env` via python-dotenv.

### 1a. Verified SDK surface (google-genai 2.25.0 — read `.venv/lib/python3.11/site-packages/google/genai/` to confirm details)
- `client = genai.Client(api_key=...)`; async via `client.aio.*`.
- `client.aio.models.generate_content(model, contents, config=types.GenerateContentConfig(...))` — config has
  `response_modalities`, `image_config=types.ImageConfig(aspect_ratio, image_size)`, `response_mime_type`,
  `response_json_schema`, `system_instruction`, `temperature`, `thinking_config`, `audio_transcription_config`.
  Image bytes come back in `resp.candidates[0].content.parts[i].inline_data.{data,mime_type}`.
- **Interactions API** (the path newer GenMedia models use; Lyria 3 models are listed there):
  `await client.aio.interactions.create(model=..., input=..., response_modalities=[...], response_format={...},
  generation_config={...}, previous_interaction_id=..., background=..., store=..., system_instruction=...)`
  and `await client.aio.interactions.get(id)` (check the exact get() signature in `_gaos/interactions.py`).
  - `input`: `str` | content dict | list of content dicts. Content dicts:
    `{"type":"text","text":...}`, `{"type":"image","data":<b64 str>,"mime_type":"image/png"}`,
    `{"type":"audio","data":<b64>,"mime_type":"audio/webm"}`, `{"type":"video","data":<b64>|"uri":...,"mime_type":"video/mp4"}`.
  - `response_modalities`: any of `"text","image","audio","video","document"`.
  - `response_format` dicts: `{"type":"image","aspect_ratio":"16:9"|"9:16"|...,"image_size":"512"|"1K"|"2K"|"4K","delivery":"inline"}`,
    `{"type":"video","aspect_ratio":"16:9"|"9:16","resolution":"360p"|"720p"|"1080p","duration":"<str>","delivery":"inline"|"uri"}`,
    `{"type":"audio","mime_type":"audio/mp3"|"audio/wav","delivery":"inline"}`.
  - `generation_config`: `{"video_config":{"task":"text_to_video"|"image_to_video"|"reference_to_video"|"edit"|"extend"},
    "transcription_config":{"language_codes":[...],"mode":"verbatim"|"smart"}, "thinking_level":..., "seed":...}`.
  - Result `Interaction` has `.id`, `.status` (`queued|in_progress|completed|failed|cancelled|incomplete|requires_action|budget_exceeded`),
    `.errors`, `.steps`, and SDK convenience props `.output_text`, `.output_image`, `.output_audio`, `.output_video`
    (content objects with `.data` (base64 str) and/or `.uri`, `.mime_type`). URIs may need the API key header
    `x-goog-api-key` to download.
  - Multi-turn video editing = new `create(..., previous_interaction_id=<prior id>, input=<instruction>,
    generation_config={"video_config":{"task":"edit"}})` (use `store=True` on the first turn).
- Legacy fallbacks exist: `client.aio.models.generate_videos(model, source=types.GenerateVideosSource(prompt, image, video), config=types.GenerateVideosConfig(aspect_ratio, duration_seconds, ...))`
  returning an operation polled via `client.aio.operations.get(op)`.

**Because these model ids are new and unverified, every adapter must try a primary path and fall back
automatically** (and remember which path worked per model, so later calls go straight there). On HTTP 400
INVALID_ARGUMENT, retry once with a minimal request (drop optional fields like `duration`, `resolution`,
`image_size`). Retry 429/500/503 with exponential backoff (0.8s, 1.6s, 3.2s; max 3). Errors surfaced to the
UI must be short, human-readable, and include the model id + api path.

## 2. `app/config.py` (A)
```python
class Settings:  # plain class or dataclass, instantiated once as `settings`
    api_key: str | None
    mock: bool                      # ADLOOP_MOCK=1 or no api key
    model_text, model_image, model_video, model_music, model_transcribe: str
    image_concurrency: int = 8      # ADLOOP_IMAGE_CONCURRENCY
    video_concurrency: int = 4      # ADLOOP_VIDEO_CONCURRENCY
    text_concurrency: int = 6       # ADLOOP_TEXT_CONCURRENCY
    video_resolution: str = "720p"  # ADLOOP_VIDEO_RESOLUTION
    video_seconds: int = 6          # ADLOOP_VIDEO_SECONDS (default clip duration)
    video_poll_seconds: float = 3.0
    video_timeout_seconds: int = 420
    judge_threshold: float = 7.0    # repair round triggered if best overall < threshold
    max_repair_rounds: int = 1
    max_concurrent_runs: int = 3    # ADLOOP_MAX_CONCURRENT_RUNS
    runs_per_ip_per_hour: int = 6   # ADLOOP_RUNS_PER_IP_PER_HOUR (0 = unlimited)
    data_dir: Path = ROOT / "data" / "runs"
    static_dir: Path = ROOT / "static"
settings = Settings()
```

## 3. `app/genai_client.py` (A) — the ONLY module that talks to Google
```python
@dataclass
class GenResult:
    data: bytes | None          # media bytes (None for text-only)
    mime_type: str              # "image/png" | "image/jpeg" | "video/mp4" | "audio/mpeg" | "audio/wav" | "text/plain" | "application/json"
    latency_ms: int
    model: str
    api_path: str               # "interactions" | "generate_content" | "generate_videos" | "mock"
    text: str | None = None
    meta: dict = field(default_factory=dict)   # video: {"interaction_id": str|None, "operation": str|None}

class GenAIError(Exception):   # message is UI-safe; attrs: model, api_path, status_code
    ...

class GenMedia:
    def __init__(self, settings): ...
    mock: bool
    async def transcribe(self, audio: bytes, mime_type: str) -> GenResult                 # .text
    async def generate_json(self, parts: list, schema: dict, *, system: str | None = None,
                            temperature: float = 0.8, model: str | None = None) -> tuple[dict, GenResult]
        # parts: list of str | bytes-image tuples ("image", bytes, mime) — A defines a tiny helper `img_part(bytes, mime)`
    async def generate_image(self, prompt: str, *, refs: list[bytes] = (), aspect: str = "16:9",
                             size: str = "1K", seed: int | None = None) -> GenResult
        # refs are continuity references (anchor, product photo, or the base image for an edit — base image FIRST)
    async def generate_video(self, prompt: str, *, image: bytes | None, aspect: str, seconds: int,
                             on_progress: Callable[[dict], Awaitable[None]] | None = None) -> GenResult
        # data = mp4 bytes, meta.interaction_id set when the interactions path was used
        # on_progress({"status": "queued|in_progress", "elapsed_ms": int}) called every poll
    async def edit_video(self, instruction: str, *, previous_interaction_id: str | None,
                         video: bytes | None, image: bytes | None, aspect: str, seconds: int,
                         on_progress=None) -> GenResult
        # Primary: interactions multi-turn (previous_interaction_id, task="edit").
        # Fallback A: interactions single-turn with the prior video bytes as input + instruction (task="edit").
        # Fallback B: re-render image_to_video from `image` with the instruction merged into the prompt (meta.fallback="rerender").
    async def generate_music(self, prompt: str, *, seconds: int) -> GenResult             # mp3 or wav bytes
    def stats(self) -> dict   # per-model: calls, errors, working api_path, p50/p95 latency
```
Semaphores per modality (image/video/text/music) live inside GenMedia. **Mock mode**: every method returns
deterministic synthetic assets after a realistic `asyncio.sleep` (image 0.6–1.8s jittered, judge 0.8s, video
6–12s with on_progress ticks, music 4s, transcribe 0.8s): images via Pillow (gradient in the plan palette + scene
title + variant number drawn big — visually distinct per variant), video via `app.media.ken_burns(image_bytes,
seconds, aspect) -> bytes` (B implements), music via pure-python `wave` synthesis (a pleasant chord progression,
44.1kHz mono WAV, `seconds` long), JSON via `app/mock.py` fake plan/judge/edit-plan builders keyed off the brief.

## 4. `app/prompts.py` (A) — high-level creative functions (use GenMedia; never touch the SDK directly)
```python
async def plan_campaign(gm, *, brief, brand, aspect, n_scenes, markets, product_image: bytes | None) -> tuple[Plan, GenResult]
async def judge_scene(gm, *, plan, scene, variants: list[bytes], anchor: bytes | None) -> tuple[Judgement, GenResult]
async def plan_direction(gm, *, plan, scenes_state: list[dict], instruction: str) -> tuple[DirectionPlan, GenResult]
async def interpret_clip_edit(gm, *, plan, scene, instruction) -> tuple[dict, GenResult]   # {"mood": str, "energy": float, "mood_changed": bool, "omni_instruction": str}
async def localize_plan(gm, *, plan, market) -> tuple[dict, GenResult]                     # see LocalizePlan
def scene_image_prompt(plan, scene, *, fix: str | None = None, instruction: str | None = None) -> str   # full NB2 prompt incl. brand bible + continuity rules
def scene_motion_prompt(plan, scene) -> str                                                               # full Omni prompt (camera, physics, mood, keep keyframe identity)
def music_prompt(plan, scenes: list[dict], *, total_seconds: int, reason: str | None = None, instruction: str | None = None) -> str
    # timed structure: "0:00–0:06 hook — <mood>, <energy>…", genre/bpm/key/instruments, "instrumental, no vocals", ends on a resolved sting for the CTA
```
### Plan JSON (exact keys — B and C depend on them)
```json
{
  "campaign_name": "str", "tagline": "str", "cta": "str",
  "brand": {"name": "str", "palette": ["#RRGGBB", "... 4-5 hex"], "visual_style": "str", "mood": "str",
            "typography": "str", "product": "str", "hero": "str"},
  "anchor_prompt": "str  (prompt for a clean hero/product continuity reference frame)",
  "scenes": [
    {"id": "s1", "title": "str", "beat": "hook|build|reveal|cta", "duration_s": 6,
     "image_prompt": "str", "motion_prompt": "str", "camera": "str", "mood": "str",
     "energy": 0.0, "on_screen_text": "str (short, may be empty)"}
  ],
  "music": {"genre": "str", "bpm": 100, "key": "str", "instruments": ["str"], "arc": "str"}
}
```
### Judgement JSON
```json
{"scores": [{"index": 0, "brief_fit": 0, "brand_consistency": 0, "composition": 0, "continuity": 0,
             "artifact_free": 0, "overall": 0.0, "notes": "str"}],
 "winner_index": 0, "rationale": "str (1-2 sentences)", "fix_instructions": "str (concrete fixes for the winner; empty if great)"}
```
All scores 0–10 (overall float, 1 decimal). `index` refers to the order of `variants` passed in.
### DirectionPlan JSON (one sentence → every modality)
```json
{"summary": "str", "scene_edits": [{"scene_id": "s1", "omni_instruction": "str"}],
 "restyle_keyframes": false, "music": {"rescore": true, "instruction": "str"},
 "mood_updates": [{"scene_id": "s1", "mood": "str", "energy": 0.0}]}
```
### LocalizePlan JSON
```json
{"market": "str", "language": "str", "tagline": "str", "cta": "str",
 "scene_edits": [{"scene_id": "s1", "nb2_instruction": "str (translate on-image text, adapt setting/cast/props culturally, KEEP composition & product)"}],
 "music_style": "str"}
```

## 5. `app/media.py` (B)
```python
def ffmpeg_exe() -> str          # shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
async def run_ffmpeg(args: list[str]) -> None   # asyncio subprocess, raise on nonzero with tail of stderr
async def ken_burns(image_bytes: bytes, seconds: int, aspect: str) -> bytes   # slow zoom mp4, h264, yuv420p, 30fps, 1280x720 or 720x1280
async def probe_duration(path: Path) -> float
async def stitch(clips: list[Path], music: Path | None, out: Path, *, aspect: str, fade_s: float = 0.35) -> float
    # normalize each clip to 1280x720 (or 720x1280) 30fps h264 (scale+pad), short crossfade or hard cut,
    # music trimmed/padded to total video length with 1.2s fade-out, music loudness normalized; drop clip audio unless
    # ADLOOP_KEEP_CLIP_AUDIO=1 (then mix at 0.35). +faststart. Returns duration seconds.
def save_bytes(run_dir: Path, name: str, data: bytes) -> Path
def ext_for_mime(mime: str) -> str
```

## 6. Run state + pipeline (B: `app/pipeline.py`, `app/events.py`)
Run folder: `data/runs/<run_id>/` (run_id = 8-char lowercase hex). Assets served at `/media/<run_id>/<file>`.
Persist `run.json` (state) + `events.jsonl` (every event) so finished runs can be replayed.

**Scheduling (pipelined — never a global barrier):**
1. `director` → plan. Immediately fork: (a) **music v1** (Lyria, from plan moods & planned durations) and
   (b) `anchor` (NB2 continuity frame; if a product photo was uploaded, pass it as ref).
2. After anchor: for **each scene independently** → K variants in parallel (`refs=[anchor]` (+product photo)),
   emit each `variant` the moment it lands → `judge` that scene as soon as its K variants are in → if best
   overall < `judge_threshold` and rounds left: **repair round** (2 new variants: NB2 edit of the winner with
   `fix_instructions`, base image first in refs) → re-judge (winner vs repairs) → `winner` → **immediately**
   submit Omni `generate_video` for that scene. So scene 1 may be rendering video while scene 4 is still judging.
3. When every clip has a done version AND music has a version → `final` stitch (auto). Any later change
   (new clip version, new music version, winner override) → auto re-stitch (debounced 1.5s, one at a time).
4. User actions (all non-blocking, return `{"ok": true}` immediately, progress via events):
   - `select` winner override → re-render that scene's clip.
   - `regenerate` scene (optional instruction) → new NB2 round for the scene → judge → re-render clip.
   - `edit` clip (instruction) → `interpret_clip_edit` + Omni `edit_video` in parallel → new clip version;
     if `mood_changed` → update scene mood → **adaptive music re-score** (reason = "scene s2 → moody").
   - `direct` (one sentence for the whole ad) → `plan_direction` → fan-out: all scene Omni edits in parallel
     + music re-score in parallel (+ optional NB2 restyle) → auto re-stitch. This is the headline demo moment.
   - `music` (optional instruction) → re-score.
   - `localize` markets → per market in parallel: `localize_plan` → NB2 edits of every winning keyframe in
     parallel (base image first) → Lyria regional variant. (No video for localized variants.)
5. Metrics recomputed and emitted (throttled ≤ 2/s) after every media event.

### Run state JSON (`GET /api/runs/{id}`) — exact keys
```json
{
 "id": "ab12cd34", "status": "running|done|error", "mode": "live|mock", "created_at": 1727340000.0,
 "input": {"brief": "", "brand": "", "aspect": "16:9", "n_scenes": 4, "variants": 4, "markets": [], "has_product_image": false},
 "plan": null,
 "anchor": {"url": "/media/ab12cd34/anchor.png", "latency_ms": 1400} ,
 "scenes": [{
    "id": "s1", "title": "", "beat": "hook", "mood": "", "energy": 0.5, "duration_s": 6,
    "variants": [{"idx": 0, "round": 0, "url": "/media/..", "latency_ms": 1234, "api_path": "generate_content", "score": null}],
    "judge": [{"round": 0, "winner_index": 0, "rationale": "", "fix_instructions": "", "scores": [], "latency_ms": 900}],
    "winner": 0,
    "clip": {"status": "idle|queued|rendering|done|error", "elapsed_ms": 0, "error": null, "current": 0,
             "versions": [{"v": 1, "url": "/media/..", "instruction": null, "latency_ms": 0, "api_path": "interactions", "interaction_id": null, "fallback": null}]}
 }],
 "music": {"status": "idle|rendering|done|error", "current": 0, "error": null,
           "versions": [{"v": 1, "url": "", "prompt": "", "latency_ms": 0, "reason": "initial"}]},
 "final": {"status": "idle|rendering|done|error", "url": null, "duration_s": null, "version": 0},
 "directions": [{"instruction": "", "summary": "", "ts": 0}],
 "localizations": {"<market>": {"status": "", "plan": {}, "scenes": [{"scene_id": "s1", "url": "", "latency_ms": 0}], "music_url": null}},
 "metrics": {}
}
```
`scenes[*].variants[*].idx` is the global index within that scene (0..), `winner` refers to it; judge `winner_index`
is mapped back to global idx by B before storing.

### Events (SSE `data: <json>\n\n`). Every event has `type`, `run_id`, `t` (ms since run start). Types & payloads:
| type | payload |
|---|---|
| `run_started` | `mode, input` |
| `stage` | `stage` ∈ director/anchor/storyboard/judge/motion/music/final/localize/direct, `status` ∈ start/done/error, `scene_id?`, `ms?`, `detail?` |
| `transcript` | `text` (only from direct-voice flows; optional) |
| `plan` | `plan` |
| `anchor` | `url, latency_ms` |
| `variant` | `scene_id, idx, round, url, latency_ms, api_path` |
| `variant_error` | `scene_id, idx, round, error` |
| `judge` | `scene_id, round, scores[] (with global idx in "idx"), winner_idx, rationale, fix_instructions, latency_ms` |
| `winner` | `scene_id, idx, url, by` ∈ judge/user |
| `clip_status` | `scene_id, status, elapsed_ms?, error?` |
| `clip` | `scene_id, v, url, instruction, latency_ms, api_path, interaction_id, fallback` |
| `scene_update` | `scene_id, mood, energy` |
| `music_status` | `status, reason?, error?` |
| `music` | `v, url, prompt, latency_ms, reason` |
| `final_status` | `status, error?` |
| `final` | `url, duration_s, version` |
| `direction` | `instruction, summary, plan (DirectionPlan)` |
| `localize_status` | `market, status, error?` |
| `localize_plan` | `market, plan` |
| `localize_image` | `market, scene_id, url, latency_ms` |
| `localize_music` | `market, url, latency_ms` |
| `metrics` | `metrics` (object below) |
| `log` | `level` ∈ info/warn/error, `msg` |
| `error` | `stage, msg` |
| `run_done` | `wall_ms` (emitted when the first final cut is done) |

Metrics object: `{"wall_ms", "images_generated", "image_p50_ms", "image_p95_ms", "images_per_min", "judge_calls",
"repair_rounds", "videos_generated", "video_p50_ms", "video_edits", "music_versions", "inflight": {"image": 0, "video": 0,
"music": 0, "text": 0}, "time_to_first_image_ms", "time_to_first_clip_ms", "time_to_final_ms", "api_paths": {"<model>": "<path>"}}`

## 7. HTTP API (B: `app/main.py`, FastAPI)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/` | – | `static/index.html` (static mounted at `/static`) |
| GET | `/media/{run_id}/{file}` | – | asset (path traversal safe) |
| GET | `/api/health` | – | `{"ok": true, "mode": "live|mock", "models": {...}, "ffmpeg": true, "genai": gm.stats()}` |
| POST | `/api/transcribe` | multipart `audio` (webm/ogg/wav/mp4) | `{"text", "latency_ms", "model", "api_path"}` |
| POST | `/api/runs` | multipart: `brief` (req), `brand`, `aspect` (16:9 \| 9:16), `n_scenes` (3–6, def 4), `variants` (2–6, def 4), `markets` (comma sep), `product_image` (file, optional) | `{"run_id"}` (429 JSON `{"error"}` if rate limited) |
| GET | `/api/runs` | – | `[{"id","campaign_name","status","created_at","thumb"}]` newest first (max 20) |
| GET | `/api/runs/{id}` | – | run state JSON |
| GET | `/api/runs/{id}/events` | query `replay=1&speed=4` optional | SSE: full history first, then live; heartbeat `: ping` every 15s. With `replay=1` re-emits history paced by original `t`/speed (for a finished run), then ends. |
| POST | `/api/runs/{id}/scenes/{scene_id}/select` | `{"idx": int}` | `{"ok": true}` |
| POST | `/api/runs/{id}/scenes/{scene_id}/regenerate` | `{"instruction": str?}` | `{"ok": true}` |
| POST | `/api/runs/{id}/scenes/{scene_id}/edit` | `{"instruction": str}` | `{"ok": true}` |
| POST | `/api/runs/{id}/direct` | `{"instruction": str}` | `{"ok": true}` |
| POST | `/api/runs/{id}/music` | `{"instruction": str?}` | `{"ok": true}` |
| POST | `/api/runs/{id}/localize` | `{"markets": [str]}` | `{"ok": true}` |
| POST | `/api/runs/{id}/final` | – | `{"ok": true}` (force re-stitch) |
| GET | `/api/showcase` | – | `{"run_id": str|null}` — env `ADLOOP_SHOWCASE_RUN` or the newest run with a final cut |

Errors: JSON `{"error": "msg"}` with proper status. On startup, load existing `run.json` files so finished runs
are browsable/replayable after restart. Background tasks must never crash the server; every exception becomes
an `error` + `log` event and a sensible `status`.

## 8. Frontend (C: vanilla JS, no build step)
Single page "studio", dark cinematic UI, zero framework (plain ES module `app.js`), fonts via Google Fonts link
(degrade gracefully offline). Must feel like a pro creative tool, not a form. Sections:
1. **Top bar**: AdLoop logo/wordmark, tagline "Brief → storyboard → film → score, in one loop", three model chips
   (Nano Banana 2 Lite · Omni Flash · Lyria 3.5) that pulse while that modality has in-flight work, LIVE/MOCK badge
   (from `/api/health`), "Watch sample run" (uses `/api/showcase` + `replay=1`).
2. **Brief panel**: large textarea, 🎙 mic button (MediaRecorder → `/api/transcribe` → fills textarea; shows
   recording timer + level meter), brand name, product photo drop zone (optional), aspect toggle 16:9/9:16,
   scenes (3–6) & variants-per-scene (2–6) steppers, markets chips (for localization later), 3 sample-brief chips
   (e.g. "Irani chai café launch in Hyderabad", "EV scooter for Gen-Z commuters", "Monsoon sneaker drop"), big
   "Launch loop" button.
3. **Pipeline rail**: Director → Storyboard (NB2) → Judge → Motion (Omni) → Score (Lyria) → Final cut, each node
   with live state + elapsed ms; a run clock.
4. **Creative plan card**: campaign name, tagline, palette swatches, style/mood, music brief.
5. **Storyboard fan-out**: one row per scene; K shimmer placeholders that fill in with images as `variant` events
   arrive, each tile showing latency badge (e.g. "1.4 s") and round; after `judge`: score badge on each tile,
   winner ring + crown, rationale text, repair-round tiles marked "repair"; click a tile → select override (POST
   select); "↻ regenerate" with optional instruction. Anchor frame shown at the row start ("continuity anchor").
6. **Motion lab**: one clip card per scene: `<video>` (muted loop autoplay), status (queued/rendering with live
   elapsed seconds/done/error), version pills v1 v2 … (click to view), per-clip chat input "Direct this shot…" with
   quick chips ("slower push-in", "golden hour light", "add gentle rain", "orbit the product") → POST edit.
   Chat history per clip (instruction → version).
7. **Director bar** (sticky bottom, appears once a plan exists): "Direct the whole ad in one sentence…" + mic →
   POST direct; shows the returned DirectionPlan summary as it fans out (which clips are being edited, music re-scoring).
8. **Soundtrack**: `<audio>` player, Lyria prompt (collapsible), version list with reason ("initial", "scene s2 → moody",
   "direction: monsoon evening"), mood timeline bar (segments per scene colored by energy), "Re-score" + instruction.
9. **Final cut**: big player of the stitched MP4 (+ download), version number, "▶ Present" button.
10. **Presentation mode** (fullscreen overlay, ←/→ keys, Esc): slide 1 title (campaign name, tagline, palette), slide 2
   storyboard winners w/ judge scores, slide 3 the film (autoplay with sound), slide 4 "How it was made" stats from
   metrics (images generated, p50 NB2 latency, time-to-first-image, judge calls, repair rounds, Omni clips & edits,
   Lyria versions, total wall time), slide 5 localized key visuals per market (if any).
11. **Localize panel**: market chips (e.g. "Hyderabad · Telugu", "Mumbai · Hindi", "Chennai · Tamil", "Tokyo · Japanese",
   "USA · English") → POST localize; grid market × scene of localized keyframes + per-market audio.
12. **Telemetry drawer** (right side, collapsible): live metrics tiles (images, p50/p95, images/min, in-flight bars per
   modality, TTFI, time-to-first-clip, time-to-final) + scrolling event log (monospace, color by type).
State: rebuild the whole UI from `GET /api/runs/{id}` on load/reconnect, then apply SSE events incrementally.
URL hash `#run=<id>` so a refresh restores the run. EventSource auto-reconnect must not duplicate tiles (dedupe by
scene_id+idx / v). Toasts for errors. Responsive down to 1280px wide; 9:16 aspect renders portrait tiles.

## 9. ADDENDUM (v2, user-requested) — Voiceover narration + UI fixes

### 9a. Model role `tts`
| Role | Env var | Default id |
|---|---|---|
| Scene-by-scene voiceover narration | `ADLOOP_MODEL_TTS` | `gemini-3.8-flash-tts` |
Settings gain `model_tts`, `tts_concurrency: int = 6` (`ADLOOP_TTS_CONCURRENCY`), and `models` dicts include `"tts"`.

`GenMedia.generate_speech(text: str, *, voice: str = "Kore", style: str | None = None, language: str | None = None) -> GenResult`
- returns **WAV bytes** (`audio/wav`). Gemini TTS returns raw PCM (`audio/L16;codec=pcm;rate=24000`, mono 16-bit) — wrap it in a WAV
  header (parse `rate=` from the mime; default 24000). If it returns wav/mp3 already, pass through (normalize mime).
- Primary: `generate_content(model_tts, contents="<style directive>: <text>", config=GenerateContentConfig(response_modalities=["AUDIO"],
  speech_config=SpeechConfig(voice_config=VoiceConfig(prebuilt_voice_config=PrebuiltVoiceConfig(voice_name=voice)))))` (+ language_code if the
  SDK SpeechConfig supports it). Fallback: interactions (`response_modalities=["audio"]`, `generation_config={"speech_config": ...}`) — verify
  field names in the SDK. Same retry/fallback/path-memory rules as other adapters; env override `ADLOOP_TTS_PATH`.
- Mock: speech-like WAV (syllable-rate amplitude envelope over a voiced formant-ish tone), length ≈ words / 2.6 s, 0.4–1.0 s latency.

### 9b. Plan additions (prompts.py / mock.py; normalize_plan must fill defaults)
- top-level `"voice": {"name": "<one of Kore|Puck|Charon|Fenrir|Aoede|Zephyr|Leda|Orus>", "style": "warm, confident, upbeat narrator"}`
- every scene gets `"voiceover": "str"` — narration for that scene. Together the lines tell a mini-story: hook (question/claim) →
  desire/problem → product reveal with 1–2 concrete benefits → CTA ending with the brand name + CTA. Word budget per scene:
  ≤ floor(duration_s × 2.4) words (6 s → ≤ 14 words). Spoken language: English unless the brief asks otherwise.
- DirectionPlan gains optional `"voiceover_updates": [{"scene_id": "s1", "text": "str"}]` and `"voice_style": "str|null"` (tone changes such as
  "make it playful" re-voice the affected scenes).
- LocalizePlan gains `"voiceover": [{"scene_id": "s1", "text": "str (in the market language & native script)"}]` and `"voice": "str"`.

### 9c. Pipeline (pipeline.py / media.py / main.py)
- Right after the plan, fork **per-scene TTS in parallel** (alongside music v1 and the anchor). Scene state gains
  `"voiceover": {"status": "idle|rendering|done|error", "v": 1, "text": "", "url": null, "latency_ms": 0, "duration_s": null, "error": null}`.
- Events: `voiceover_status` {scene_id, status, error?}; `voiceover` {scene_id, v, text, url, latency_ms, duration_s}.
- Final cut waits for clips + music + voiceovers (a voiceover error must NOT block the cut — cut without that line).
- `media.stitch(..., voiceovers: list[tuple[Path | None, float]] | None = None)`: each scene's VO placed at that scene's start offset
  (+0.25 s lead-in) via adelay; if a VO is longer than its scene − 0.3 s, speed it up with atempo (≤ 1.2×), else let it spill slightly;
  music **ducked under narration** (sidechaincompress keyed on the VO bus; fallback: music volume 0.3 when VO present); loudness-balanced so
  the narration is clearly intelligible. Also write `captions_v<N>.vtt` (WebVTT; one cue per scene VO line at its real time range).
  `final` event + state gain `captions_url`.
- User action: `POST /api/runs/{id}/scenes/{scene_id}/voiceover` body `{"text": str?, "voice": str?}` → re-voice that scene → re-stitch.
- `direct`: apply `voiceover_updates` / `voice_style` (re-voice those scenes in parallel with the Omni edits) → re-stitch.
- `localize`: per market also TTS every localized line (language hint) and build a **localized animatic MP4**: `ken_burns` of each
  localized keyframe for the scene duration → stitch with the localized music + localized VO → events `localize_voiceover`
  {market, scene_id, url, text, latency_ms} and `localize_video` {market, url, duration_s, captions_url}; state
  `localizations[m].voiceover = [{scene_id, url, text}]`, `.video_url`, `.captions_url`.
- Metrics gain `voiceovers_generated`, `tts_p50_ms`, and `inflight.tts`.

### 9d. Frontend (static/*)
- **Launch button bug (user-reported: "not showing properly")**: the absolutely positioned `.launch-glow` paints over the non-positioned label;
  the button may also be clipped/below the fold in the brief panel. Fix: label/kbd `position: relative; z-index: 1`, glow `pointer-events:none`,
  keep the button always visible (sticky footer of the brief panel), clear busy state ("Launching…" + spinner, disabled), strong contrast.
- **Telemetry drawer closed by default** (toggle stays visible with a tiny live activity dot; open/closed persisted in localStorage but default = closed).
- 4th, smaller model chip "Flash TTS" (the 3 focus chips stay primary). Pipeline rail gains a "Voice" node.
- Clip cards show the scene's voiceover line: editable text, ▶ preview of the VO audio, "↻ re-voice" → POST voiceover.
- Final player gets `<track kind="captions" default>` from `captions_url`.
- **Presentation mode = slide by slide**: title slide → **one slide per scene** (the scene's clip looping muted, full-bleed; its VO audio plays
  automatically when the slide opens; the VO line as a large caption; "Scene 1 · Hook" label) → the full film slide (with sound) → "How it was made"
  stats → localized animatics per market. "Auto-play" toggle advances when the slide's VO ends (+0.8 s).
- Localize panel shows each market's animatic video (with captions) + its VO lines.
