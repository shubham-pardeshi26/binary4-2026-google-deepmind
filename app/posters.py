"""Campaign Kit plugin: Nano Banana 2 Lite posters for every format, judged, localized and zipped.

Self-contained by design (POSTERS_CONTRACT.md): it never touches the pipeline's internals, it only *observes* run
events and uses the stable :class:`~app.pipeline.Run` helpers (``gm``, ``emit``, ``save``, ``save_asset``,
``read_url``, ``spawn``, ``log``, ``anchor_bytes``, ``product_image``, ``plan``, ``state``).

Flow::

    winner (every scene has one) ──> poster_copy (Flash) ──┬─> ig_square:    2 NB2 variants ─> judge ─> poster
                                                           ├─> ig_story:     2 NB2 variants ─> judge ─> poster
                                                           ├─> print_poster: ...
                                                           ├─> web_banner:   ...
                                                           └─> billboard:    ...
    localize_plan (market) ──> wait for kit ──> transcreate copy (Flash) ──> NB2 edit of every winning poster

Everything runs in the background through :meth:`Run.spawn`, wrapped in a guard that turns *any* exception into
``poster_status: error`` + ``log`` events -- the posters can never break the run. The storyboard is finished when
the kit starts while the Omni clips are still rendering, so the whole kit adds ~zero wall-clock to the demo.

Public surface:

* :class:`PosterStudio` -- ``on_event(run, event)`` observer + actions (regenerate / select / localize).
* :data:`router` -- ``POST /api/runs/{id}/posters``, ``POST /api/runs/{id}/posters/{format}/select``,
  ``GET /api/runs/{id}/kit.zip``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import re
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from app import media
from app import poster_prompts as PP
from app.genai_client import img_part

log = logging.getLogger("admate.posters")

#: Variants rendered per format (in parallel).
VARIANTS_PER_FORMAT = 2
#: Maximum characters kept from a user instruction.
MAX_INSTRUCTION_CHARS = 600
#: How long a ``localize_plan`` waits for the kit to finish before giving up (seconds).
KIT_WAIT_TIMEOUT_S = 900.0
_ASPECT_ERR_RE = re.compile(r"aspect|ratio|invalid[ _]?argument|unsupported", re.I)
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}$")


# --------------------------------------------------------------------------------------------- helpers
def _now_ms(run: Any) -> int:
    """Milliseconds since run start (same clock as event ``t``)."""
    try:
        return int(run.bus.now_ms())
    except Exception:  # noqa: BLE001
        return int(max(0.0, time.time() - float(run.state.get("created_at") or time.time())) * 1000)


def _err_text(exc: BaseException | str, limit: int = 240) -> str:
    """Short, UI-safe error message (credentials redacted)."""
    msg = exc if isinstance(exc, str) else (str(exc).strip() or exc.__class__.__name__)
    msg = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[redacted]", msg)
    return msg if len(msg) <= limit else msg[: limit - 1] + "…"


def market_slug(text: str, limit: int = 24) -> str:
    """Filesystem-safe market slug; identical algorithm to the pipeline's localized asset names."""
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (s[:limit].strip("-") or "market") + "-" + format(zlib.crc32(str(text).encode()) & 0xFFFF, "04x")


def _winner_variant(sc: dict) -> dict | None:
    if sc.get("winner") is None:
        return None
    return next((v for v in sc.get("variants", []) if v.get("idx") == sc["winner"]), None)


def all_scenes_have_winner(run: Any) -> bool:
    """True once the storyboard is finished (every scene has a winning keyframe)."""
    scenes = run.state.get("scenes") or []
    return bool(scenes) and bool(run.plan) and all(_winner_variant(sc) for sc in scenes)


def _default_item(fmt: dict) -> dict:
    return {"format": fmt["id"], "label": fmt["label"], "aspect": fmt["aspect"], "status": "idle", "variants": [],
            "winner": None, "score": None, "rationale": "", "error": None}


def _clip_instruction(text: Any) -> str | None:
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text[:MAX_INSTRUCTION_CHARS] or None


# --------------------------------------------------------------------------------------------- mock renderer
_UNICODE_FONTS = ["/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf",
                  "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def _font(size: int, text: str = ""):
    """A TrueType font of ``size`` px; a wide-coverage Unicode face for non-Latin copy."""
    from PIL import ImageFont

    if any(ord(ch) > 0x24F for ch in text):
        for path in _UNICODE_FONTS:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        from app.mock import _font as mock_font  # the studio's own mock face (read-only reuse)

        return mock_font(size)
    except Exception:  # noqa: BLE001
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def _rgb(hx: str) -> tuple[int, int, int]:
    return int(hx[1:3], 16), int(hx[3:5], 16), int(hx[5:7], 16)


def mock_canvas(aspect: str, long_side: int = 1152) -> tuple[int, int]:
    """Pixel size (even) for an ``a:b`` aspect with the given long side."""
    try:
        a, b = (float(x) for x in aspect.split(":"))
    except ValueError:
        a, b = 16.0, 9.0
    if a >= b:
        w, h = long_side, long_side * b / a
    else:
        w, h = long_side * a / b, long_side
    return int(round(w / 2) * 2), int(round(h / 2) * 2)


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def _fit(draw, text: str, max_w: int, max_h: int, size: int, min_size: int, max_lines: int):
    """Largest font (≤ ``size``) whose wrapped ``text`` fits the box; returns (font, lines, line_h)."""
    size = max(size, min_size)
    while True:
        font = _font(size, text)
        lines = _wrap(draw, text, font, max_w)
        line_h = int(size * 1.12)
        if (len(lines) <= max_lines and line_h * len(lines) <= max_h and
                all(draw.textlength(ln, font=font) <= max_w for ln in lines)) or size <= min_size:
            return font, lines, line_h
        size = max(min_size, int(size * 0.9))


def compose_mock_poster(base: bytes, *, aspect: str, fmt: dict, copy: dict, plan: dict, take: int,
                        tag: str = "MOCK · NANO BANANA 2 LITE POSTER") -> bytes:
    """Offline stand-in for an NB2 poster: the mock keyframe re-composed at the exact aspect with real
    typography (headline / subline / CTA pill / wordmark) in the brand palette. Two layouts (``take`` 0/1)."""
    from PIL import Image, ImageDraw, ImageFilter

    W, H = mock_canvas(aspect)
    roles = PP.palette_roles(plan)
    field_c, ink, accent, accent_ink = (_rgb(roles[k]) for k in ("field", "ink", "accent", "accent_ink"))
    try:
        src = Image.open(io.BytesIO(base)).convert("RGB")
    except Exception:  # noqa: BLE001
        src = Image.new("RGB", (W, H), accent)
    portrait, wide = H > W * 1.05, W > H * 1.6

    def cover(img, w: int, h: int):
        s = max(w / img.width, h / img.height)
        r = img.resize((max(1, math.ceil(img.width * s)), max(1, math.ceil(img.height * s))), Image.LANCZOS)
        left, top = (r.width - w) // 2, (r.height - h) // 2
        # Soft-focus the placeholder so the mock keyframe's burned-in labels read as bokeh, not competing text.
        return r.crop((left, top, left + w, top + h)).filter(ImageFilter.GaussianBlur(max(6, min(w, h) // 28)))

    canvas = Image.new("RGB", (W, H), field_c)
    m = int(min(W, H) * 0.07)
    if take % 2 == 0:  # type-led: brand field + framed image block
        if portrait:
            box = (m, int(H * (0.16 if fmt["id"] == "ig_story" else 0.06)), W - m, int(H * 0.58))
        else:
            box = (int(W * (0.46 if not wide else 0.52)), m, W - m, H - m)
        canvas.paste(cover(src, box[2] - box[0], box[3] - box[1]), box[:2])
        text_box = ((m, box[3] + m // 2, W - m, int(H * (0.80 if fmt["id"] == "ig_story" else 0.94)))
                    if portrait else (m, m, box[0] - m, H - m))
    else:  # image-led: full bleed + brand-colour scrim behind the type
        canvas = cover(src, W, H)
        scrim = Image.new("L", (W, H), 0)
        sd = ImageDraw.Draw(scrim)
        if portrait:
            for y in range(H // 2, H):
                sd.line([(0, y), (W, y)], fill=int(235 * (y - H / 2) / (H / 2)))
            text_box = (m, int(H * 0.52), W - m, int(H * (0.80 if fmt["id"] == "ig_story" else 0.94)))
        else:
            for x in range(0, int(W * 0.62)):
                sd.line([(x, 0), (x, H)], fill=int(235 * (1 - x / (W * 0.62))))
            text_box = (m, m, int(W * (0.46 if not wide else 0.5)), H - m)
        canvas = Image.composite(Image.new("RGB", (W, H), field_c), canvas, scrim.filter(ImageFilter.GaussianBlur(8)))
    draw = ImageDraw.Draw(canvas)
    x0, y0, x1, y1 = text_box
    bw, bh = max(40, x1 - x0), max(40, y1 - y0)
    head_font, head_lines, head_lh = _fit(draw, copy["headline"], bw, int(bh * 0.55), int(H * (0.2 if wide else 0.1)),
                                          14, 3)
    y = y0
    for ln in head_lines:
        draw.text((x0, y), ln, font=head_font, fill=ink)
        y += head_lh
    if fmt.get("subline", True) and copy.get("subline"):
        sub_size = max(12, int(head_font.size * 0.38)) if hasattr(head_font, "size") else 16
        sub_font, sub_lines, sub_lh = _fit(draw, copy["subline"], bw, int(bh * 0.25), sub_size, 10, 2)
        y += int(head_lh * 0.25)
        for ln in sub_lines:
            draw.text((x0, y), ln, font=sub_font, fill=ink)
            y += sub_lh
    cta_size = max(12, int(H * (0.05 if wide else 0.028)))
    cta_font = _font(cta_size, copy["cta"])
    tw = draw.textlength(copy["cta"], font=cta_font)
    pad_x, pad_y = int(cta_size * 0.9), int(cta_size * 0.5)
    y += int(cta_size * 0.8)
    draw.rounded_rectangle([x0, y, x0 + tw + 2 * pad_x, y + cta_size + 2 * pad_y], radius=cta_size, fill=accent)
    draw.text((x0 + pad_x, y + pad_y), copy["cta"], font=cta_font, fill=accent_ink)
    name = PP.brand_name(plan)
    if name:
        wf = _font(max(11, int(min(W, H) * 0.035)), name)
        draw.text((W - m, H - int(m * 0.6)), name, font=wf, fill=ink, anchor="rs")
    draw.text((int(m * 0.5), int(m * 0.35)), tag, font=_font(max(10, int(min(W, H) * 0.018))),
              fill=(255, 255, 255))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------------------------- runtime
@dataclass
class _KitRuntime:
    """Per-run, in-memory coordination (never persisted)."""

    started: bool = False                       # a full kit has been started in this process (or finished before)
    active: int = 0                             # running kit/regenerate jobs
    kit_done: asyncio.Event = field(default_factory=asyncio.Event)
    format_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    next_idx: dict[str, int] = field(default_factory=dict)
    copy_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def lock(self, fid: str) -> asyncio.Lock:
        return self.format_locks.setdefault(fid, asyncio.Lock())


class PosterStudio:
    """Campaign Kit orchestrator. One instance per server; observes every run's events."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._rt: dict[str, _KitRuntime] = {}
        #: Aspect actually accepted by the image model per format (after a rejection fallback).
        self._aspect_used: dict[str, str] = {}

    # ------------------------------------------------------------------ state
    def state(self, run: Any) -> dict:
        """``run.state["posters"]``, created (and every format item ensured) if missing."""
        st = run.state.get("posters")
        if not isinstance(st, dict):
            st = {"status": "idle", "copy": None, "items": [], "started_ms": 0, "done_ms": 0}
            run.state["posters"] = st
        st.setdefault("status", "idle")
        st.setdefault("copy", None)
        st.setdefault("started_ms", 0)
        st.setdefault("done_ms", 0)
        items = [i for i in st.get("items") or [] if isinstance(i, dict) and i.get("format") in PP.FORMAT_BY_ID]
        have = {i["format"] for i in items}
        items += [_default_item(f) for f in PP.FORMATS if f["id"] not in have]
        items.sort(key=lambda i: PP.FORMAT_IDS.index(i["format"]))
        st["items"] = items
        return st

    def _item(self, run: Any, fid: str) -> dict:
        return next(i for i in self.state(run)["items"] if i["format"] == fid)

    def runtime(self, run: Any) -> _KitRuntime:
        """Per-run runtime; on first sight of a run, work interrupted by a restart is marked as such."""
        rt = self._rt.get(run.id)
        if rt is None:
            rt = self._rt[run.id] = _KitRuntime()
            st = self.state(run)
            for it in st["items"]:
                rt.next_idx[it["format"]] = max([v.get("idx", -1) for v in it["variants"]] + [-1]) + 1
                if it["status"] == "rendering":
                    it["status"] = "done" if it.get("winner") is not None else "error"
                    it["error"] = None if it.get("winner") is not None else "interrupted by server restart"
            if st["status"] == "rendering":
                st["status"] = "done" if any(i.get("winner") is not None for i in st["items"]) else "error"
            if st["status"] in ("done", "error"):
                rt.started = True
                rt.kit_done.set()
        return rt

    # ------------------------------------------------------------------ observer
    def on_event(self, run: Any, event: dict) -> None:
        """Run-event observer (sync, never raises, never blocks -- work is spawned)."""
        try:
            etype = event.get("type") if isinstance(event, dict) else None
            if etype not in ("winner", "localize_plan"):
                return
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return
            rt = self.runtime(run)
            if etype == "winner":
                if not rt.started and self.state(run)["status"] == "idle" and all_scenes_have_winner(run):
                    self.start_kit(run)
            elif etype == "localize_plan":
                market = str(event.get("market") or "").strip()
                if market:
                    plan = event.get("plan") if isinstance(event.get("plan"), dict) else {}
                    self._spawn(run, self._localize(run, market, plan), what=f"localize {market}", market=market)
        except Exception:  # noqa: BLE001 - an observer must never break Run.emit
            log.exception("poster observer failed")

    # ------------------------------------------------------------------ actions
    def start_kit(self, run: Any, formats: list[str] | None = None, instruction: str | None = None) -> None:
        """Start the full kit (``formats=None``) or regenerate the given formats in the background."""
        rt = self.runtime(run)
        full = not formats
        if full:
            rt.started = True
        fids = [f for f in PP.FORMAT_IDS if full or f in formats]
        self._spawn(run, self._kit(run, fids, _clip_instruction(instruction), full=full), what="campaign kit")

    def select(self, run: Any, fid: str, idx: int) -> bool:
        """User override of a format's winning variant. Returns False if the variant does not exist."""
        it = self._item(run, fid)
        var = next((v for v in it["variants"] if v.get("idx") == idx), None)
        if var is None:
            return False
        it.update({"winner": idx, "score": var.get("score"), "rationale": "Selected by you.", "status": "done",
                   "error": None})
        run.emit("poster", format=fid, label=it["label"], aspect=it["aspect"], winner=idx, url=var["url"],
                 score=var.get("score"), rationale=it["rationale"], by="user")
        run.save()
        return True

    # ------------------------------------------------------------------ plumbing
    def _spawn(self, run: Any, coro: Coroutine, *, what: str, market: str | None = None) -> None:
        """Run ``coro`` via ``run.spawn`` inside a guard: any exception -> ``poster_status`` error + ``log``."""

        async def guarded() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the plugin must never crash a run
                log.exception("run %s: %s failed", run.id, what)
                msg = _err_text(exc)
                payload: dict[str, Any] = {"status": "error", "error": f"{what}: {msg}"}
                if market:
                    payload["market"] = market
                run.emit("poster_status", **payload)
                run.log("error", f"{what} failed: {msg}")
                run.save()

        run.spawn(guarded(), stage="posters")

    async def _refs(self, run: Any) -> tuple[bytes | None, bytes | None, bytes | None]:
        """(hero keyframe, anchor, product photo) continuity references."""
        sc = PP.hero_scene(run.plan, run.state.get("scenes") or [])
        var = _winner_variant(sc) if sc else None
        hero = await run.read_url(var["url"]) if var else None
        try:
            anchor = await run.anchor_bytes()
        except Exception:  # noqa: BLE001
            anchor = None
        return hero, anchor, getattr(run, "product_image", None)

    async def _copy(self, run: Any) -> dict:
        """One quick Flash call: headline / subline / CTA + per-format art direction (``posters_copy``)."""
        st = self.state(run)
        rt = self.runtime(run)
        async with rt.copy_lock:
            if st.get("copy"):
                return st["copy"]
            plan = run.plan or {}
            try:
                raw, _ = await run.gm.generate_json(PP.copy_parts(plan), PP.COPY_SCHEMA, system=PP.COPY_SYSTEM,
                                                    temperature=0.7, mock_context={"kind": "poster_copy"})
            except Exception as exc:  # noqa: BLE001 - fall back to the plan's own lines
                run.log("warn", f"poster copy fell back to the plan's tagline/CTA: {_err_text(exc)}")
                raw = {}
            copy = PP.normalize_copy(raw, plan)
            st["copy"] = copy
            run.emit("posters_copy", copy=copy)
            run.save()
            return copy

    # ------------------------------------------------------------------ kit
    async def _kit(self, run: Any, fids: list[str], instruction: str | None, *, full: bool) -> None:
        st = self.state(run)
        rt = self.runtime(run)
        rt.active += 1
        if full:
            rt.kit_done.clear()
        st["status"] = "rendering"
        if full or not st.get("started_ms"):
            st["started_ms"] = _now_ms(run)
        for fid in fids:
            it = self._item(run, fid)
            it.update({"status": "rendering", "error": None})
        run.emit("poster_status", status="start", formats=fids, **({"instruction": instruction} if instruction else {}))
        run.save()
        t0 = time.perf_counter()
        try:
            copy = await self._copy(run)
            refs = await self._refs(run)
            await asyncio.gather(*(self._format(run, fid, copy, refs, instruction) for fid in fids))
        finally:
            rt.active -= 1
            if rt.active == 0:
                ok = any(i.get("winner") is not None for i in st["items"])
                st["status"] = "done" if ok else "error"
                st["done_ms"] = _now_ms(run)
                n = sum(len(i["variants"]) for i in st["items"])
                run.emit("poster_status", status="done" if ok else "error",
                         **({} if ok else {"error": "no poster could be rendered"}))
                run.log("info" if ok else "warn", f"campaign kit {'ready' if ok else 'failed'}: {n} poster variants, "
                                                  f"{time.perf_counter() - t0:.1f}s")
                rt.kit_done.set()
                run.save()
            try:
                run.touch_metrics()
            except Exception:  # noqa: BLE001
                pass

    def _aspect_chain(self, fmt: dict) -> list[str]:
        start = self._aspect_used.get(fmt["id"], fmt["aspect"])
        chain = [a for a in fmt["fallbacks"] if a in PP.SUPPORTED_ASPECTS]
        return chain[chain.index(start):] if start in chain else [start] + chain

    async def _render(self, run: Any, fmt: dict, prompt_for: Any, refs: list[bytes]) -> tuple[Any, str]:
        """NB2 call with aspect fallback: returns (GenResult, aspect actually used)."""
        chain = self._aspect_chain(fmt)
        last: Exception | None = None
        for i, aspect in enumerate(chain):
            try:
                res = await run.gm.generate_image(prompt_for(aspect), refs=refs, aspect=aspect, size="1K")
            except Exception as exc:  # noqa: BLE001
                last = exc
                if i + 1 < len(chain) and _ASPECT_ERR_RE.search(str(exc)):
                    run.log("warn", f"poster {fmt['id']}: aspect {aspect} rejected, falling back to {chain[i + 1]}")
                    continue
                raise
            if aspect != fmt["aspect"] and self._aspect_used.get(fmt["id"]) != aspect:
                self._aspect_used[fmt["id"]] = aspect
            return res, aspect
        raise last or RuntimeError("no aspect ratio accepted")

    async def _format(self, run: Any, fid: str, copy: dict, refs: tuple, instruction: str | None) -> None:
        """Two variants in parallel -> poster judge -> winner, for one format (serialized per format)."""
        fmt = PP.FORMAT_BY_ID[fid]
        rt = self.runtime(run)
        async with rt.lock(fid):
            it = self._item(run, fid)
            it.update({"status": "rendering", "error": None})
            run.emit("poster_status", status="start", format=fid)
            plan = run.plan or {}
            hero, anchor, product = refs
            ref_list = [r for r in (hero, anchor, product) if r]
            base = rt.next_idx.get(fid, 0)
            rt.next_idx[fid] = base + VARIANTS_PER_FORMAT

            async def one(idx: int) -> tuple[dict, bytes] | None:
                take = idx % len(PP.TAKES)

                def prompt_for(aspect: str) -> str:
                    return PP.poster_prompt(plan, copy, fmt, take=take, aspect=aspect, has_hero=bool(hero),
                                            has_anchor=bool(anchor), has_product=bool(product),
                                            instruction=instruction)

                try:
                    res, aspect = await self._render(run, fmt, prompt_for, ref_list)
                    data, mime = res.data, res.mime_type
                    if not data:
                        raise RuntimeError("the image model returned no image")
                    if getattr(run.gm, "mock", False):
                        data = await asyncio.to_thread(compose_mock_poster, data, aspect=aspect, fmt=fmt, copy=copy,
                                                       plan=plan, take=take)
                        mime = "image/png"
                    url = await run.save_asset(f"poster_{fid}_{idx}{media.ext_for_mime(mime)}", data)
                except Exception as exc:  # noqa: BLE001 - one failed variant must not sink the format
                    run.log("warn", f"poster {fid} variant {idx} failed: {_err_text(exc)}")
                    return None
                if aspect != it["aspect"]:
                    it.setdefault("aspect_requested", fmt["aspect"])
                    it["aspect"] = aspect
                var = {"idx": idx, "url": url, "latency_ms": int(res.latency_ms), "api_path": res.api_path,
                       "take": PP.TAKES[take][0], "score": None}
                it["variants"].append(var)
                it["variants"].sort(key=lambda v: v["idx"])
                run.emit("poster_variant", format=fid, idx=idx, url=url, latency_ms=var["latency_ms"],
                         api_path=res.api_path)
                run.save()
                try:
                    run.touch_metrics()
                except Exception:  # noqa: BLE001
                    pass
                return var, data

            landed = [r for r in await asyncio.gather(*(one(base + k) for k in range(VARIANTS_PER_FORMAT))) if r]
            if not landed:
                msg = "both poster variants failed to render"
                it.update({"status": "error" if it.get("winner") is None else "done", "error": msg})
                run.emit("poster_status", status="error", format=fid, error=msg)
                run.save()
                return
            winner, score, rationale = await self._judge(run, fmt, copy, hero, landed)
            it.update({"winner": winner, "score": score, "rationale": rationale, "status": "done", "error": None})
            win = next(v for v in it["variants"] if v["idx"] == winner)
            run.emit("poster", format=fid, label=it["label"], aspect=it["aspect"], winner=winner, url=win["url"],
                     score=score, rationale=rationale, by="judge")
            run.emit("poster_status", status="done", format=fid)
            run.save()

    async def _judge(self, run: Any, fmt: dict, copy: dict, hero: bytes | None,
                     landed: list[tuple[dict, bytes]]) -> tuple[int, float | None, str]:
        """Poster-rubric judge over the freshly rendered variants -> (global winner idx, score, rationale)."""
        if len(landed) == 1:
            return landed[0][0]["idx"], None, "Only this variant rendered successfully."
        plan = run.plan or {}
        aspect = self._item(run, fmt["id"])["aspect"]
        parts = PP.judge_parts(plan, copy, fmt, aspect, hero, [d for _, d in landed], img_part)
        ctx = {"kind": "judge", "plan": plan, "scene": {"id": f"poster-{fmt['id']}", "beat": "poster"},
               "n_variants": len(landed)}
        try:
            raw, _ = await run.gm.generate_json(parts, PP.JUDGE_SCHEMA, system=PP.JUDGE_SYSTEM, temperature=0.2,
                                                mock_context=ctx)
        except Exception as exc:  # noqa: BLE001
            run.log("warn", f"poster judge {fmt['id']} failed, keeping the first variant: {_err_text(exc)}")
            return landed[0][0]["idx"], None, "Judge unavailable — first variant kept."
        verdict = PP.normalize_judgement(raw, len(landed))
        for (var, _), sc in zip(landed, verdict["scores"]):
            var["score"] = sc["overall"]
            var["scores"] = {k: sc[k] for k in ("legibility", "brand", "composition", "impact")}
            var["notes"] = sc["notes"]
        win_var = landed[verdict["winner_index"]][0]
        rationale = re.sub(r"\bVariant (\d+)\b",
                           lambda m: f"Variant {landed[int(m.group(1))][0]['idx']}"
                           if int(m.group(1)) < len(landed) else m.group(0), verdict["rationale"])
        return win_var["idx"], win_var["score"], rationale

    # ------------------------------------------------------------------ localization
    async def _localize(self, run: Any, market: str, lplan: dict) -> None:
        """Wait for the kit, transcreate the copy, then NB2-edit every winning poster into the market's script."""
        rt = self.runtime(run)
        st = self.state(run)
        if not rt.started and st["status"] == "idle" and all_scenes_have_winner(run):
            self.start_kit(run)
        try:
            await asyncio.wait_for(rt.kit_done.wait(), KIT_WAIT_TIMEOUT_S)
        except asyncio.TimeoutError:
            run.log("warn", f"localized posters for {market} skipped: the campaign kit was not ready in time")
            return
        winners = [it for it in st["items"] if it.get("winner") is not None]
        copy = st.get("copy")
        if not winners or not copy:
            run.log("warn", f"localized posters for {market} skipped: no winning posters")
            return
        locs = run.state.setdefault("localizations", {})
        entry = locs.setdefault(market, {"status": "", "plan": lplan, "scenes": [], "music_url": None})
        entry["posters"] = []
        run.save()
        plan = run.plan or {}
        try:
            raw, _ = await run.gm.generate_json(PP.localize_copy_parts(copy, market, lplan), PP.LOCALIZE_COPY_SCHEMA,
                                                system=PP.LOCALIZE_COPY_SYSTEM, temperature=0.4,
                                                mock_context={"kind": "poster_localize"})
        except Exception as exc:  # noqa: BLE001
            run.log("warn", f"poster transcreation for {market} fell back to the film's lines: {_err_text(exc)}")
            raw = {}
        loc = PP.normalize_localized_copy(raw, copy, lplan)
        entry["poster_copy"] = loc
        product = getattr(run, "product_image", None)
        slug = market_slug(market)
        hero_mock = (await self._refs(run))[0] if getattr(run.gm, "mock", False) else None

        async def one(it: dict) -> None:
            fmt = PP.FORMAT_BY_ID[it["format"]]
            win = next((v for v in it["variants"] if v["idx"] == it["winner"]), None)
            base = await run.read_url(win["url"]) if win else None
            if base is None:
                return
            prompt = PP.localize_poster_prompt(plan, copy, loc, fmt, market, has_product=bool(product))
            try:
                res = await run.gm.generate_image(prompt, refs=[r for r in (base, product) if r], aspect=it["aspect"],
                                                  size="1K")
                data, mime = res.data, res.mime_type
                if not data:
                    raise RuntimeError("the image model returned no image")
                if getattr(run.gm, "mock", False):
                    take = next((i for i, t in enumerate(PP.TAKES) if t[0] == win.get("take")), win["idx"] % 2)
                    data = await asyncio.to_thread(compose_mock_poster, hero_mock or data, aspect=it["aspect"],
                                                   fmt=fmt, copy=loc, plan=plan, take=take,
                                                   tag=f"MOCK · NB2 LITE · {market}")
                    mime = "image/png"
                url = await run.save_asset(f"poster_{slug}_{it['format']}{media.ext_for_mime(mime)}", data)
            except Exception as exc:  # noqa: BLE001
                run.log("warn", f"localized poster {market} {it['format']} failed: {_err_text(exc)}")
                return
            rec = {"format": it["format"], "url": url, "latency_ms": int(res.latency_ms)}
            entry["posters"] = [p for p in entry.get("posters", []) if p.get("format") != it["format"]] + [rec]
            entry["posters"].sort(key=lambda p: PP.FORMAT_IDS.index(p["format"]))
            run.emit("localize_poster", market=market, format=it["format"], url=url, latency_ms=rec["latency_ms"])
            run.save()

        await asyncio.gather(*(one(it) for it in winners))
        if not entry["posters"]:
            run.emit("poster_status", status="error", market=market, error=f"no localized posters for {market}")
        run.save()

    # ------------------------------------------------------------------ kit.zip
    def build_zip(self, run: Any) -> tuple[Path, str]:
        """Write the campaign kit ZIP to a temp file (call in a thread). Returns (path, download filename)."""
        st = json.loads(json.dumps(run.state, default=str))  # snapshot: the loop may mutate state meanwhile
        prefix = f"/media/{run.id}/"

        def path_of(url: Any) -> Path | None:
            if not isinstance(url, str) or not url.startswith(prefix):
                return None
            name = url[len(prefix):]
            if "/" in name or name.startswith(".") or name in ("run.json", "events.jsonl"):
                return None
            p = run.dir / name
            return p if p.is_file() else None

        files: list[tuple[str, Path]] = []

        def add(arc: str, url: Any) -> None:
            p = path_of(url)
            if p is not None and arc not in {a for a, _ in files}:
                files.append((arc, p))

        def ext(url: str) -> str:
            return Path(url).suffix or ".bin"

        fin = st.get("final") or {}
        if fin.get("url"):
            add(f"film/{Path(fin['url']).name}", fin["url"])
        if fin.get("captions_url"):
            add(f"film/{Path(fin['captions_url']).name}", fin["captions_url"])
        mus = st.get("music") or {}
        cur = next((m for m in mus.get("versions", []) if m.get("v") == mus.get("current")), None)
        if cur and cur.get("url"):
            add(f"music/soundtrack_v{cur['v']}{ext(cur['url'])}", cur["url"])
        for i, sc in enumerate(st.get("scenes") or [], 1):
            vo = sc.get("voiceover") if isinstance(sc.get("voiceover"), dict) else {}
            if vo.get("url"):
                add(f"voiceover/{i:02d}_{sc['id']}{ext(vo['url'])}", vo["url"])
            var = _winner_variant(sc)
            if var and var.get("url"):
                add(f"keyframes/{i:02d}_{sc['id']}_{sc.get('beat') or 'scene'}{ext(var['url'])}", var["url"])
        posters = st.get("posters") or {}
        for it in posters.get("items") or []:
            win = next((v for v in it.get("variants", []) if v.get("idx") == it.get("winner")), None)
            if win:
                add(f"posters/{it['format']}_{str(it.get('aspect', '')).replace(':', 'x')}{ext(win['url'])}",
                    win["url"])
        for market, loc in (st.get("localizations") or {}).items():
            d = f"localized/{market_slug(market)}"
            for p in loc.get("posters") or []:
                add(f"{d}/posters/{p.get('format')}{ext(p.get('url') or '')}", p.get("url"))
            for s in loc.get("scenes") or []:
                add(f"{d}/keyframes/{s.get('scene_id')}{ext(s.get('url') or '')}", s.get("url"))
            for v in loc.get("voiceover") or []:
                if isinstance(v, dict) and v.get("url"):
                    add(f"{d}/voiceover/{v.get('scene_id')}{ext(v['url'])}", v["url"])
            if loc.get("video_url"):
                add(f"{d}/animatic{ext(loc['video_url'])}", loc["video_url"])
            if loc.get("captions_url"):
                add(f"{d}/animatic{ext(loc['captions_url'])}", loc["captions_url"])
            if loc.get("music_url"):
                add(f"{d}/music{ext(loc['music_url'])}", loc["music_url"])

        plan = st.get("plan") or {}
        name = plan.get("campaign_name") or (st.get("input") or {}).get("brief", "")[:48] or f"AdMate {run.id}"
        copy = posters.get("copy") or {}
        readme = [f"{name}", "=" * min(72, max(8, len(name))), ""]
        if plan.get("tagline"):
            readme.append(f"Tagline: {plan['tagline']}")
        if plan.get("cta"):
            readme.append(f"CTA: {plan['cta']}")
        if copy:
            readme += ["", "Poster copy", f"  Headline: {copy.get('headline', '')}",
                       f"  Subline:  {copy.get('subline', '')}", f"  CTA:      {copy.get('cta', '')}"]
        pal = (plan.get("brand") or {}).get("palette") or []
        if pal:
            readme.append(f"Palette: {', '.join(map(str, pal))}")
        readme += ["", f"Run {run.id} · generated by AdMate (Nano Banana 2 Lite · Omni Flash · Lyria · Gemini Flash)",
                   "", "Files", "-----"]
        readme += [f"  {arc}  ({p.stat().st_size / 1024:.0f} KB)" for arc, p in files]
        readme += ["  plan.json", "  README.txt", ""]

        fd, tmp = tempfile.mkstemp(prefix=f"admate_kit_{run.id}_", suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr("README.txt", "\n".join(readme), compress_type=zipfile.ZIP_DEFLATED)
                zf.writestr("plan.json", json.dumps({"plan": plan, "poster_copy": copy or None}, ensure_ascii=False,
                                                    indent=2), compress_type=zipfile.ZIP_DEFLATED)
                for arc, p in files:
                    zf.write(p, arc, compress_type=(zipfile.ZIP_DEFLATED if p.suffix in (".vtt", ".json", ".txt")
                                                    else zipfile.ZIP_STORED))
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        base = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:48] or "admate"
        return Path(tmp), f"{base}_campaign_kit.zip"


# --------------------------------------------------------------------------------------------- HTTP
router = APIRouter()


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _studio(request: Request) -> PosterStudio:
    """The app's studio (created lazily if the integration did not register one)."""
    studio = getattr(request.app.state, "posters", None)
    if studio is None:
        studio = PosterStudio(request.app.state.manager)
        request.app.state.posters = studio
    return studio


def _run(request: Request, run_id: str) -> Any:
    if not _RUN_ID_RE.match(run_id or ""):
        return None
    return request.app.state.manager.get(run_id)


async def _body(request: Request) -> dict | None:
    """JSON object body; ``{}`` if empty, ``None`` if malformed."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@router.post("/api/runs/{run_id}/posters")
async def regenerate_posters(run_id: str, request: Request) -> JSONResponse:
    """Regenerate all (or the listed) poster formats, optionally with a director's instruction."""
    run = _run(request, run_id)
    if run is None:
        return _err(404, "run not found")
    body = await _body(request)
    if body is None:
        return _err(400, "body must be a JSON object")
    formats = body.get("formats")
    if formats is not None:
        if not isinstance(formats, list) or not all(isinstance(f, str) for f in formats):
            return _err(422, "formats must be a list of format ids")
        unknown = [f for f in formats if f not in PP.FORMAT_BY_ID]
        if unknown:
            return _err(422, f"unknown format(s): {', '.join(unknown[:5])}; valid: {', '.join(PP.FORMAT_IDS)}")
    instruction = body.get("instruction")
    if instruction is not None and not isinstance(instruction, str):
        return _err(422, "instruction must be a string")
    if not all_scenes_have_winner(run):
        return _err(409, "the storyboard is not ready yet: every scene needs a winning keyframe first")
    _studio(request).start_kit(run, formats or None, instruction)
    return JSONResponse({"ok": True})


@router.post("/api/runs/{run_id}/posters/{fmt}/select")
async def select_poster(run_id: str, fmt: str, request: Request) -> JSONResponse:
    """User override of the winning variant for one format."""
    run = _run(request, run_id)
    if run is None:
        return _err(404, "run not found")
    if fmt not in PP.FORMAT_BY_ID:
        return _err(404, f"unknown format: {fmt[:40]}")
    body = await _body(request)
    if body is None:
        return _err(400, "body must be a JSON object")
    idx = body.get("idx")
    if isinstance(idx, bool) or not isinstance(idx, int):
        return _err(422, "idx must be an integer")
    if not _studio(request).select(run, fmt, idx):
        return _err(404, f"variant {idx} not found for {fmt}")
    return JSONResponse({"ok": True})


@router.get("/api/runs/{run_id}/kit.zip")
async def kit_zip(run_id: str, request: Request):
    """Download the campaign kit: film, captions, music, voiceovers, keyframes, posters, localized assets."""
    run = _run(request, run_id)
    if run is None:
        return _err(404, "run not found")
    try:
        path, filename = await asyncio.to_thread(_studio(request).build_zip, run)
    except Exception as exc:  # noqa: BLE001
        log.exception("run %s: kit.zip failed", run_id)
        return _err(500, f"could not build the campaign kit: {_err_text(exc)}")
    return FileResponse(path, media_type="application/zip", filename=filename,
                        background=BackgroundTask(lambda: Path(path).unlink(missing_ok=True)))
