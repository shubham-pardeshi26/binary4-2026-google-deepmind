#!/usr/bin/env python3
"""AdMate Campaign Kit (posters) end-to-end test -- mock mode, in-process ASGI (no TCP port, no network).

Drives the real ``app.main:app`` (with its lifespan) through a streaming in-process ASGI transport and asserts
POSTERS_CONTRACT.md §2/§4 end to end:

* storyboard done -> ``posters_copy`` -> 2 ``poster_variant`` per format -> one judged ``poster`` per format;
* ``state.posters`` shape (status/copy/items/variants/winner/score/rationale, started/done ms) and media URLs
  that serve real PNGs at each format's aspect ratio;
* ``POST .../posters/{format}/select`` -> ``poster`` with ``by: "user"``; validation errors are JSON ``{"error"}``;
* ``POST .../posters`` for one format with an instruction -> 2 new variants -> re-judged ``poster``;
* ``POST .../localize`` for one market -> ``localize_poster`` per format + ``localizations[m].posters``;
* ``GET .../kit.zip`` -> attachment ZIP containing README, plan.json, film, music, keyframes, posters and the
  localized posters;
* ``node --check static/posters.js`` (skipped with a note if the file or node is missing).

Until the §5 integration patch lands, the test wires the plugin itself (includes the router, registers
``studio.on_event`` via ``manager.observers`` if the pipeline has the hook, else by wrapping ``Run.emit`` -- in
this process only). Once the integration exists it detects it and skips self-wiring.

Usage::

    .venv/bin/python scripts/e2e_posters.py --inprocess --data-dir /tmp/admate_posters --mock-speed 1.0
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parent.parent
FORMATS = {"ig_square": "1:1", "ig_story": "9:16", "print_poster": "4:5", "web_banner": "16:9", "billboard": "21:9"}
POSTERS_KEYS = {"status", "copy", "items", "started_ms", "done_ms"}
ITEM_KEYS = {"format", "label", "aspect", "status", "variants", "winner", "score", "rationale", "error"}
VARIANT_KEYS = {"idx", "url", "latency_ms", "api_path"}
COPY_KEYS = {"headline", "subline", "cta", "art_direction"}
MARKET = "Tokyo · Japanese"


class CheckFailed(AssertionError):
    """A contract assertion failed."""


def check(cond: Any, msg: str) -> None:
    if not cond:
        raise CheckFailed(msg)


# ========================================================================================== transport
class _ASGIStream(httpx.AsyncByteStream):
    """Response body fed live from the ASGI app's ``send`` messages (true streaming for SSE)."""

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

    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": request.method,
            "scheme": request.url.scheme, "path": request.url.path, "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query, "root_path": "",
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "client": ("127.0.0.1", 51235), "server": (request.url.host, request.url.port or 80),
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
        return httpx.Response(first["status"], headers=list(first.get("headers", [])),
                              stream=_ASGIStream(queue, task, disconnected), request=request)


# ========================================================================================== SSE watcher
class EventWatcher:
    """Consumes ``/api/runs/{id}/events`` in the background (dedupe by ``seq``) and lets the test await events."""

    def __init__(self, client: httpx.AsyncClient, run_id: str) -> None:
        self.client, self.run_id = client, run_id
        self.events: list[dict] = []
        self._last_seq = 0
        self._cond = asyncio.Condition()
        self._task: asyncio.Task | None = None

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
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        ev = json.loads(line[5:].strip())
                        seq = int(ev.get("seq", 0))
                        if seq and seq <= self._last_seq:
                            continue
                        self._last_seq = max(self._last_seq, seq)
                        async with self._cond:
                            self.events.append(ev)
                            self._cond.notify_all()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect like EventSource
                pass
            await asyncio.sleep(0.3)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    async def wait_for(self, pred: Callable[[list[dict]], Any], timeout: float, what: str) -> Any:
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
            errs = [f"{e['type']}: {e.get('msg') or e.get('error')}" for e in self.events
                    if e["type"] in ("error", "poster_status") and (e.get("msg") or e.get("error"))][-4:]
            raise CheckFailed(f"timed out after {timeout:.0f}s waiting for {what}"
                              f"{' (recent: ' + '; '.join(errs) + ')' if errs else ''}") from None

    def after(self, seq: int, type_: str | None = None, **match: Any) -> list[dict]:
        return [e for e in self.events if e.get("seq", 0) > seq and (type_ is None or e["type"] == type_)
                and all(e.get(k) == v for k, v in match.items())]

    def of(self, type_: str, **match: Any) -> list[dict]:
        return self.after(0, type_, **match)


# ========================================================================================== helpers
def make_png(width: int = 320, height: int = 320) -> bytes:
    """A small valid RGB PNG (product photo stand-in)."""
    raw = b"".join(b"\x00" + b"".join(bytes((x * 255 // width, 90, y * 255 // height)) for x in range(width))
                   for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


def aspect_value(aspect: str) -> float:
    a, b = aspect.split(":")
    return float(a) / float(b)


def missing(keys: set[str], obj: Any, where: str) -> list[str]:
    if not isinstance(obj, dict):
        return [f"{where} is not an object"]
    return [f"{where}.{k}" for k in sorted(keys - obj.keys())]


# ========================================================================================== wiring
async def wire_plugin(app: Any, client: httpx.AsyncClient) -> str:
    """Self-wire the plugin into ``app`` unless the §5 integration already did. Returns a description.

    Router detection probes the endpoint itself (FastAPI versions differ in how included routers appear in
    ``app.router.routes``): the plugin answers an unknown run with ``{"error": "run not found"}``, the framework's
    catch-all 404 does not.
    """
    from app.pipeline import Run
    from app.posters import PosterStudio, router

    notes = []
    probe = await client.get("/api/runs/00000000/kit.zip")
    if probe.status_code == 404 and probe.headers.get("content-type", "").startswith("application/json") \
            and probe.json().get("error") == "run not found":
        notes.append("router already integrated")
    else:
        app.include_router(router)
        notes.append("router included by test")
    manager = app.state.manager
    studio = getattr(app.state, "posters", None)
    observers = getattr(manager, "observers", None)
    if studio is not None and isinstance(observers, list) and studio.on_event in observers:
        notes.append("observer already integrated")
        return ", ".join(notes)
    if studio is None:
        studio = PosterStudio(manager)
        app.state.posters = studio
    if isinstance(observers, list):
        observers.append(studio.on_event)
        notes.append("observer registered on manager.observers by test")
    else:
        original = Run.emit

        def emit(self, type_: str, **payload: Any) -> dict:  # test-only observer shim
            event = original(self, type_, **payload)
            try:
                studio.on_event(self, event)
            except Exception:  # noqa: BLE001
                pass
            return event

        Run.emit = emit
        notes.append("Run.emit wrapped by test")
    return ", ".join(notes)


# ========================================================================================== the test
async def main_test(client: httpx.AsyncClient, timeout: float) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    async def step(name: str, coro) -> Any:
        t0 = time.perf_counter()
        try:
            detail = await coro
            results.append((name, True, f"{detail or ''} ({time.perf_counter() - t0:.1f}s)"))
            print(f"  PASS  {name}: {detail or ''}", flush=True)
            return detail
        except Exception as exc:  # noqa: BLE001
            results.append((name, False, f"{exc.__class__.__name__}: {exc}"))
            print(f"  FAIL  {name}: {exc.__class__.__name__}: {exc}", flush=True)
            return None

    # ---- launch
    data = {"brief": "Irani chai café launch in Hyderabad's Old City — steaming glasses and Osmania biscuits",
            "brand": "Chai Nagar", "aspect": "16:9", "n_scenes": "3", "variants": "2", "markets": ""}
    r = await client.post("/api/runs", data=data, files={"product_image": ("product.png", make_png(), "image/png")})
    check(r.status_code == 200, f"POST /api/runs -> {r.status_code} {r.text[:200]}")
    run_id = r.json()["run_id"]
    print(f"  run {run_id}", flush=True)
    w = EventWatcher(client, run_id)
    w.start()
    try:
        async def kit() -> str:
            copy_ev = await w.wait_for(lambda ev: [e for e in ev if e["type"] == "posters_copy"], timeout,
                                       "posters_copy")
            copy = copy_ev[0]["copy"]
            check(not missing(COPY_KEYS, copy, "copy"), f"copy keys {missing(COPY_KEYS, copy, 'copy')}")
            check(len(copy["headline"].split()) <= 6 and len(copy["cta"].split()) <= 4, f"copy too long {copy}")
            check(set(copy["art_direction"]) >= set(FORMATS), "art_direction must cover every format")
            winners_seq = max(e["seq"] for e in w.of("winner"))
            check(copy_ev[0]["seq"] > winners_seq or len(w.of("winner")) >= 3, "kit must start after winners")
            await w.wait_for(lambda ev: {e["format"] for e in ev if e["type"] == "poster"} >= set(FORMATS),
                             timeout, "a poster per format")
            for fid in FORMATS:
                vs = w.of("poster_variant", format=fid)
                check(len(vs) == 2, f"{fid}: expected 2 poster_variant, got {len(vs)}")
                check({v["idx"] for v in vs} == {0, 1}, f"{fid}: variant idx {[v['idx'] for v in vs]}")
                for v in vs:
                    check(v["url"].startswith(f"/media/{run_id}/poster_{fid}_") and v["latency_ms"] >= 0
                          and v.get("api_path"), f"{fid}: bad poster_variant {v}")
                p = w.of("poster", format=fid)[0]
                check(p["by"] == "judge" and p["winner"] in (0, 1) and isinstance(p["score"], (int, float)),
                      f"{fid}: bad poster event {p}")
                check(p["label"] and p["aspect"] and p["rationale"], f"{fid}: poster missing label/aspect/rationale")
            await w.wait_for(lambda ev: [e for e in ev if e["type"] == "poster_status" and e.get("status") == "done"
                                         and "format" not in e and "market" not in e], timeout, "kit done")
            return f"copy {copy['headline']!r} / {copy['cta']!r}; 10 variants, 5 winners"

        await step("kit: copy + 2 variants/format + judged poster/format", kit())

        async def state_shape() -> str:
            st = (await client.get(f"/api/runs/{run_id}")).json()
            ps = st.get("posters")
            errs = missing(POSTERS_KEYS, ps, "posters")
            check(not errs, f"missing {errs}")
            check(ps["status"] == "done", f"posters.status {ps['status']}")
            check(ps["done_ms"] >= ps["started_ms"] > 0, f"started/done ms {ps['started_ms']}/{ps['done_ms']}")
            check([i["format"] for i in ps["items"]] == list(FORMATS), f"item order {[i['format'] for i in ps['items']]}")
            for it in ps["items"]:
                errs = missing(ITEM_KEYS, it, it.get("format", "item"))
                check(not errs, f"missing {errs}")
                check(it["status"] == "done" and it["winner"] is not None and it["error"] is None, f"item {it}")
                check(it["aspect"] == FORMATS[it["format"]], f"{it['format']} aspect {it['aspect']}")
                check(len(it["variants"]) == 2, f"{it['format']} variants {len(it['variants'])}")
                for v in it["variants"]:
                    check(not missing(VARIANT_KEYS, v, "variant"), f"variant keys {v}")
                    resp = await client.get(v["url"])
                    check(resp.status_code == 200 and resp.headers["content-type"].startswith("image/"),
                          f"GET {v['url']} -> {resp.status_code}")
                    size = png_size(resp.content)
                    check(size is not None, f"{v['url']} is not a PNG")
                    ratio = size[0] / size[1]
                    check(abs(ratio - aspect_value(it["aspect"])) / aspect_value(it["aspect"]) < 0.02,
                          f"{v['url']} is {size[0]}x{size[1]}, expected {it['aspect']}")
            return "state.posters shape OK; 10 PNGs at the right aspect ratios"

        await step("state shape + media", state_shape())

        async def select() -> str:
            st = (await client.get(f"/api/runs/{run_id}")).json()
            it = next(i for i in st["posters"]["items"] if i["format"] == "ig_square")
            other = 1 - it["winner"]
            seq = w.last_seq
            r = await client.post(f"/api/runs/{run_id}/posters/ig_square/select", json={"idx": other})
            check(r.status_code == 200 and r.json() == {"ok": True}, f"select -> {r.status_code} {r.text}")
            ev = await w.wait_for(lambda ev: [e for e in ev if e["seq"] > seq and e["type"] == "poster"
                                              and e["format"] == "ig_square"], timeout, "poster by=user")
            check(ev[0]["by"] == "user" and ev[0]["winner"] == other, f"select event {ev[0]}")
            st = (await client.get(f"/api/runs/{run_id}")).json()
            it = next(i for i in st["posters"]["items"] if i["format"] == "ig_square")
            check(it["winner"] == other, f"state winner {it['winner']} != {other}")
            # validation errors are JSON {"error"}
            for path, body, code in [(f"/api/runs/{run_id}/posters/nope/select", {"idx": 0}, 404),
                                     (f"/api/runs/{run_id}/posters/ig_square/select", {"idx": "x"}, 422),
                                     (f"/api/runs/{run_id}/posters/ig_square/select", {"idx": 99}, 404),
                                     ("/api/runs/deadbeef/posters/ig_square/select", {"idx": 0}, 404),
                                     (f"/api/runs/{run_id}/posters", {"formats": ["poster_xl"]}, 422),
                                     (f"/api/runs/{run_id}/posters", {"formats": "ig_square"}, 422)]:
                r = await client.post(path, json=body)
                check(r.status_code == code and isinstance(r.json().get("error"), str),
                      f"POST {path} {body} -> {r.status_code} {r.text[:120]} (expected {code} JSON error)")
            r = await client.get("/api/runs/deadbeef/kit.zip")
            check(r.status_code == 404 and "error" in r.json(), f"kit.zip unknown run -> {r.status_code}")
            return f"ig_square winner -> {other} (by=user); 7 validation errors are JSON"

        await step("select override + validation", select())

        async def regenerate() -> str:
            seq = w.last_seq
            r = await client.post(f"/api/runs/{run_id}/posters",
                                  json={"formats": ["web_banner"], "instruction": "dusk light, moodier, more contrast"})
            check(r.status_code == 200 and r.json() == {"ok": True}, f"regenerate -> {r.status_code} {r.text}")
            await w.wait_for(lambda ev: [e for e in ev if e["seq"] > seq and e["type"] == "poster"
                                         and e["format"] == "web_banner"], timeout, "re-judged web_banner")
            vs = w.after(seq, "poster_variant", format="web_banner")
            check(sorted(v["idx"] for v in vs) == [2, 3], f"new variant idx {[v['idx'] for v in vs]}")
            check(not w.after(seq, "poster_variant", format="ig_square"), "other formats must not regenerate")
            p = w.after(seq, "poster", format="web_banner")[0]
            check(p["winner"] in (2, 3) and p["by"] == "judge", f"regenerated winner {p}")
            st = (await client.get(f"/api/runs/{run_id}")).json()
            it = next(i for i in st["posters"]["items"] if i["format"] == "web_banner")
            check(len(it["variants"]) == 4 and it["winner"] in (2, 3), f"state after regenerate {it['winner']}")
            await w.wait_for(lambda ev: [e for e in ev if e["seq"] > seq and e["type"] == "poster_status"
                                         and e.get("status") == "done" and "format" not in e], timeout,
                             "regenerate done")
            check((await client.get(f"/api/runs/{run_id}")).json()["posters"]["status"] == "done", "status done")
            return f"web_banner variants 2,3 -> winner {p['winner']}"

        await step("regenerate one format with instruction", regenerate())

        async def localize() -> str:
            seq = w.last_seq
            r = await client.post(f"/api/runs/{run_id}/localize", json={"markets": [MARKET]})
            check(r.status_code == 200, f"localize -> {r.status_code} {r.text}")
            await w.wait_for(lambda ev: {e["format"] for e in ev if e["seq"] > seq and e["type"] == "localize_poster"
                                         and e["market"] == MARKET} >= set(FORMATS), timeout,
                             "localize_poster per format")
            for e in w.after(seq, "localize_poster", market=MARKET):
                check(e["url"].startswith(f"/media/{run_id}/poster_") and e["latency_ms"] >= 0, f"bad {e}")
                resp = await client.get(e["url"])
                check(resp.status_code == 200 and png_size(resp.content), f"GET {e['url']} -> {resp.status_code}")
            st = (await client.get(f"/api/runs/{run_id}")).json()
            loc = st["localizations"][MARKET]
            check([p["format"] for p in loc.get("posters", [])] == list(FORMATS), f"loc posters {loc.get('posters')}")
            check("scenes" in loc and "plan" in loc, "localize must not clobber the pipeline's market keys")
            await w.wait_for(lambda ev: [e for e in ev if e["seq"] > seq and e["type"] == "localize_status"
                                         and e["market"] == MARKET and e["status"] in ("done", "error")],
                             timeout, "pipeline localize done")
            return f"{MARKET}: 5 localized posters"

        await step("localize one market", localize())

        async def kit_zip() -> str:
            await w.wait_for(lambda ev: [e for e in ev if e["type"] == "final"], timeout * 2, "final cut")
            r = await client.get(f"/api/runs/{run_id}/kit.zip")
            check(r.status_code == 200, f"kit.zip -> {r.status_code} {r.text[:200]}")
            check(r.headers["content-type"].startswith("application/zip"), f"content-type {r.headers['content-type']}")
            check("attachment" in r.headers.get("content-disposition", ""), "kit.zip must be an attachment")
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            check(zf.testzip() is None, "corrupt zip")
            names = zf.namelist()
            need = {"README.txt", "plan.json"}
            check(need <= set(names), f"missing {need - set(names)}")
            groups = {g: [n for n in names if n.startswith(g)] for g in
                      ("film/", "music/", "keyframes/", "posters/", "localized/")}
            check(any(n.endswith(".mp4") for n in groups["film/"]), f"no film mp4 in {names}")
            check(groups["music/"], "no music")
            check(len(groups["keyframes/"]) == 3, f"keyframes {groups['keyframes/']}")
            check(len(groups["posters/"]) == 5, f"posters {groups['posters/']}")
            loc_posters = [n for n in groups["localized/"] if "/posters/" in n]
            check(len(loc_posters) == 5, f"localized posters {loc_posters}")
            readme = zf.read("README.txt").decode()
            check("posters/" in readme and "Headline" in readme, "README must list files and poster copy")
            plan = json.loads(zf.read("plan.json"))
            check(plan.get("plan", {}).get("campaign_name"), "plan.json must hold the plan")
            return f"{len(names)} files, {len(r.content) / 1e6:.1f} MB"

        await step("kit.zip", kit_zip())
    finally:
        await w.stop()
    return results


def node_check() -> tuple[str, bool, str]:
    js = ROOT / "static" / "posters.js"
    node = shutil.which("node")
    if not js.exists():
        return ("node --check static/posters.js", True, "SKIPPED: static/posters.js not present yet")
    if not node:
        return ("node --check static/posters.js", True, "SKIPPED: node not installed")
    proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    return ("node --check static/posters.js", proc.returncode == 0, (proc.stderr or "syntax OK").strip()[:300])


async def amain(args: argparse.Namespace) -> int:
    os.environ["ADMATE_MOCK"] = "1"
    os.environ["ADMATE_DATA_DIR"] = args.data_dir
    os.environ["ADMATE_MOCK_SPEED"] = str(args.mock_speed)
    os.environ.setdefault("ADMATE_RUNS_PER_IP_PER_HOUR", "0")
    sys.path.insert(0, str(ROOT))
    from app.main import app  # noqa: WPS433 - settings are read at import time

    t0 = time.perf_counter()
    print(f"AdMate posters e2e · mock speed {args.mock_speed} · data {args.data_dir}", flush=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=StreamingASGITransport(app), base_url="http://admate.test",
                                     timeout=httpx.Timeout(args.timeout, connect=10.0)) as client:
            print(f"  wiring: {await wire_plugin(app, client)}", flush=True)
            results = await main_test(client, args.timeout)
    res = node_check()
    print(f"  {'PASS' if res[1] else 'FAIL'}  {res[0]}: {res[2]}")
    results.append(res)
    ok = all(r[1] for r in results)
    print(f"\n{'PASS' if ok else 'FAIL'}: {sum(r[1] for r in results)}/{len(results)} checks · "
          f"{time.perf_counter() - t0:.1f}s")
    return 0 if ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--inprocess", action="store_true", default=True,
                    help="serve app.main in-process (the only supported mode; flag kept for symmetry)")
    ap.add_argument("--data-dir", default=None, help="data dir (default: fresh temp dir)")
    ap.add_argument("--mock-speed", type=float, default=1.0, help="ADMATE_MOCK_SPEED (default 1.0)")
    ap.add_argument("--timeout", type=float, default=90.0, help="per-wait timeout in seconds (default 90)")
    args = ap.parse_args(argv)
    args.data_dir = args.data_dir or tempfile.mkdtemp(prefix="admate_posters_e2e_")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(parse_args())))
