#!/usr/bin/env python3
"""AdMate end-to-end integration test (mock mode).

Drives the real HTTP API exactly like the studio UI does -- multipart run creation, SSE consumption, every user
action -- and asserts the CONTRACT (§6 run state + events, §7 endpoints) holds end to end:

* **Initial loop**: ``run_started -> plan -> anchor -> variant x (scenes*variants) -> judge / winner / clip per
  scene -> music -> final -> run_done`` with per-scene causal ordering (winner after that scene's judge, clip
  after that scene's winner, final after every clip + music).
* **State shape**: ``GET /api/runs/{id}`` keys against CONTRACT §6 (plan, scenes, variants, judge, clip
  versions, music, final, localizations, metrics).
* **Media**: every URL in the state returns 200 with the right content-type; Range requests return 206;
  path traversal is refused; the final MP4's duration matches the clips it was cut from (ffmpeg parse).
* **Narration (§9)**: one ``voiceover`` per scene before the first cut; WebVTT captions served as ``text/vtt`` with
  one cue per line; the narration is audible in the cut (``volumedetect`` on a line's window).
* **Actions**: edit (clip v2 + adaptive re-score + re-stitch), direct (fan-out edits + music + re-voice +
  re-stitch), re-voice one scene, music, select (re-render), regenerate (new round -> judge -> clip), localize
  (2 markets -> image + narration per scene + music + localized animatic), forced final; plus input-validation
  errors (400/404/409/422 JSON).
* **Degradation** (``--inprocess`` only, via monkeypatched model calls): an Omni outage for one scene yields a
  Ken-Burns fallback clip, a Lyria outage and a failed TTS line still produce a cut, and a failed creative
  director ends the run with ``status=error`` and a clear error event.
* **Replay**, a **9:16** run (portrait keyframes + 720x1280 cut), the **429** rate-limit JSON shape and
  **restart persistence** (a fresh server process serves and replays the finished run).

Two ways to reach the server:

``--base http://localhost:8102``
    A running server started with ``ADMATE_MOCK=1``. For the rate-limit step start it with a small
    ``ADMATE_RUNS_PER_IP_PER_HOUR`` (e.g. 6); restart persistence is then checked with a second invocation
    ``--phase restart --run-id <id>`` after restarting the server (the first phase prints the exact command).

``--inprocess``
    No TCP port needed (useful in sandboxes that forbid ``bind``): the real ``app.main:app`` -- including its
    lifespan -- is served through a streaming in-process ASGI transport, and restart persistence is verified by
    spawning a genuinely fresh Python process on the same data directory.

Exit status is 0 only if every check passed. Usage::

    ADMATE_MOCK=1 .venv/bin/uvicorn app.main:app --port 8102 &
    .venv/bin/python scripts/e2e_mock.py --base http://localhost:8102
    .venv/bin/python scripts/e2e_mock.py --inprocess            # self-contained
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------------------------------ CONTRACT §6 keys
RUN_KEYS = {"id", "status", "mode", "created_at", "input", "plan", "anchor", "scenes", "music", "final",
            "directions", "localizations", "metrics"}
INPUT_KEYS = {"brief", "brand", "aspect", "n_scenes", "variants", "markets", "has_product_image"}
PLAN_KEYS = {"campaign_name", "tagline", "cta", "brand", "anchor_prompt", "scenes", "music"}
PLAN_BRAND_KEYS = {"name", "palette", "visual_style", "mood", "typography", "product", "hero"}
PLAN_SCENE_KEYS = {"id", "title", "beat", "duration_s", "image_prompt", "motion_prompt", "camera", "mood",
                   "energy", "on_screen_text"}
PLAN_MUSIC_KEYS = {"genre", "bpm", "key", "instruments", "arc"}
SCENE_KEYS = {"id", "title", "beat", "mood", "energy", "duration_s", "variants", "judge", "winner", "clip",
              "voiceover"}
VOICEOVER_KEYS = {"status", "v", "text", "url", "latency_ms", "duration_s", "error"}
VARIANT_KEYS = {"idx", "round", "url", "latency_ms", "api_path", "score"}
JUDGE_KEYS = {"round", "winner_index", "rationale", "fix_instructions", "scores", "latency_ms"}
SCORE_KEYS = {"index", "brief_fit", "brand_consistency", "composition", "continuity", "artifact_free",
              "overall", "notes", "idx"}
CLIP_KEYS = {"status", "elapsed_ms", "error", "current", "versions"}
CLIP_VERSION_KEYS = {"v", "url", "instruction", "latency_ms", "api_path", "interaction_id", "fallback"}
MUSIC_KEYS = {"status", "current", "error", "versions"}
MUSIC_VERSION_KEYS = {"v", "url", "prompt", "latency_ms", "reason"}
FINAL_KEYS = {"status", "url", "duration_s", "version", "captions_url"}
DIRECTION_KEYS = {"instruction", "summary", "ts"}
LOCALIZATION_KEYS = {"status", "plan", "scenes", "music_url", "voiceover", "video_url", "captions_url"}
LOC_PLAN_KEYS = {"market", "language", "tagline", "cta", "scene_edits", "music_style", "voiceover", "voice"}
METRICS_KEYS = {"wall_ms", "images_generated", "image_p50_ms", "image_p95_ms", "images_per_min", "judge_calls",
                "repair_rounds", "videos_generated", "video_p50_ms", "video_edits", "music_versions", "inflight",
                "time_to_first_image_ms", "time_to_first_clip_ms", "time_to_final_ms", "api_paths",
                "voiceovers_generated", "tts_p50_ms"}
INFLIGHT_KEYS = {"image", "video", "music", "text", "tts"}

EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".mp4": "video/mp4", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".vtt": "text/vtt"}
STITCH_FADE_S = 0.35  # app.media.stitch default crossfade

MARKETS = ["Hyderabad · Telugu", "Mumbai · Hindi"]


# ========================================================================================== reporting
class CheckFailed(AssertionError):
    """A hard assertion failure inside a step."""


def check(cond: Any, msg: str) -> None:
    if not cond:
        raise CheckFailed(msg)


@dataclass
class StepResult:
    name: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass
class Report:
    steps: list[StepResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        print(f"      · {msg}", flush=True)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def print_summary(self) -> None:
        width = max(len(s.name) for s in self.steps) if self.steps else 10
        print("\n" + "=" * (width + 40))
        print("AdMate e2e summary")
        print("=" * (width + 40))
        for s in self.steps:
            print(f"  {'PASS' if s.ok else 'FAIL'}  {s.name:<{width}}  {s.seconds:7.2f}s  {s.detail}")
        passed = sum(s.ok for s in self.steps)
        print("-" * (width + 40))
        print(f"  {'PASS' if self.ok else 'FAIL'}: {passed}/{len(self.steps)} steps passed")


async def run_step(report: Report, name: str, fn: Callable[[], Any], *, fatal: bool = False) -> Any:
    """Run one named step, time it, record PASS/FAIL. ``fatal`` steps abort the remaining run on failure."""
    print(f"[....] {name}", flush=True)
    t0 = time.perf_counter()
    try:
        out = await fn()
    except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
        dt = time.perf_counter() - t0
        detail = f"{exc.__class__.__name__}: {exc}" if not isinstance(exc, CheckFailed) else str(exc)
        report.steps.append(StepResult(name, False, dt, detail[:300]))
        print(f"[FAIL] {name} ({dt:.2f}s): {detail}", flush=True)
        if fatal:
            raise _Abort(name) from exc
        return None
    dt = time.perf_counter() - t0
    detail = out if isinstance(out, str) else ""
    report.steps.append(StepResult(name, True, dt, detail))
    print(f"[ OK ] {name} ({dt:.2f}s){' ' + detail if detail else ''}", flush=True)
    return out


class _Abort(Exception):
    """Raised when a fatal step fails; remaining steps are skipped."""


# ========================================================================================== test assets
def make_png(width: int = 256, height: int = 256) -> bytes:
    """Dependency-free RGB PNG: a warm gradient 'product' with a darker centre block."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter: none
        for x in range(width):
            inside = width // 3 < x < 2 * width // 3 and height // 4 < y < 3 * height // 4
            r = 200 if inside else 40 + x * 180 // width
            g = 90 if inside else 30 + y * 120 // height
            b = 40 if inside else 120
            rows += bytes((r, g, b))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + \
        chunk(b"IEND", b"")


def make_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """Short silent-ish mono WAV used to exercise /api/transcribe."""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * int(seconds * rate))
    return buf.getvalue()


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG or JPEG without Pillow."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def ffmpeg_path() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # installed with the app requirements

    return imageio_ffmpeg.get_ffmpeg_exe()


def probe_media(data: bytes, suffix: str) -> dict:
    """Parse ``ffmpeg -i`` output: duration seconds, video size, whether an audio stream exists."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        proc = subprocess.run([ffmpeg_path(), "-hide_banner", "-nostdin", "-i", path], capture_output=True,
                              text=True, timeout=60)
    finally:
        os.unlink(path)
    err = proc.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    vm = re.search(r"Stream #\S+.*?: Video:.*?(\d{2,5})x(\d{2,5})", err)
    return {"duration": dur, "width": int(vm.group(1)) if vm else None, "height": int(vm.group(2)) if vm else None,
            "has_audio": bool(re.search(r"Stream #\S+.*?: Audio:", err))}


_VTT_CUE = re.compile(r"(\d+):(\d+):(\d+\.\d+)\s+-->\s+(\d+):(\d+):(\d+\.\d+)\s*\n(.+)")


def parse_vtt(text: str) -> list[tuple[float, float, str]]:
    """(start_s, end_s, text) cues of a WebVTT document; raises CheckFailed if the header is missing."""
    check(text.startswith("WEBVTT"), f"captions must start with WEBVTT, got {text[:20]!r}")
    cues = []
    for m in _VTT_CUE.finditer(text):
        g = m.groups()
        start = int(g[0]) * 3600 + int(g[1]) * 60 + float(g[2])
        end = int(g[3]) * 3600 + int(g[4]) * 60 + float(g[5])
        cues.append((start, end, g[6].strip()))
    return cues


def mean_volume(data: bytes, suffix: str, start: float, duration: float) -> float:
    """Mean loudness (dBFS) of a media file's audio in [start, start + duration] (-inf -> -120)."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        proc = subprocess.run([ffmpeg_path(), "-hide_banner", "-nostdin", "-ss", f"{start:.3f}", "-t",
                               f"{duration:.3f}", "-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                              capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    return float(m.group(1)) if m else -120.0


# ========================================================================================== transport
class _ASGIStream(httpx.AsyncByteStream):
    """Response body fed live from the ASGI app's ``send`` messages (true streaming, unlike ASGITransport)."""

    def __init__(self, queue: asyncio.Queue, task: asyncio.Task, disconnected: asyncio.Event) -> None:
        self._q, self._task, self._disc = queue, task, disconnected

    async def __aiter__(self):
        while True:
            msg = await self._q.get()
            if msg["type"] == "_done":
                return
            if msg["type"] == "http.response.body":
                if msg.get("body"):
                    yield msg["body"]
                if not msg.get("more_body"):
                    return

    async def aclose(self) -> None:
        self._disc.set()
        if not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=2.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self._task.cancel()


class StreamingASGITransport(httpx.AsyncBaseTransport):
    """httpx transport that calls an ASGI app in-process and streams its response body incrementally."""

    def __init__(self, app: Any, client: tuple[str, int] = ("127.0.0.1", 51234)) -> None:
        self.app, self.client = app, client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": request.method,
            "scheme": request.url.scheme, "path": request.url.path, "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query, "root_path": "",
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "client": self.client, "server": (request.url.host, request.url.port or 80),
        }
        queue: asyncio.Queue = asyncio.Queue()
        disconnected = asyncio.Event()
        sent = False

        async def receive() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            await queue.put(message)

        task = asyncio.create_task(self.app(scope, receive, send))
        task.add_done_callback(lambda _t: queue.put_nowait({"type": "_done"}))
        first = await queue.get()
        if first["type"] != "http.response.start":
            exc = task.exception() if task.done() else None
            raise httpx.TransportError(f"ASGI app did not start a response ({exc!r})")
        headers = [(k, v) for k, v in first.get("headers", [])]
        return httpx.Response(first["status"], headers=headers, stream=_ASGIStream(queue, task, disconnected),
                              request=request)


# ========================================================================================== SSE watcher
class EventWatcher:
    """Consumes ``/api/runs/{id}/events`` in the background, dedupes by ``seq`` and lets tests await events."""

    def __init__(self, client: httpx.AsyncClient, run_id: str) -> None:
        self.client, self.run_id = client, run_id
        self.events: list[dict] = []
        self._last_seq = 0
        self._cond = asyncio.Condition()
        self._task: asyncio.Task | None = None
        self.reconnects = 0
        self.error: str | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        while True:
            try:
                async with self.client.stream("GET", f"/api/runs/{self.run_id}/events",
                                              timeout=httpx.Timeout(10.0, read=None)) as resp:
                    if resp.status_code != 200 or "text/event-stream" not in resp.headers.get("content-type", ""):
                        self.error = f"SSE status {resp.status_code} {resp.headers.get('content-type')}"
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        ev = json.loads(line[5:].strip())
                        seq = int(ev.get("seq", 0))
                        if seq and seq <= self._last_seq:
                            continue  # history re-sent after a reconnect
                        self._last_seq = max(self._last_seq, seq)
                        async with self._cond:
                            self.events.append(ev)
                            self._cond.notify_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect like EventSource does
                self.error = f"{exc.__class__.__name__}: {exc}"
            self.reconnects += 1
            await asyncio.sleep(0.5)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    async def wait_for(self, pred: Callable[[list[dict]], Any], timeout: float, what: str) -> Any:
        """Wait until ``pred(events)`` is truthy; returns its value. Raises CheckFailed on timeout."""
        async def waiter():
            async with self._cond:
                while True:
                    val = pred(self.events)
                    if val:
                        return val
                    await self._cond.wait()

        try:
            return await asyncio.wait_for(waiter(), timeout)
        except asyncio.TimeoutError:
            errs = [e.get("msg") for e in self.events if e["type"] == "error"][-3:]
            raise CheckFailed(f"timed out after {timeout:.0f}s waiting for {what}"
                              f"{' (recent errors: ' + '; '.join(map(str, errs)) + ')' if errs else ''}") from None

    def after(self, seq: int, type_: str | None = None, **match: Any) -> list[dict]:
        return [e for e in self.events if e.get("seq", 0) > seq and (type_ is None or e["type"] == type_)
                and all(e.get(k) == v for k, v in match.items())]

    def of(self, type_: str, **match: Any) -> list[dict]:
        return self.after(0, type_, **match)


# ========================================================================================== helpers
def missing(keys: set[str], obj: Any, where: str) -> list[str]:
    if not isinstance(obj, dict):
        return [f"{where} is not an object"]
    return [f"{where}.{k}" for k in sorted(keys - obj.keys())]


async def get_json(client: httpx.AsyncClient, path: str, status: int = 200) -> Any:
    r = await client.get(path)
    check(r.status_code == status, f"GET {path} -> {r.status_code} (expected {status}): {r.text[:200]}")
    return r.json()


async def post_json(client: httpx.AsyncClient, path: str, body: Any = None, status: int = 200) -> Any:
    r = await client.post(path, json=body) if body is not None else await client.post(path)
    check(r.status_code == status, f"POST {path} -> {r.status_code} (expected {status}): {r.text[:200]}")
    data = r.json()
    if status == 200:
        check(data == {"ok": True} or "run_id" in data, f"POST {path} unexpected body {data}")
    else:
        check(isinstance(data, dict) and isinstance(data.get("error"), str) and data["error"],
              f"POST {path} error body must be {{'error': str}}, got {data}")
    return data


async def create_run(client: httpx.AsyncClient, *, brief: str, brand: str, aspect: str, n_scenes: int,
                     variants: int, markets: list[str], product: bytes | None,
                     headers: dict | None = None) -> httpx.Response:
    data = {"brief": brief, "brand": brand, "aspect": aspect, "n_scenes": str(n_scenes),
            "variants": str(variants), "markets": ",".join(markets)}
    files = {"product_image": ("product.png", product, "image/png")} if product else None
    return await client.post("/api/runs", data=data, files=files, headers=headers or {})


# ========================================================================================== the test
class E2E:
    def __init__(self, client: httpx.AsyncClient, report: Report, *, timeout: float, gm: Any = None) -> None:
        self.c, self.r, self.timeout = client, report, timeout
        self.gm = gm  # the server's GenMedia when --inprocess (lets degradation steps inject model failures)
        self.run_id: str | None = None
        self.w: EventWatcher | None = None
        self.n_scenes, self.variants = 4, 3
        self.state: dict = {}

    # ---------------------------------------------------------------- basics
    async def health(self) -> str:
        h = await get_json(self.c, "/api/health")
        check(h.get("ok") is True, f"health not ok: {h}")
        check(h.get("mode") == "mock", f"server must run in mock mode (ADMATE_MOCK=1), got {h.get('mode')}")
        check(h.get("ffmpeg") is True, "ffmpeg not available on the server")
        check(isinstance(h.get("models"), dict) and isinstance(h.get("genai"), dict), "health.models/genai missing")
        r = await self.c.get("/")
        check(r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), f"GET / -> {r.status_code}")
        for asset, mime in (("/static/app.js", "javascript"), ("/static/styles.css", "text/css")):
            r = await self.c.get(asset)
            check(r.status_code == 200 and mime in r.headers.get("content-type", ""),
                  f"GET {asset} -> {r.status_code} {r.headers.get('content-type')}")
        return f"mode={h['mode']} models={len(h['models'])}"

    async def transcribe(self) -> str:
        r = await self.c.post("/api/transcribe", files={"audio": ("brief.wav", make_wav(), "audio/wav")})
        check(r.status_code == 200, f"/api/transcribe -> {r.status_code}: {r.text[:200]}")
        body = r.json()
        check(missing({"text", "latency_ms", "model", "api_path"}, body, "transcribe") == [],
              f"transcribe keys: {body}")
        check(body["text"].strip(), "transcribe returned empty text")
        r = await self.c.post("/api/transcribe", files={"audio": ("x.wav", b"", "audio/wav")})
        check(r.status_code == 400 and "error" in r.json(), f"empty audio should be 400 JSON, got {r.status_code}")
        return f"{body['latency_ms']}ms via {body['api_path']}"

    # ---------------------------------------------------------------- initial run
    async def launch(self) -> str:
        r = await create_run(self.c, brief="Irani chai café launch in Hyderabad — warm, nostalgic, modern",
                             brand="Chai Loop", aspect="16:9", n_scenes=self.n_scenes, variants=self.variants,
                             markets=MARKETS, product=make_png())
        check(r.status_code == 200, f"POST /api/runs -> {r.status_code}: {r.text[:200]}")
        self.run_id = r.json().get("run_id")
        check(re.fullmatch(r"[0-9a-f]{8}", self.run_id or ""), f"bad run_id {self.run_id!r}")
        self.w = EventWatcher(self.c, self.run_id)
        self.w.start()
        return f"run {self.run_id}"

    async def initial_loop(self) -> str:
        w = self.w
        t0 = time.perf_counter()
        await w.wait_for(lambda ev: any(e["type"] == "run_done" for e in ev), self.timeout, "run_done")
        wall = time.perf_counter() - t0
        ev = w.events
        types = [e["type"] for e in ev]
        check(types[0] == "run_started", f"first event must be run_started, got {types[0]}")
        check(ev[0].get("mode") == "mock" and isinstance(ev[0].get("input"), dict), "run_started payload")
        for e in ev:
            check({"type", "run_id", "t"} <= e.keys() and e["run_id"] == self.run_id, f"event envelope: {e}")
        seqs = [e["seq"] for e in ev]
        check(seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "event seq must be strictly increasing")
        ts = [e["t"] for e in ev]
        check(all(isinstance(t, (int, float)) for t in ts), "t must be numeric")

        def first(type_: str, **m: Any) -> dict:
            hit = next((e for e in ev if e["type"] == type_ and all(e.get(k) == v for k, v in m.items())), None)
            check(hit is not None, f"missing event {type_} {m or ''}")
            return hit

        plan_ev, anchor_ev = first("plan"), first("anchor")
        plan = plan_ev["plan"]
        check(plan_ev["seq"] < anchor_ev["seq"], "plan must precede anchor")
        check(len(plan["scenes"]) == self.n_scenes, f"plan has {len(plan['scenes'])} scenes, asked {self.n_scenes}")
        scene_ids = [s["id"] for s in plan["scenes"]]
        variants = [e for e in ev if e["type"] == "variant"]
        check(all(e["seq"] > anchor_ev["seq"] for e in variants), "a variant arrived before the anchor")
        initial = [e for e in variants if e["round"] == 0]
        check(len(initial) == self.n_scenes * self.variants,
              f"expected {self.n_scenes * self.variants} round-0 variants, got {len(initial)}")
        for e in variants:
            check({"scene_id", "idx", "round", "url", "latency_ms", "api_path"} <= e.keys(), f"variant payload {e}")
        final_ev = first("final")
        music_ev = first("music")
        check(music_ev["seq"] < final_ev["seq"], "music must land before the first final cut")
        check(music_ev.get("reason") == "initial" and music_ev.get("v") == 1, f"music v1 payload {music_ev}")
        check(first("run_done")["seq"] > final_ev["seq"], "run_done must follow the first final")
        check(final_ev.get("version") == 1 and final_ev.get("url"), f"final payload {final_ev}")
        repairs = 0
        for sid in scene_ids:
            judges = [e for e in ev if e["type"] == "judge" and e["scene_id"] == sid]
            check(judges, f"no judge event for {sid}")
            repairs += len(judges) - 1
            idxs = {e["idx"] for e in variants if e["scene_id"] == sid}
            for j in judges:
                check({"round", "scores", "winner_idx", "rationale", "fix_instructions", "latency_ms"} <= j.keys(),
                      f"judge payload {j}")
                check(j["winner_idx"] in idxs, f"{sid} judge winner_idx {j['winner_idx']} not a variant idx {idxs}")
                check(all(s.get("idx") in idxs for s in j["scores"]), f"{sid} judge scores must carry global idx")
            win = first("winner", scene_id=sid)
            check(win["by"] == "judge" and win["seq"] > judges[-1]["seq"], f"{sid} winner must follow its judge")
            check(win["idx"] == judges[-1]["winner_idx"], f"{sid} winner idx != last judge's winner_idx")
            clip = first("clip", scene_id=sid)
            check(clip["seq"] > win["seq"] and clip["v"] == 1, f"{sid} clip v1 must follow its winner")
            check({"url", "instruction", "latency_ms", "api_path", "interaction_id", "fallback"} <= clip.keys(),
                  f"clip payload {clip}")
            check(clip["seq"] < final_ev["seq"], f"{sid} clip must land before the first final")
            vo = first("voiceover", scene_id=sid)
            check({"v", "text", "url", "latency_ms", "duration_s"} <= vo.keys() and vo["v"] == 1 and vo["text"],
                  f"{sid} voiceover payload {vo}")
            check(vo["seq"] > plan_ev["seq"] and vo["seq"] < final_ev["seq"],
                  f"{sid} voiceover must land between the plan and the first final")
        check(final_ev.get("captions_url"), f"first final has no captions_url: {final_ev}")
        errors = [e for e in ev if e["type"] == "error"]
        check(not errors, f"error events during the initial loop: {[e.get('msg') for e in errors]}")
        check(any(e["type"] == "metrics" for e in ev), "no metrics events")
        # Pipelining evidence (informational: timing-dependent, not a contract guarantee).
        first_clip_start = min(e["t"] for e in ev if e["type"] == "stage" and e.get("stage") == "motion")
        last_judge = max(e["t"] for e in ev if e["type"] == "judge")
        music_start = next(e["t"] for e in ev if e["type"] == "stage" and e.get("stage") == "music")
        last_vo = max(e["t"] for e in ev if e["type"] == "voiceover")
        self.r.note(f"pipelining: first Omni start t={first_clip_start}ms vs last judge t={last_judge}ms; "
                    f"music start t={music_start}ms vs anchor t={anchor_ev['t']}ms; all narration by "
                    f"t={last_vo}ms; repair rounds={repairs}")
        return (f"{len(ev)} events, first final v1 after {wall:.1f}s "
                f"(TTFI {next(e['t'] for e in variants)}ms, repairs {repairs})")

    # ---------------------------------------------------------------- state shape
    async def state_shape(self) -> str:
        st = await get_json(self.c, f"/api/runs/{self.run_id}")
        self.state = st
        miss = missing(RUN_KEYS, st, "run") + missing(INPUT_KEYS, st["input"], "input")
        plan = st["plan"]
        miss += missing(PLAN_KEYS, plan, "plan") + missing(PLAN_BRAND_KEYS, plan.get("brand"), "plan.brand")
        miss += missing(PLAN_MUSIC_KEYS, plan.get("music"), "plan.music")
        for ps in plan.get("scenes", []):
            miss += missing(PLAN_SCENE_KEYS, ps, f"plan.scenes[{ps.get('id')}]")
        pal = plan.get("brand", {}).get("palette", [])
        check(4 <= len(pal) <= 5 and all(re.fullmatch(r"#[0-9A-Fa-f]{6}", p) for p in pal), f"palette {pal}")
        miss += missing({"url", "latency_ms"}, st["anchor"], "anchor")
        check(st["status"] == "done" and st["mode"] == "mock", f"status/mode {st['status']}/{st['mode']}")
        check(st["input"]["has_product_image"] is True, "has_product_image should be true")
        check(st["input"]["markets"] == MARKETS, f"markets round-trip {st['input']['markets']}")
        for sc in st["scenes"]:
            sid = sc.get("id")
            miss += missing(SCENE_KEYS, sc, f"scenes[{sid}]") + missing(CLIP_KEYS, sc["clip"], f"{sid}.clip")
            for v in sc["variants"]:
                miss += missing(VARIANT_KEYS, v, f"{sid}.variants[{v.get('idx')}]")
            for j in sc["judge"]:
                miss += missing(JUDGE_KEYS, j, f"{sid}.judge[{j.get('round')}]")
                for s in j["scores"]:
                    miss += missing(SCORE_KEYS, s, f"{sid}.judge.scores")
            for cv in sc["clip"]["versions"]:
                miss += missing(CLIP_VERSION_KEYS, cv, f"{sid}.clip.versions[{cv.get('v')}]")
            miss += missing(VOICEOVER_KEYS, sc.get("voiceover"), f"{sid}.voiceover")
            vo = sc.get("voiceover") or {}
            check(vo.get("status") == "done" and vo.get("url") and (vo.get("duration_s") or 0) > 0.3,
                  f"{sid} voiceover not rendered: {vo}")
            idxs = [v["idx"] for v in sc["variants"]]
            check(idxs == sorted(set(idxs)), f"{sid} variant idx must be unique + sorted: {idxs}")
            check(sc["winner"] in idxs, f"{sid} winner {sc['winner']} not in {idxs}")
            check(all(j["winner_index"] in idxs for j in sc["judge"]), f"{sid} judge winner_index not global idx")
            check(sc["clip"]["status"] == "done" and sc["clip"]["current"] in
                  [cv["v"] for cv in sc["clip"]["versions"]], f"{sid} clip current/status {sc['clip']}")
            check(sum(1 for v in sc["variants"] if v["score"] is not None) >= 1, f"{sid} no scored variants")
        miss += missing(MUSIC_KEYS, st["music"], "music") + missing(FINAL_KEYS, st["final"], "final")
        for mv in st["music"]["versions"]:
            miss += missing(MUSIC_VERSION_KEYS, mv, f"music.versions[{mv.get('v')}]")
        miss += missing(METRICS_KEYS, st["metrics"], "metrics")
        miss += missing(INFLIGHT_KEYS, st["metrics"].get("inflight"), "metrics.inflight")
        check(not miss, f"missing contract keys: {miss[:12]}")
        leaked = [k for k in st["metrics"] if k.startswith("_")]
        check(not leaked, f"internal metrics keys leaked: {leaked}")
        m = st["metrics"]
        check(m["images_generated"] >= self.n_scenes * self.variants + 1, f"images_generated {m['images_generated']}")
        check(m["judge_calls"] >= self.n_scenes and m["videos_generated"] >= self.n_scenes, "judge/video counts")
        check(m["time_to_final_ms"] and m["time_to_first_image_ms"] and m["time_to_first_clip_ms"], "time-to-x unset")
        check(m["time_to_first_image_ms"] <= m["time_to_first_clip_ms"] <= m["time_to_final_ms"], "TTFx ordering")
        check(m["voiceovers_generated"] >= self.n_scenes and m["tts_p50_ms"], f"tts metrics {m}")
        plan_voice = plan.get("voice") or {}
        check(plan_voice.get("name") and plan_voice.get("style"), f"plan.voice {plan_voice}")
        extra = sorted(st.keys() - RUN_KEYS)
        return (f"all CONTRACT §6 keys present; images={m['images_generated']} p50={m['image_p50_ms']}ms "
                f"img/min={m['images_per_min']}" + (f"; extra top-level keys {extra}" if extra else ""))

    # ---------------------------------------------------------------- media
    @staticmethod
    def urls_in(st: dict) -> list[str]:
        urls = [st["anchor"]["url"]] if st.get("anchor") else []
        for sc in st["scenes"]:
            urls += [v["url"] for v in sc["variants"]] + [cv["url"] for cv in sc["clip"]["versions"]]
            urls += [(sc.get("voiceover") or {}).get("url")]
        urls += [mv["url"] for mv in st["music"]["versions"]]
        urls += [st["final"].get("url"), st["final"].get("captions_url")]
        for loc in st["localizations"].values():
            urls += [s["url"] for s in loc.get("scenes", [])] + [loc.get("music_url")]
            urls += [v["url"] for v in loc.get("voiceover", [])] + [loc.get("video_url"), loc.get("captions_url")]
        return [u for u in urls if u]

    async def media_all(self, st: dict | None = None) -> str:
        st = st or await get_json(self.c, f"/api/runs/{self.run_id}")
        urls = self.urls_in(st)
        check(urls, "no media URLs in state")
        total = 0
        for url in urls:
            check(url.startswith(f"/media/{st['id']}/"), f"media url outside run folder: {url}")
            r = await self.c.get(url)
            ext = Path(url).suffix.lower()
            ctype = r.headers.get("content-type", "").split(";")[0]
            check(r.status_code == 200, f"GET {url} -> {r.status_code}")
            check(ctype == EXT_MIME.get(ext), f"{url} content-type {ctype!r}, expected {EXT_MIME.get(ext)!r}")
            check(len(r.content) > (20 if ext == ".vtt" else 100),
                  f"{url} is suspiciously small ({len(r.content)} bytes)")
            total += len(r.content)
        return f"{len(urls)} assets OK ({total / 1e6:.1f} MB)"

    async def media_security(self) -> str:
        final = self.state["final"]["url"]
        r = await self.c.get(final, headers={"Range": "bytes=0-99"})
        check(r.status_code == 206 and len(r.content) == 100 and r.headers.get("content-range", "").startswith(
            "bytes 0-99/"), f"Range request -> {r.status_code} {r.headers.get('content-range')}")
        bad = [f"/media/{self.run_id}/run.json", f"/media/{self.run_id}/..%2F..%2F.env",
               f"/media/{self.run_id}/events.jsonl", "/media/zzzzzzzz/anchor.png", f"/media/{self.run_id}/nope.png"]
        for url in bad:
            r = await self.c.get(url)
            check(r.status_code == 404, f"GET {url} should be 404, got {r.status_code}")
        return "Range 206; traversal/hidden files 404"

    async def final_duration(self, st: dict | None = None, *, expect_wh: tuple[int, int] = (1280, 720)) -> str:
        st = st or self.state
        durs = []
        for sc in st["scenes"]:
            cur = next(cv for cv in sc["clip"]["versions"] if cv["v"] == sc["clip"]["current"])
            durs.append(probe_media((await self.c.get(cur["url"])).content, ".mp4")["duration"])
        info = probe_media((await self.c.get(st["final"]["url"])).content, ".mp4")
        expected = sum(durs) - STITCH_FADE_S * (len(durs) - 1)
        check(abs(info["duration"] - expected) <= 0.3,
              f"final {info['duration']:.2f}s vs clips {sum(durs):.2f}s - crossfades = {expected:.2f}s")
        check(abs(info["duration"] - float(st["final"]["duration_s"])) <= 0.15,
              f"final.duration_s {st['final']['duration_s']} != probed {info['duration']:.2f}")
        check((info["width"], info["height"]) == expect_wh, f"final is {info['width']}x{info['height']}")
        check(info["has_audio"], "final cut has no soundtrack")
        narration = await self.check_narration(st["final"]["url"], st["final"].get("captions_url"),
                                               [(sc.get("voiceover") or {}).get("text") for sc in st["scenes"]],
                                               info["duration"])
        return (f"{info['duration']:.2f}s = {len(durs)} clips {sum(durs):.2f}s - {len(durs) - 1}x{STITCH_FADE_S}s "
                f"fades; {info['width']}x{info['height']} + audio; {narration}")

    async def check_narration(self, video_url: str, captions_url: str | None, lines: list[str | None],
                              duration: float) -> str:
        """Captions have one in-range cue per narrated line, and the narration is audible under its cue."""
        check(captions_url, f"no captions for {video_url}")
        r = await self.c.get(captions_url)
        check(r.status_code == 200 and r.headers.get("content-type", "").startswith("text/vtt"),
              f"GET {captions_url} -> {r.status_code} {r.headers.get('content-type')}")
        cues = parse_vtt(r.text)
        expected = [" ".join(t.split()) for t in lines if t]
        check([c[2] for c in cues] == expected, f"caption cues {[c[2] for c in cues]} != lines {expected}")
        for a, b in zip(cues, cues[1:]):
            check(a[1] <= b[0] + 1e-3, f"caption cues overlap: {a} / {b}")
        check(all(0 <= c[0] < c[1] <= duration + 0.05 for c in cues), f"caption times outside the cut: {cues}")
        video = (await self.c.get(video_url)).content
        start, end, _ = cues[0]
        loud = mean_volume(video, ".mp4", start + 0.2, max(0.5, min(2.0, end - start - 0.4)))
        check(loud > -35.0, f"narration window {start:.2f}-{end:.2f}s is near-silent ({loud:.1f} dBFS)")
        return f"{len(cues)} caption cues, narration {loud:.1f} dBFS"

    # ---------------------------------------------------------------- actions
    def _final_after(self, seq: int) -> Callable[[list[dict]], Any]:
        return lambda ev: next((e for e in ev if e["type"] == "final" and e["seq"] > seq), None)

    async def _wait_restitch(self, after_seq: int, what: str) -> dict:
        return await self.w.wait_for(self._final_after(after_seq), self.timeout, f"re-stitch after {what}")

    async def act_validation(self) -> str:
        rid = self.run_id
        await post_json(self.c, f"/api/runs/{rid}/scenes/s1/select", {"idx": 999}, status=400)
        await post_json(self.c, f"/api/runs/{rid}/scenes/s1/edit", {"instruction": ""}, status=422)
        await post_json(self.c, f"/api/runs/{rid}/scenes/s99/edit", {"instruction": "x"}, status=404)
        await post_json(self.c, "/api/runs/deadbeef/direct", {"instruction": "x"}, status=404)
        await post_json(self.c, f"/api/runs/{rid}/localize", {"markets": []}, status=422)
        await get_json(self.c, "/api/runs/NOTANID", status=404)
        r = await create_run(self.c, brief="x", brand="", aspect="4:3", n_scenes=4, variants=3, markets=[],
                             product=None)
        check(r.status_code == 400 and "error" in r.json(), f"bad aspect should be 400, got {r.status_code}")
        r = await create_run(self.c, brief="x", brand="", aspect="16:9", n_scenes=9, variants=3, markets=[],
                             product=None)
        check(r.status_code == 400 and "error" in r.json(), f"n_scenes=9 should be 400, got {r.status_code}")
        return "400/404/422 JSON {error}"

    async def act_edit(self) -> str:
        w, sid = self.w, "s1"
        mark = w.last_seq
        await post_json(self.c, f"/api/runs/{self.run_id}/scenes/{sid}/edit", {"instruction": "add gentle rain"})
        clip = await w.wait_for(lambda ev: next((e for e in w.after(mark, "clip", scene_id=sid)), None),
                                self.timeout, f"{sid} edited clip")
        check(clip["v"] == 2 and clip["instruction"] == "add gentle rain", f"edit clip payload {clip}")
        # The mood interpretation runs in parallel with the Omni edit; its scene_update is emitted right after the
        # clip lands, so give it a moment rather than racing the event.
        try:
            upd = await w.wait_for(lambda ev: w.after(mark, "scene_update", scene_id=sid), 5, "scene_update")
        except CheckFailed:
            upd = []
        music = None
        # Mock interpretation is deterministic: "rain" is a mood change, so the adaptive re-score is mandatory.
        check(upd and upd[0].get("mood") and "energy" in upd[0], f"expected a scene_update for {sid}, got {upd}")
        music = await w.wait_for(lambda ev: next((e for e in w.after(mark, "music")
                                                  if str(e.get("reason", "")).startswith(f"scene {sid}")), None),
                                 self.timeout, "adaptive music re-score")
        last = max(clip["seq"], music["seq"] if music else 0)
        fin = await self._wait_restitch(last, "edit")
        return (f"clip v2 ({clip['api_path']}, fallback={clip['fallback']}); "
                f"{'music v' + str(music['v']) + ' ' + repr(music['reason']) if music else 'no re-score'}; "
                f"final v{fin['version']}")

    async def act_direct(self) -> str:
        w = self.w
        mark = w.last_seq
        instruction = "Make it a monsoon evening — moodier, slower, rain on the windows, dramatic trailer narration"
        await post_json(self.c, f"/api/runs/{self.run_id}/direct", {"instruction": instruction})
        d = await w.wait_for(lambda ev: next(iter(w.after(mark, "direction")), None), self.timeout, "direction")
        dplan = d["plan"]
        check(missing({"summary", "scene_edits", "restyle_keyframes", "music", "mood_updates"}, dplan,
                      "DirectionPlan") == [], f"DirectionPlan keys {list(dplan)}")
        targets = {se["scene_id"] for se in dplan["scene_edits"]}
        check(targets, "direction produced no scene edits")
        await w.wait_for(lambda ev: next((e for e in w.after(mark, "stage", stage="direct", status="done")), None),
                         self.timeout, "direct stage done")
        clips = w.after(mark, "clip")
        check(targets <= {e["scene_id"] for e in clips}, f"direct fan-out missing clips for "
                                                         f"{targets - {e['scene_id'] for e in clips}}")
        musics = [e for e in w.after(mark, "music") if str(e.get("reason", "")).startswith("direction:")]
        if dplan["music"].get("rescore", True):
            check(musics, "direction asked for a re-score but no music version landed")
        vo_targets = {vu["scene_id"] for vu in dplan.get("voiceover_updates") or []}
        check(vo_targets and dplan.get("voice_style"), f"a tone note must re-voice the narration: {dplan}")
        vos = w.after(mark, "voiceover")
        check(vo_targets <= {e["scene_id"] for e in vos}, f"re-voice missing for {vo_targets - {e['scene_id'] for e in vos}}")
        last = max([e["seq"] for e in clips + musics + vos])
        fin = await self._wait_restitch(last, "direct")
        errs = w.after(mark, "error")
        check(not errs, f"errors during direct: {[e['msg'] for e in errs]}")
        return (f"{len(targets)} clip edits in parallel + {len(musics)} re-score + {len(vo_targets)} re-voiced "
                f"({dplan['voice_style']!r}, restyle={dplan['restyle_keyframes']}); final v{fin['version']}")

    async def act_voiceover(self) -> str:
        w, sid = self.w, "s2"
        mark = w.last_seq
        text = "Every sip, a little slower. Every table, a story."
        await post_json(self.c, f"/api/runs/{self.run_id}/scenes/{sid}/voiceover", {"text": text, "voice": "Puck"})
        vo = await w.wait_for(lambda ev: next(iter(w.after(mark, "voiceover", scene_id=sid)), None), self.timeout,
                              f"{sid} re-voice")
        check(vo["text"] == text and vo["v"] >= 2 and vo["url"], f"re-voice payload {vo}")
        fin = await self._wait_restitch(vo["seq"], "re-voice")
        cues = parse_vtt((await self.c.get(fin["captions_url"])).text)
        check(any(c[2] == text for c in cues), f"new line missing from captions {fin['captions_url']}")
        st = await get_json(self.c, f"/api/runs/{self.run_id}")
        sc = next(s for s in st["scenes"] if s["id"] == sid)
        check(sc["voiceover"]["text"] == text and sc["voiceover"]["voice"] == "Puck", f"state {sc['voiceover']}")
        await post_json(self.c, f"/api/runs/{self.run_id}/scenes/{sid}/voiceover", {"voice": "../x"}, status=422)
        return f"{sid} v{vo['v']} ({vo['latency_ms']}ms, {vo['duration_s']}s); final v{fin['version']} re-captioned"

    async def act_music(self) -> str:
        w = self.w
        mark = w.last_seq
        await post_json(self.c, f"/api/runs/{self.run_id}/music", {"instruction": "more tabla, brighter ending"})
        mus = await w.wait_for(lambda ev: next(iter(w.after(mark, "music")), None), self.timeout, "music version")
        check(str(mus["reason"]).startswith("re-score") and mus["url"], f"music payload {mus}")
        fin = await self._wait_restitch(mus["seq"], "music")
        return f"music v{mus['v']} ({mus['latency_ms']}ms); final v{fin['version']}"

    async def act_select(self) -> str:
        w, sid = self.w, "s2"
        st = await get_json(self.c, f"/api/runs/{self.run_id}")
        sc = next(s for s in st["scenes"] if s["id"] == sid)
        pick = next(v["idx"] for v in sc["variants"] if v["idx"] != sc["winner"])
        prev_v = sc["clip"]["current"]
        mark = w.last_seq
        await post_json(self.c, f"/api/runs/{self.run_id}/scenes/{sid}/select", {"idx": pick})
        win = await w.wait_for(lambda ev: next(iter(w.after(mark, "winner", scene_id=sid)), None), 10, "winner")
        check(win["by"] == "user" and win["idx"] == pick, f"select winner payload {win}")
        clip = await w.wait_for(lambda ev: next(iter(w.after(mark, "clip", scene_id=sid)), None), self.timeout,
                                f"{sid} re-render")
        check(clip["v"] > prev_v, f"re-render should be a new version (> v{prev_v})")
        fin = await self._wait_restitch(clip["seq"], "select")
        st = await get_json(self.c, f"/api/runs/{self.run_id}")
        check(next(s for s in st["scenes"] if s["id"] == sid)["winner"] == pick, "state winner not updated")
        return f"{sid} winner -> idx {pick}, clip v{clip['v']}; final v{fin['version']}"

    async def act_regenerate(self) -> str:
        w, sid = self.w, "s3"
        mark = w.last_seq
        await post_json(self.c, f"/api/runs/{self.run_id}/scenes/{sid}/regenerate",
                        {"instruction": "tighter close-up on the glass"})
        clip = await w.wait_for(lambda ev: next(iter(w.after(mark, "clip", scene_id=sid)), None), self.timeout,
                                f"{sid} regenerated clip")
        vars_ = w.after(mark, "variant", scene_id=sid)
        judges = w.after(mark, "judge", scene_id=sid)
        wins = w.after(mark, "winner", scene_id=sid)
        check(len(vars_) >= self.variants, f"regenerate produced {len(vars_)} variants")
        check(judges and wins and wins[-1]["by"] == "judge", "regenerate must judge and pick a winner")
        check(min(v["round"] for v in vars_) >= 1, "regenerated variants must be a new round")
        prior = {v["idx"] for v in next(s for s in self.state["scenes"] if s["id"] == sid)["variants"]}
        check(not ({v["idx"] for v in vars_} & prior), "regenerated variants reused old idx values")
        check(wins[-1]["seq"] < clip["seq"], "clip must follow the new winner")
        fin = await self._wait_restitch(clip["seq"], "regenerate")
        return (f"round {vars_[0]['round']}: {len(vars_)} variants, {len(judges)} judge, winner idx {wins[-1]['idx']}, "
                f"clip v{clip['v']}; final v{fin['version']}")

    async def act_localize(self) -> str:
        w = self.w
        mark = w.last_seq
        await post_json(self.c, f"/api/runs/{self.run_id}/localize", {"markets": MARKETS})

        def done(ev):
            fin = {e["market"] for e in w.after(mark, "localize_status") if e["status"] in ("done", "error")}
            return fin >= set(MARKETS)

        await w.wait_for(done, self.timeout, "localize done for both markets")
        summary = []
        for mk in MARKETS:
            st = w.after(mark, "localize_status", market=mk)
            check(st[-1]["status"] == "done", f"{mk} localize status {st[-1]}")
            lp = w.after(mark, "localize_plan", market=mk)
            check(lp and missing(LOC_PLAN_KEYS, lp[0]["plan"], "LocalizePlan") == [], f"{mk} localize_plan")
            imgs = w.after(mark, "localize_image", market=mk)
            check({e["scene_id"] for e in imgs} == {f"s{i}" for i in range(1, self.n_scenes + 1)},
                  f"{mk} localize_image scenes {[e['scene_id'] for e in imgs]}")
            mus = w.after(mark, "localize_music", market=mk)
            check(len(mus) == 1 and mus[0]["url"], f"{mk} localize_music {mus}")
            vos = w.after(mark, "localize_voiceover", market=mk)
            check({e["scene_id"] for e in vos} == {f"s{i}" for i in range(1, self.n_scenes + 1)}
                  and all(e["text"] and e["url"] for e in vos), f"{mk} localize_voiceover {vos}")
            vid = w.after(mark, "localize_video", market=mk)
            check(len(vid) == 1 and vid[0]["url"] and vid[0]["captions_url"] and vid[0]["duration_s"] > 5,
                  f"{mk} localize_video {vid}")
            check(vid[0]["seq"] < st[-1]["seq"], f"{mk} localize_video must precede localize done")
            info = probe_media((await self.c.get(vid[0]["url"])).content, ".mp4")
            check(info["has_audio"] and abs(info["duration"] - vid[0]["duration_s"]) < 0.2, f"{mk} animatic {info}")
            await self.check_narration(vid[0]["url"], vid[0]["captions_url"], [e["text"] for e in
                                       sorted(vos, key=lambda e: e["scene_id"])], info["duration"])
            summary.append(f"{mk}: {len(imgs)} img + {len(vos)} VO + music + {info['duration']:.1f}s animatic "
                           f"({lp[0]['plan']['language']})")
        st = await get_json(self.c, f"/api/runs/{self.run_id}")
        for mk in MARKETS:
            loc = st["localizations"].get(mk)
            check(loc and missing(LOCALIZATION_KEYS, loc, f"localizations[{mk}]") == [], f"state localization {mk}")
            check(len(loc["scenes"]) == self.n_scenes and loc["music_url"] and loc["video_url"]
                  and len(loc["voiceover"]) == self.n_scenes, f"state localization {mk} incomplete")
        return "; ".join(summary)

    async def act_final(self) -> str:
        mark = self.w.last_seq
        await post_json(self.c, f"/api/runs/{self.run_id}/final")
        fin = await self._wait_restitch(mark, "forced final")
        return f"final v{fin['version']} {fin['duration_s']}s"

    async def final_state(self) -> str:
        st = await get_json(self.c, f"/api/runs/{self.run_id}")
        self.state = st
        m = st["metrics"]
        check(st["final"]["version"] >= 7, f"expected >= 7 stitched versions, got {st['final']['version']}")
        check(len(st["directions"]) == 1 and missing(DIRECTION_KEYS, st["directions"][0], "directions[0]") == [],
              "directions entry")
        check(m["video_edits"] >= 1 + self.n_scenes - 1, f"video_edits {m['video_edits']}")
        check(m["music_versions"] == len(st["music"]["versions"]) >= 3, f"music_versions {m['music_versions']}")
        check(st["music"]["current"] == st["music"]["versions"][-1]["v"], "music.current must be newest version")
        errs = self.w.of("error")
        check(not errs, f"error events: {[e['msg'] for e in errs]}")
        await self.media_all(st)
        await self.final_duration(st)
        return (f"final v{st['final']['version']}, {m['videos_generated']} clips ({m['video_edits']} edits), "
                f"{m['music_versions']} scores, {m['images_generated']} images, all media re-verified")

    # ---------------------------------------------------------------- listings & replay
    async def listings(self) -> str:
        runs = await get_json(self.c, "/api/runs")
        check(isinstance(runs, list) and runs and len(runs) <= 20, "GET /api/runs must be a non-empty list <= 20")
        for it in runs:
            check(missing({"id", "campaign_name", "status", "created_at", "thumb"}, it, "runs[]") == [], f"{it}")
        check([r["created_at"] for r in runs] == sorted((r["created_at"] for r in runs), reverse=True),
              "runs must be newest first")
        mine = next((r for r in runs if r["id"] == self.run_id), None)
        check(mine and mine["thumb"] and mine["status"] == "done", f"run missing from list: {mine}")
        sc = await get_json(self.c, "/api/showcase")
        check("run_id" in sc and sc["run_id"], f"showcase {sc}")
        return f"{len(runs)} runs listed; showcase={sc['run_id']}"

    async def replay(self, run_id: str | None = None, *, expect_min: int | None = None) -> str:
        run_id = run_id or self.run_id
        hist_n = expect_min if expect_min is not None else len(self.w.events)
        got: list[dict] = []
        comments: list[str] = []
        t0 = time.perf_counter()

        async def consume():
            async with self.c.stream("GET", f"/api/runs/{run_id}/events?replay=1&speed=100",
                                     timeout=httpx.Timeout(10.0, read=None)) as resp:
                check(resp.status_code == 200, f"replay status {resp.status_code}")
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        got.append(json.loads(line[5:]))
                    elif line.startswith(":"):
                        comments.append(line)

        try:
            await asyncio.wait_for(consume(), timeout=max(60.0, self.timeout))
        except asyncio.TimeoutError:
            raise CheckFailed(f"replay stream did not end (got {len(got)} events)") from None
        check(got and got[0]["type"] == "run_started", "replay must start with run_started")
        check(len(got) >= hist_n, f"replay re-emitted {len(got)} events, history had {hist_n}")
        seqs = [e["seq"] for e in got]
        check(seqs == sorted(seqs), "replay out of order")
        types = {e["type"] for e in got}
        check({"plan", "variant", "judge", "winner", "clip", "music", "final", "run_done"} <= types,
              f"replay missing types {sorted({'plan', 'variant', 'judge', 'winner', 'clip', 'music', 'final'} - types)}")
        return f"{len(got)} events replayed in {time.perf_counter() - t0:.1f}s, stream ended"

    # ---------------------------------------------------------------- portrait run
    async def portrait_run(self) -> str:
        r = await create_run(self.c, brief="Monsoon sneaker drop for Gen-Z", brand="PuddleJump", aspect="9:16",
                             n_scenes=3, variants=2, markets=[], product=None)
        check(r.status_code == 200, f"9:16 POST -> {r.status_code}: {r.text[:200]}")
        rid = r.json()["run_id"]
        w = EventWatcher(self.c, rid)
        w.start()
        try:
            await w.wait_for(lambda ev: any(e["type"] == "run_done" for e in ev), self.timeout, "9:16 run_done")
            check(not w.of("error"), f"9:16 errors: {[e['msg'] for e in w.of('error')]}")
            check(len([e for e in w.of("variant") if e["round"] == 0]) == 6, "9:16 expected 3x2 round-0 variants")
        finally:
            await w.stop()
        st = await get_json(self.c, f"/api/runs/{rid}")
        check(st["input"]["aspect"] == "9:16" and st["input"]["has_product_image"] is False, "9:16 input")
        img = (await self.c.get(st["scenes"][0]["variants"][0]["url"])).content
        size = image_size(img)
        check(size and size[0] < size[1], f"9:16 keyframe must be portrait, got {size}")
        await self.media_all(st)
        detail = await self.final_duration(st, expect_wh=(720, 1280))
        return f"run {rid}: keyframe {size[0]}x{size[1]}; final {detail}"

    # ---------------------------------------------------------------- degradation (in-process only)
    async def degraded_run(self) -> str:
        """Omni fails for one scene, Lyria is down and one TTS line fails: the run must still cut a film."""
        from app.genai_client import GenAIError  # noqa: WPS433 - only importable in-process

        gm = self.gm
        orig = {name: getattr(gm, name) for name in ("generate_video", "generate_music", "generate_speech")}
        calls = {"video": 0, "speech": 0}

        async def flaky_video(*a: Any, **kw: Any):
            calls["video"] += 1
            if calls["video"] == 1:
                raise GenAIError("forced Omni outage (e2e)", model="omni", api_path="interactions")
            return await orig["generate_video"](*a, **kw)

        async def dead_music(*_a: Any, **_kw: Any):
            raise GenAIError("forced Lyria outage (e2e)", model="lyria", api_path="interactions")

        async def flaky_speech(*a: Any, **kw: Any):
            calls["speech"] += 1
            if calls["speech"] == 1:
                raise GenAIError("forced TTS outage (e2e)", model="tts", api_path="generate_content")
            return await orig["generate_speech"](*a, **kw)

        gm.generate_video, gm.generate_music, gm.generate_speech = flaky_video, dead_music, flaky_speech
        try:
            r = await create_run(self.c, brief="EV scooter for Gen-Z commuters", brand="Zipp", aspect="16:9",
                                 n_scenes=3, variants=2, markets=[], product=None)
            check(r.status_code == 200, f"POST -> {r.status_code}: {r.text[:200]}")
            rid = r.json()["run_id"]
            w = EventWatcher(self.c, rid)
            w.start()
            try:
                await w.wait_for(lambda ev: any(e["type"] == "run_done" for e in ev), self.timeout,
                                 "degraded run_done")
            finally:
                await w.stop()
        finally:
            for name, fn in orig.items():
                setattr(gm, name, fn)
        fallbacks = [e for e in w.of("clip") if e.get("fallback") == "ken_burns"]
        check(len(fallbacks) == 1 and fallbacks[0]["api_path"] == "fallback", f"fallback clips {fallbacks}")
        check(any("Ken Burns" in str(e.get("msg")) for e in w.of("log", level="warn")), "no warn log for fallback")
        check({e["stage"] for e in w.of("error")} <= {"music"}, f"unexpected errors {w.of('error')}")
        check(len(w.of("voiceover_status", status="error")) == 1 and len(w.of("voiceover")) == 2,
              "expected exactly one failed narration line")
        st = await get_json(self.c, f"/api/runs/{rid}")
        check(st["status"] == "done" and st["music"]["status"] == "error", f"status {st['status']}/{st['music']}")
        check(all(sc["clip"]["status"] == "done" for sc in st["scenes"]), "every scene must have a clip")
        info = probe_media((await self.c.get(st["final"]["url"])).content, ".mp4")
        check(info["has_audio"], "narration-only cut must still carry audio")
        cues = parse_vtt((await self.c.get(st["final"]["captions_url"])).text)
        check(len(cues) == 2, f"captions should skip the failed line, got {len(cues)} cues")
        # With no soundtrack the audio is narration alone: loud under a cue, silent in the widest gap between cues.
        video = (await self.c.get(st["final"]["url"])).content
        edges = [0.0] + [t for c in cues for t in c[:2]] + [info["duration"]]
        gap = max(((a, b) for a, b in zip(edges[::2], edges[1::2])), key=lambda g: g[1] - g[0])
        check(gap[1] - gap[0] >= 0.8, f"no narration-free gap to measure: {cues}")
        voiced = mean_volume(video, ".mp4", cues[0][0] + 0.2, max(0.5, min(2.0, cues[0][1] - cues[0][0] - 0.4)))
        silent = mean_volume(video, ".mp4", gap[0] + 0.2, gap[1] - gap[0] - 0.4)
        check(voiced > -35.0 and voiced - silent > 30.0,
              f"narration not audible: cue window {voiced:.1f} dBFS vs gap {silent:.1f} dBFS")
        return (f"run {rid}: {fallbacks[0]['scene_id']} -> ken_burns fallback, no soundtrack, 1 line dropped; "
                f"final {info['duration']:.1f}s, {len(cues)} cues, narration {voiced:.1f} dBFS vs gap "
                f"{silent:.1f} dBFS")

    async def director_failure(self) -> str:
        """A failing creative director must end the run with status=error and a clear error event."""
        from app.genai_client import GenAIError  # noqa: WPS433

        gm = self.gm
        orig = gm.generate_json

        async def dead_json(*_a: Any, **_kw: Any):
            raise GenAIError("forced text-model outage (e2e)", model="flash", api_path="generate_content")

        gm.generate_json = dead_json
        try:
            r = await create_run(self.c, brief="Director outage probe", brand="", aspect="16:9", n_scenes=3,
                                 variants=2, markets=[], product=None)
            rid = r.json()["run_id"]
            w = EventWatcher(self.c, rid)
            w.start()
            try:
                err = await w.wait_for(lambda ev: next((e for e in ev if e["type"] == "error"), None), 30,
                                       "director error event")
            finally:
                await w.stop()
        finally:
            gm.generate_json = orig
        check(err["stage"] == "director" and "forced text-model outage" in err["msg"], f"error event {err}")
        await asyncio.sleep(0.2)
        st = await get_json(self.c, f"/api/runs/{rid}")
        check(st["status"] == "error" and st["plan"] is None, f"status {st['status']}")
        return f"run {rid}: status=error, {err['msg']!r}"

    # ---------------------------------------------------------------- rate limit
    async def rate_limit(self) -> str:
        h = await get_json(self.c, "/api/health")
        limits = h.get("limits") or {}
        per_ip = int(limits.get("runs_per_ip_per_hour") or 0)
        check(per_ip > 0, "server has no per-IP limit; start it with ADMATE_RUNS_PER_IP_PER_HOUR=<n> (e.g. 6)")
        check(per_ip <= 12, f"per-IP limit {per_ip} too high to exercise; use ADMATE_RUNS_PER_IP_PER_HOUR<=12")
        created = 0
        for _ in range(per_ip + 1):
            r = await create_run(self.c, brief="rate limit probe", brand="", aspect="16:9", n_scenes=3, variants=2,
                                 markets=[], product=None)
            if r.status_code == 429:
                body = r.json()
                check(set(body) == {"error"} and isinstance(body["error"], str) and body["error"],
                      f"429 body must be exactly {{'error': str}}, got {body}")
                check(r.headers.get("content-type", "").startswith("application/json"), "429 must be JSON")
                return f"429 after {created} more run(s): {body['error']!r}"
            check(r.status_code == 200, f"unexpected {r.status_code}: {r.text[:200]}")
            created += 1
        raise CheckFailed(f"no 429 after {created} runs (limit {per_ip}/h)")


# ========================================================================================== restart phase
async def restart_checks(client: httpx.AsyncClient, report: Report, run_id: str, timeout: float) -> None:
    """Run in a fresh server process: the finished run must be listed, served, and replayable."""
    t = E2E(client, report, timeout=timeout)
    t.run_id = run_id

    async def reload_state() -> str:
        st = await get_json(client, f"/api/runs/{run_id}")
        check(st["status"] == "done" and st["final"]["url"], f"reloaded run status {st['status']}")
        check(missing(RUN_KEYS, st, "run") == [], "reloaded state keys")
        t.state = st
        runs = await get_json(client, "/api/runs")
        check(any(r["id"] == run_id for r in runs), "reloaded run missing from /api/runs")
        return f"status={st['status']} final v{st['final']['version']}"

    async def live_history() -> str:
        w = EventWatcher(client, run_id)
        w.start()
        try:
            await w.wait_for(lambda ev: any(e["type"] == "run_done" for e in ev), 20, "history run_done")
            await asyncio.sleep(0.5)
            n = len(w.events)
        finally:
            await w.stop()
        check(n > 50, f"only {n} history events after restart")
        t._restart_hist = n  # type: ignore[attr-defined]
        return f"{n} history events re-sent on connect"

    await run_step(report, "restart: GET reloaded run", reload_state, fatal=True)
    await run_step(report, "restart: media still served", lambda: t.media_all(t.state))
    await run_step(report, "restart: SSE history", live_history)
    await run_step(report, "restart: replay=1",
                   lambda: t.replay(run_id, expect_min=getattr(t, "_restart_hist", 1)))

    async def action_after_restart() -> str:
        mark = 0
        w = EventWatcher(client, run_id)
        w.start()
        try:
            await w.wait_for(lambda ev: any(e["type"] == "run_done" for e in ev), 20, "history")
            mark = w.last_seq
            await post_json(client, f"/api/runs/{run_id}/music", {"instruction": "after restart"})
            fin = await w.wait_for(lambda ev: next((e for e in w.after(mark, "final")), None), timeout,
                                   "re-stitch after restart")
        finally:
            await w.stop()
        return f"re-score + re-stitch still work: final v{fin['version']}"

    await run_step(report, "restart: actions still work", action_after_restart)


# ========================================================================================== drivers
async def main_phase(client: httpx.AsyncClient, report: Report, args: argparse.Namespace,
                     gm: Any = None) -> str | None:
    t = E2E(client, report, timeout=args.timeout, gm=gm)
    try:
        await run_step(report, "health + static", t.health, fatal=True)
        await run_step(report, "transcribe", t.transcribe)
        await run_step(report, "launch 16:9 run (4x3 + product photo)", t.launch, fatal=True)
        await run_step(report, "initial loop event sequence", t.initial_loop, fatal=True)
        await run_step(report, "run state shape (CONTRACT §6)", t.state_shape, fatal=True)
        await run_step(report, "media URLs + content types", t.media_all)
        await run_step(report, "media Range + traversal safety", t.media_security)
        await run_step(report, "final duration vs clips (ffmpeg)", t.final_duration)
        await run_step(report, "input validation errors", t.act_validation)
        await run_step(report, "edit s1 -> v2 + re-score + re-stitch", t.act_edit)
        await run_step(report, "direct whole ad (fan-out + re-voice)", t.act_direct)
        await run_step(report, "re-voice s2 -> new line + re-stitch", t.act_voiceover)
        await run_step(report, "music re-score", t.act_music)
        await run_step(report, "select override -> re-render", t.act_select)
        await run_step(report, "regenerate s3", t.act_regenerate)
        await run_step(report, "localize 2 markets (+ narration + animatic)", t.act_localize)
        await run_step(report, "forced final", t.act_final)
        await run_step(report, "final state + all media re-check", t.final_state)
        await run_step(report, "runs list + showcase", t.listings)
        await run_step(report, "replay=1 SSE", t.replay)
        await run_step(report, "9:16 run (3x2, portrait)", t.portrait_run)
        if gm is not None:
            await run_step(report, "degraded run (Omni/Lyria/TTS failures)", t.degraded_run)
            await run_step(report, "director failure -> status=error", t.director_failure)
        else:
            report.note("degradation steps need --inprocess (they inject model failures)")
        if not args.skip_rate_limit:
            await run_step(report, "rate limit 429 JSON", t.rate_limit)
        if t.w:
            report.note(f"SSE reconnects during the main run: {t.w.reconnects}")
    except _Abort as exc:
        report.note(f"aborted after fatal step: {exc}")
    finally:
        if t.w:
            await t.w.stop()
    return t.run_id


def _restart_in_fresh_process(args: argparse.Namespace, run_id: str) -> bool:
    """Verify persistence the honest way: a brand-new interpreter loads the same data dir from disk."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--inprocess", "--phase", "restart", "--run-id", run_id,
           "--data-dir", args.data_dir, "--timeout", str(args.timeout)]
    print(f"\n--- restart persistence: fresh process\n$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT), env={**os.environ, "ADMATE_MOCK": "1"})
    return proc.returncode == 0


async def amain(args: argparse.Namespace) -> int:
    report = Report()
    t0 = time.perf_counter()
    limits = httpx.Timeout(args.timeout, connect=10.0)
    run_id: str | None = None
    if args.inprocess:
        # Configure the app *before* importing it: settings are read once at import time.
        os.environ["ADMATE_MOCK"] = "1"
        os.environ["ADMATE_DATA_DIR"] = args.data_dir
        os.environ.setdefault("ADMATE_MOCK_SPEED", str(args.mock_speed))
        os.environ.setdefault("ADMATE_RUNS_PER_IP_PER_HOUR", "6")
        os.environ.setdefault("ADMATE_MAX_CONCURRENT_RUNS", "8")
        sys.path.insert(0, str(ROOT))
        from app.main import app  # noqa: WPS433

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=StreamingASGITransport(app), base_url="http://admate.test",
                                         timeout=limits) as client:
                if args.phase == "restart":
                    await restart_checks(client, report, args.run_id, args.timeout)
                else:
                    run_id = await main_phase(client, report, args, gm=app.state.gm)
    else:
        async with httpx.AsyncClient(base_url=args.base, timeout=limits, trust_env=False) as client:
            if args.phase == "restart":
                await restart_checks(client, report, args.run_id, args.timeout)
            else:
                run_id = await main_phase(client, report, args)

    restart_ok: bool | None = None
    if args.phase == "main" and run_id and report.ok:
        if args.inprocess:
            restart_ok = _restart_in_fresh_process(args, run_id)
            report.steps.append(StepResult("restart persistence (fresh process)", restart_ok, 0.0,
                                           "see restart phase output above"))
        else:
            report.note(f"restart the server, then run: {sys.executable} {Path(__file__).name} --base {args.base} "
                        f"--phase restart --run-id {run_id}")
    report.print_summary()
    print(f"  total wall time {time.perf_counter() - t0:.1f}s")
    return 0 if report.ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--base", help="base URL of a running mock-mode server, e.g. http://localhost:8102")
    target.add_argument("--inprocess", action="store_true", help="serve app.main in-process (no TCP port)")
    ap.add_argument("--phase", choices=("main", "restart"), default="main")
    ap.add_argument("--run-id", help="run to verify in --phase restart")
    ap.add_argument("--timeout", type=float, default=120.0, help="per-wait timeout in seconds (default 120)")
    ap.add_argument("--data-dir", default=None, help="--inprocess data dir (default: fresh temp dir)")
    ap.add_argument("--mock-speed", type=float, default=0.5, help="--inprocess ADMATE_MOCK_SPEED (default 0.5)")
    ap.add_argument("--skip-rate-limit", action="store_true", help="skip the 429 step")
    args = ap.parse_args(argv)
    if args.phase == "restart" and not args.run_id:
        ap.error("--phase restart requires --run-id")
    if args.inprocess and not args.data_dir:
        if args.phase == "restart":
            ap.error("--phase restart --inprocess requires --data-dir")
        args.data_dir = tempfile.mkdtemp(prefix="admate_e2e_")
    if args.base:
        args.base = args.base.rstrip("/")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(parse_args())))
