"""AdLoop HTTP server (FastAPI): studio page, media files, run API, SSE event streams.

Endpoints follow CONTRACT §7. Design notes:

* **Non-blocking actions** -- every mutating endpoint validates its input, schedules background work on the
  :class:`~app.pipeline.Run` and returns ``{"ok": true}`` immediately; progress is streamed as events.
* **SSE** -- ``/api/runs/{id}/events`` sends the full history first, then live events, with a ``: ping``
  heartbeat every 15s. ``replay=1&speed=N`` re-emits a finished run's history paced by the original ``t``
  stamps (gaps capped so dead time never stalls a demo) and then ends the stream.
* **Media** -- ``/media/{run_id}/{file}`` is path-traversal safe (strict name patterns + resolved-path check)
  and served by Starlette's ``FileResponse``, which implements HTTP Range requests so ``<video>`` can seek.
* **Abuse protection** -- per-IP runs/hour and a global cap on concurrently active runs, both from settings,
  answered with ``429 {"error": ...}``.
* **Errors** -- always JSON ``{"error": "msg"}`` with a proper status code.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import media
from app.config import settings
from app.events import CLOSED, sse_format
from app.genai_client import GenAIError, GenMedia
from app.pipeline import MAX_INSTRUCTION_CHARS, RunManager, valid_run_id

log = logging.getLogger("adloop.main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

#: SSE keep-alive interval (seconds).
HEARTBEAT_S = 15.0
#: Longest pause between two replayed events, after dividing by ``speed`` (seconds).
REPLAY_MAX_GAP_S = 2.5
MAX_BRIEF_CHARS = 4000
MAX_BRAND_CHARS = 120
MAX_MARKETS = 6
MAX_MARKET_CHARS = 60
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024

_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCENE_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
#: Run-folder bookkeeping files that live next to the assets but are never served as media.
_PRIVATE_FILES = frozenset({"run.json", "events.jsonl"})

for _ext, _mime in ((".mp4", "video/mp4"), (".webm", "video/webm"), (".mp3", "audio/mpeg"), (".wav", "audio/wav"),
                    (".m4a", "audio/mp4"), (".webp", "image/webp"), (".js", "text/javascript"),
                    (".css", "text/css"), (".jsonl", "application/x-ndjson")):
    mimetypes.add_type(_mime, _ext)


# ------------------------------------------------------------------------------------ app lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the GenMedia client + RunManager, load persisted runs, and clean up on shutdown."""
    gm = GenMedia(settings)
    manager = RunManager(settings, gm)
    manager.load_existing()
    app.state.gm = gm
    app.state.manager = manager
    log.info("AdLoop ready: %r", settings)
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(title="AdLoop", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
settings.static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


def err(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return err(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    first = (exc.errors() or [{}])[0]
    where = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query"))
    return err(422, f"invalid input{(' for ' + where) if where else ''}: {first.get('msg', 'bad request')}")


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return err(500, "internal server error")


def _manager(request: Request) -> RunManager:
    return request.app.state.manager


def _run_or_404(request: Request, run_id: str):
    run = _manager(request).get(run_id)
    if run is None:
        raise StarletteHTTPException(404, "run not found")
    return run


# ------------------------------------------------------------------------------------ rate limiting
class RateLimiter:
    """Sliding one-hour window of run creations per client IP (0 = unlimited)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check_and_record(self, ip: str, limit: int) -> bool:
        if limit <= 0:
            return True
        now = time.time()
        q = self._hits[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    """Client IP used for per-visitor rate limiting.

    Deliberately ``request.client.host`` rather than a raw ``X-Forwarded-For`` header: the leftmost XFF hop is
    client-controlled and trivially spoofable. Behind a proxy (HF Spaces / Cloud Run / Render) uvicorn runs with
    ``--proxy-headers`` (see Dockerfile), which rewrites ``client.host`` from the proxy's forwarding headers.
    """
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------------------------ pages & media
@app.get("/", include_in_schema=False)
async def index():
    page = settings.static_dir / "index.html"
    if page.exists():
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<!doctype html><title>AdLoop</title><h1>AdLoop</h1><p>The studio UI is not built yet. "
                        "The API is live at <a href='/api/health'>/api/health</a>.</p>")


@app.get("/media/{run_id}/{file}", include_in_schema=False)
async def media_file(run_id: str, file: str):
    """Serve a run asset. Strict names + resolved-path containment make traversal impossible."""
    if not valid_run_id(run_id) or not _FILE_RE.match(file) or ".." in file:
        return err(404, "not found")
    base = (settings.data_dir / run_id).resolve()
    path = (base / file).resolve()
    if path.parent != base or not path.is_file() or file in _PRIVATE_FILES:
        return err(404, "not found")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # Versioned asset names never change content, so they can be cached aggressively.
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})


# ------------------------------------------------------------------------------------ meta endpoints
@app.get("/api/health")
async def health(request: Request):
    gm: GenMedia = request.app.state.gm
    return {"ok": True, "mode": settings.mode, "models": settings.models(), "ffmpeg": media.ffmpeg_available(),
            "genai": gm.stats(), "active_runs": _manager(request).active_count(),
            "limits": {"max_concurrent_runs": settings.max_concurrent_runs,
                       "runs_per_ip_per_hour": settings.runs_per_ip_per_hour}}


@app.get("/api/showcase")
async def showcase(request: Request):
    return {"run_id": _manager(request).showcase_id()}


@app.post("/api/transcribe")
async def transcribe(request: Request, audio: UploadFile = File(...)):
    data = await audio.read()
    if not data:
        return err(400, "empty audio upload")
    if len(data) > MAX_AUDIO_BYTES:
        return err(413, "audio too large (max 20 MB)")
    mime = (audio.content_type or "audio/webm").split(";")[0].strip() or "audio/webm"
    if not (mime.startswith("audio/") or mime.startswith("video/") or mime == "application/octet-stream"):
        return err(415, f"unsupported audio type {mime}")
    try:
        res = await request.app.state.gm.transcribe(data, mime)
    except GenAIError as exc:
        return err(502, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("transcription failed")
        return err(502, f"transcription failed: {exc.__class__.__name__}")
    return {"text": (res.text or "").strip(), "latency_ms": res.latency_ms, "model": res.model,
            "api_path": res.api_path}


# ------------------------------------------------------------------------------------ runs
def _sniff_image(data: bytes) -> str | None:
    head = data[:16]
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.post("/api/runs")
async def create_run(request: Request, brief: str = Form(...), brand: str = Form(""), aspect: str = Form("16:9"),
                     n_scenes: int = Form(4), variants: int = Form(4), markets: str = Form(""),
                     product_image: UploadFile | None = File(None)):
    brief = brief.strip()
    if not brief:
        return err(400, "brief is required")
    if len(brief) > MAX_BRIEF_CHARS:
        return err(400, f"brief too long (max {MAX_BRIEF_CHARS} characters)")
    if aspect not in ("16:9", "9:16"):
        return err(400, "aspect must be 16:9 or 9:16")
    if not 3 <= n_scenes <= 6:
        return err(400, "n_scenes must be between 3 and 6")
    if not 2 <= variants <= 6:
        return err(400, "variants must be between 2 and 6")
    market_list = [m.strip()[:MAX_MARKET_CHARS] for m in markets.split(",") if m.strip()][:MAX_MARKETS]

    image: bytes | None = None
    image_mime: str | None = None
    if product_image is not None and product_image.filename:
        image = await product_image.read()
        if image:
            if len(image) > MAX_IMAGE_BYTES:
                return err(413, "product image too large (max 12 MB)")
            image_mime = _sniff_image(image)
            if image_mime is None:
                return err(415, "product image must be PNG, JPEG or WebP")
        else:
            image = None

    manager = _manager(request)
    if manager.active_count() >= settings.max_concurrent_runs:
        return err(429, f"the studio is busy ({settings.max_concurrent_runs} runs in progress) — try again in a "
                        f"minute")
    if not limiter.check_and_record(client_ip(request), settings.runs_per_ip_per_hour):
        return err(429, f"rate limit: at most {settings.runs_per_ip_per_hour} runs per hour per visitor")
    run = manager.create_run(brief=brief, brand=brand.strip()[:MAX_BRAND_CHARS], aspect=aspect, n_scenes=n_scenes,
                             variants=variants, markets=market_list, product_image=image, product_mime=image_mime)
    return {"run_id": run.id}


@app.get("/api/runs")
async def list_runs(request: Request):
    return _manager(request).list_runs(20)


@app.get("/api/runs/{run_id}")
async def get_run(request: Request, run_id: str):
    return _run_or_404(request, run_id).public_state()


# ------------------------------------------------------------------------------------ SSE
@app.get("/api/runs/{run_id}/events")
async def run_events(request: Request, run_id: str, replay: int = 0, speed: float = 4.0):
    run = _run_or_404(request, run_id)
    speed = min(100.0, max(0.25, speed))
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}

    async def replay_stream() -> AsyncIterator[str]:
        """Re-emit the recorded history paced by the original ``t`` stamps, then end."""
        history = list(run.bus.history)
        yield sse_format(comment="replay")
        prev_t = history[0].get("t", 0) if history else 0
        for ev in history:
            gap = max(0.0, (float(ev.get("t", prev_t)) - prev_t) / 1000.0 / speed)
            prev_t = float(ev.get("t", prev_t))
            if gap > 0:
                await asyncio.sleep(min(gap, REPLAY_MAX_GAP_S))
            if await request.is_disconnected():
                return
            yield sse_format(ev)
        yield sse_format(comment="replay-end")

    async def live_stream() -> AsyncIterator[str]:
        """History snapshot first, then live events from a bounded subscriber queue, with heartbeats."""
        sub, history = run.bus.subscribe()
        try:
            yield sse_format(comment="connected")
            for ev in history:
                yield sse_format(ev)
            while True:
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_S)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield sse_format(comment="ping")
                    continue
                if ev is CLOSED:  # dropped as a slow consumer or server shutting down -> client reconnects
                    return
                yield sse_format(ev)
        finally:
            run.bus.unsubscribe(sub)

    stream = replay_stream() if replay else live_stream()
    return StreamingResponse(stream, media_type="text/event-stream", headers=headers)


# ------------------------------------------------------------------------------------ actions
class SelectBody(BaseModel):
    idx: int = Field(..., ge=0)


class InstructionBody(BaseModel):
    instruction: str | None = Field(None, max_length=MAX_INSTRUCTION_CHARS)


class RequiredInstructionBody(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=MAX_INSTRUCTION_CHARS)


class LocalizeBody(BaseModel):
    markets: list[str] = Field(..., min_length=1, max_length=MAX_MARKETS)


OK = {"ok": True}


def _scene_or_error(run, scene_id: str) -> JSONResponse | None:
    """Validate that a run has a plan and the scene exists; return an error response or None."""
    if run.plan is None:
        return err(409, "the creative plan is not ready yet")
    if not _SCENE_RE.match(scene_id) or run.scene(scene_id) is None:
        return err(404, f"scene {scene_id} not found")
    return None


def _clean(text: str | None) -> str | None:
    text = (text or "").strip()
    return text or None


@app.post("/api/runs/{run_id}/scenes/{scene_id}/select")
async def select_variant(request: Request, run_id: str, scene_id: str, body: SelectBody):
    run = _run_or_404(request, run_id)
    if (bad := _scene_or_error(run, scene_id)) is not None:
        return bad
    if not any(v["idx"] == body.idx for v in run.scene(scene_id)["variants"]):
        return err(400, f"variant {body.idx} does not exist for {scene_id}")
    run.action_select(scene_id, body.idx)
    return OK


@app.post("/api/runs/{run_id}/scenes/{scene_id}/regenerate")
async def regenerate_scene(request: Request, run_id: str, scene_id: str, body: InstructionBody | None = None):
    run = _run_or_404(request, run_id)
    if (bad := _scene_or_error(run, scene_id)) is not None:
        return bad
    run.action_regenerate(scene_id, _clean(body.instruction if body else None))
    return OK


@app.post("/api/runs/{run_id}/scenes/{scene_id}/edit")
async def edit_scene(request: Request, run_id: str, scene_id: str, body: RequiredInstructionBody):
    run = _run_or_404(request, run_id)
    if (bad := _scene_or_error(run, scene_id)) is not None:
        return bad
    instruction = _clean(body.instruction)
    if not instruction:
        return err(400, "instruction is required")
    run.action_edit(scene_id, instruction)
    return OK


@app.post("/api/runs/{run_id}/direct")
async def direct_run(request: Request, run_id: str, body: RequiredInstructionBody):
    run = _run_or_404(request, run_id)
    if run.plan is None:
        return err(409, "the creative plan is not ready yet")
    instruction = _clean(body.instruction)
    if not instruction:
        return err(400, "instruction is required")
    run.action_direct(instruction)
    return OK


@app.post("/api/runs/{run_id}/music")
async def rescore(request: Request, run_id: str, body: InstructionBody | None = None):
    run = _run_or_404(request, run_id)
    if run.plan is None:
        return err(409, "the creative plan is not ready yet")
    run.action_music(_clean(body.instruction if body else None))
    return OK


@app.post("/api/runs/{run_id}/localize")
async def localize(request: Request, run_id: str, body: LocalizeBody):
    run = _run_or_404(request, run_id)
    if run.plan is None:
        return err(409, "the creative plan is not ready yet")
    if not any(sc.get("winner") is not None for sc in run.state["scenes"]):
        return err(409, "no winning keyframes to localize yet")
    markets = list(dict.fromkeys(m.strip()[:MAX_MARKET_CHARS] for m in body.markets if m and m.strip()))
    if not markets:
        return err(400, "at least one market is required")
    run.action_localize(markets)
    return OK


@app.post("/api/runs/{run_id}/final")
async def force_final(request: Request, run_id: str):
    run = _run_or_404(request, run_id)
    if not any(sc["clip"].get("current") for sc in run.state["scenes"]):
        return err(409, "no clips rendered yet")
    run.action_final()
    return OK

