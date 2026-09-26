#!/usr/bin/env python3
"""AdMate live model smoke test.

Probes every GenMedia model AdMate depends on, in pipeline order, and reports
which API path actually worked for each one. The model ids are new preview ids,
so the printed output is how we learn the real API shape:

    text JSON  ->  image  ->  image w/ ref (continuity)  ->  music  ->  tts
               ->  video (image_to_video from the generated image)
               ->  edit turn 1 + turn 2 (multi-turn conversational edit)
               ->  transcribe

Two modes:

* default   -- goes through ``app.genai_client.GenMedia`` exactly like the app
               does (adaptive fallbacks, path memory, retries). Works in mock
               mode too (``ADMATE_MOCK=1`` or ``--mock``), which is how we prove
               the harness end to end on a laptop without network.
* ``--raw`` -- bypasses GenMedia and calls the google-genai SDK directly with
               the primary request shapes from CONTRACT section 1a, printing
               the raw (truncated) responses. Use it to diagnose an adapter.

Every artefact is written to ``data/smoke/<timestamp>/`` and a summary table
(role, model, api_path, latency, bytes, error) is printed at the end. Failures
print the full underlying exception chain, because that text is the most
useful debugging signal we have for unverified preview APIs.

Examples::

    .venv/bin/python scripts/smoke_test.py                      # everything, live
    .venv/bin/python scripts/smoke_test.py --mock               # offline harness check
    .venv/bin/python scripts/smoke_test.py --only image,video --aspect 9:16
    .venv/bin/python scripts/smoke_test.py --only transcribe --audio brief.webm
    .venv/bin/python scripts/smoke_test.py --raw --only image
    .venv/bin/python scripts/smoke_test.py --only tts,transcribe  # speech round trip
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import mimetypes
import os
import struct
import sys
import time
import traceback
import wave
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STEPS = ("text", "image", "music", "tts", "video", "edit", "transcribe")

# Contract defaults (CONTRACT section 1). Only used by --raw, which must work even
# if app/ is broken; the GenMedia path reads the ids from app.config.settings.
DEFAULT_MODELS = {
    "text": ("ADMATE_MODEL_TEXT", "gemini-3.8-flash"),
    "image": ("ADMATE_MODEL_IMAGE", "gemini-3.1-flash-lite-image"),
    "video": ("ADMATE_MODEL_VIDEO", "gemini-omni-1.1-flash"),
    "music": ("ADMATE_MODEL_MUSIC", "lyria-3.5"),
    "transcribe": ("ADMATE_MODEL_TRANSCRIBE", "gemini-3.5-transcribe"),
    "tts": ("ADMATE_MODEL_TTS", "gemini-3.8-flash-tts"),
}

# Small fixed creative inputs so runs are comparable across days.
TEXT_PROMPT = (
    "You are an ad creative director. For the brief 'Irani chai cafe launch in "
    "Hyderabad', return a campaign name, a tagline, and exactly 2 scene titles."
)
TEXT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "campaign_name": {"type": "string"},
        "tagline": {"type": "string"},
        "scenes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["campaign_name", "tagline", "scenes"],
}
IMAGE_PROMPT = (
    "Cinematic product hero shot: a steaming glass of Irani chai with an Osmania "
    "biscuit on a marble cafe table, warm morning light, shallow depth of field, "
    "teal and saffron palette, no text."
)
IMAGE_REF_PROMPT = (
    "Same cup, same table, same lighting and palette as the reference image; now "
    "a hand reaches in to dip the biscuit into the chai. Keep the product identical."
)
MUSIC_PROMPT = (
    "Instrumental, no vocals. 12 seconds. Warm lo-fi Indian cafe groove, 92 bpm, "
    "D major, sitar, soft tabla, Rhodes. 0:00-0:06 gentle hook; 0:06-0:12 lift, "
    "ending on a resolved sting."
)
VIDEO_PROMPT = (
    "Slow push-in on the chai glass, steam curling upward, dust motes in the "
    "light, subtle handheld drift. Keep the keyframe's product and composition."
)
TTS_LINE = "Slow-brewed for hours, creamy and saffron-sweet. Your table is waiting at Irani Chai House."
TTS_VOICE = "Kore"
TTS_STYLE = "warm, confident, upbeat narrator"
EDIT_TURNS = (
    "Make it golden hour: warmer, lower sun, longer shadows.",
    "Now add gentle rain on the window behind the table; keep everything else.",
)
VIDEO_TERMINAL = {"completed", "failed", "cancelled", "incomplete", "budget_exceeded", "requires_action"}


# --------------------------------------------------------------------------- utils
@dataclass
class Row:
    """One line of the final summary table."""

    role: str
    model: str
    api_path: str = "-"
    latency_ms: int | None = None
    nbytes: int | None = None
    error: str = ""
    file: str = ""


def ext_for_mime(mime: str | None) -> str:
    """Map a response mime type to a file extension (kept local: no app import)."""
    table = {
        "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
        "audio/x-wav": ".wav", "audio/webm": ".webm", "audio/ogg": ".ogg",
        "application/json": ".json", "text/plain": ".txt",
    }
    return table.get((mime or "").split(";")[0].strip().lower(), ".bin")


def synth_wav(seconds: float = 3.0, rate: int = 16000) -> bytes:
    """Return a short mono WAV (a two-tone chime). Proves the transcribe request
    path only: it contains no speech, so an empty transcript is the right answer.
    Pass ``--audio`` with a real recording to check transcription quality."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            t = i / rate
            f = 440.0 if t < seconds / 2 else 660.0
            env = min(1.0, t * 8) * max(0.0, 1.0 - t / seconds)
            frames += struct.pack("<h", int(12000 * env * math.sin(2 * math.pi * f * t)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


def pcm_to_wav(pcm: bytes, mime: str | None) -> bytes:
    """Wrap raw 16-bit mono PCM (``audio/L16;codec=pcm;rate=24000``) in a WAV header.

    Kept local (like :func:`ext_for_mime`) so ``--raw`` works even if app/ is broken.
    Payloads that already are WAV/MP3 are returned unchanged."""
    if pcm[:4] == b"RIFF" or pcm[:3] == b"ID3" or pcm[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return pcm
    rate = 24000
    for param in (mime or "").split(";"):
        if param.strip().startswith("rate="):
            rate = int(param.strip()[5:] or rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_seconds(data: bytes | None) -> float | None:
    """Duration of a WAV payload (None if it is not a readable WAV)."""
    try:
        with wave.open(io.BytesIO(data or b"")) as w:
            return round(w.getnframes() / float(w.getframerate()), 2)
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def truncate(obj: Any, limit: int = 160) -> Any:
    """Recursively shorten long strings / bytes (base64 payloads) for printing."""
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, str):
        return obj if len(obj) <= limit else f"{obj[:limit]}... <{len(obj)} chars>"
    if isinstance(obj, dict):
        return {k: truncate(v, limit) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [truncate(v, limit) for v in obj]
    return obj


def dump(obj: Any) -> str:
    """Pretty-print an SDK response (pydantic model, dict or anything else)."""
    try:
        if hasattr(obj, "model_dump"):
            data = obj.model_dump(mode="json", exclude_none=True)
        elif isinstance(obj, (dict, list)):
            data = obj
        else:
            return repr(obj)[:4000]
        return json.dumps(truncate(data), indent=2, default=str)[:6000]
    except Exception:  # noqa: BLE001 - printing must never fail a probe
        return repr(obj)[:4000]


def describe_exception(exc: BaseException) -> str:
    """Full traceback + chained causes + any structured attrs (status, model, path)."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    extras = {}
    for attr in ("model", "api_path", "status_code", "code", "status", "details", "response_json"):
        val = getattr(exc, attr, None)
        if val is not None and not callable(val):
            extras[attr] = truncate(val, 800)
    if extras:
        text += "  attrs: " + json.dumps(extras, default=str) + "\n"
    return text


def short_error(exc: BaseException, limit: int = 70) -> str:
    """One-line error for the table."""
    msg = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return msg if len(msg) <= limit else msg[: limit - 3] + "..."


def print_table(rows: list[Row]) -> None:
    """Render the summary as an aligned plain-text table."""
    headers = ("role", "model", "api_path", "latency", "bytes", "error")
    data = [
        (
            r.role, r.model, r.api_path,
            "-" if r.latency_ms is None else f"{r.latency_ms / 1000:.2f}s",
            "-" if r.nbytes is None else f"{r.nbytes:,}",
            r.error or "ok",
        )
        for r in rows
    ]
    widths = [max(len(h), *(len(str(d[i])) for d in data)) if data else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print("\n" + line + "\n" + "-" * len(line))
    for d in data:
        print("  ".join(str(c).ljust(w) for c, w in zip(d, widths)))


class Harness:
    """Shared bookkeeping: output dir, rows, per-step timeout + error capture."""

    def __init__(self, out_dir: Path, timeout: float):
        self.out_dir = out_dir
        self.timeout = timeout
        self.rows: list[Row] = []

    def save(self, name: str, data: bytes | None, mime: str | None) -> str:
        """Write an artefact and return its path relative to the repo root."""
        if not data:
            return ""
        path = self.out_dir / f"{name}{ext_for_mime(mime)}"
        path.write_bytes(data)
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    async def step(self, role: str, model: str, fn: Callable[[], Awaitable[Row]], api_path: str = "-") -> Row:
        """Run one probe with a timeout; failures become a Row with the error.

        ``api_path`` labels the failure row when the exception doesn't carry one
        (raw SDK errors); GenAIError instances report their own path."""
        print(f"\n=== {role}  ({model})", flush=True)
        t0 = time.perf_counter()
        try:
            row = await asyncio.wait_for(fn(), timeout=self.timeout)
            print(f"    ok  api_path={row.api_path}  latency={row.latency_ms}ms  bytes={row.nbytes}  {row.file}")
        except Exception as exc:  # noqa: BLE001 - we want every failure reported, not raised
            if isinstance(exc, asyncio.TimeoutError):
                exc = TimeoutError(f"step exceeded --timeout {self.timeout:.0f}s")
            print(f"    FAILED after {time.perf_counter() - t0:.1f}s\n{describe_exception(exc)}", flush=True)
            row = Row(role=role, model=model, api_path=getattr(exc, "api_path", None) or api_path,
                      latency_ms=int((time.perf_counter() - t0) * 1000), error=short_error(exc))
        self.rows.append(row)
        return row


async def progress_printer(update: dict) -> None:
    """on_progress callback for long video renders."""
    print(f"    .. {update.get('status', '?')} {update.get('elapsed_ms', 0) / 1000:.0f}s", flush=True)


def load_audio(path: str | None) -> tuple[bytes, str] | None:
    """Read ``--audio`` from disk and guess its mime type."""
    if not path:
        return None
    p = Path(path).expanduser()
    mime = mimetypes.guess_type(p.name)[0] or "audio/wav"
    if p.suffix.lower() == ".webm":
        mime = "audio/webm"
    return p.read_bytes(), mime


# ----------------------------------------------------------------- GenMedia mode
async def run_genmedia(args: argparse.Namespace, h: Harness, steps: list[str]) -> None:
    """Probe each model through the app's own adapter layer (CONTRACT section 3)."""
    from app.config import settings  # imported late so --mock can set env first
    from app.genai_client import GenMedia

    gm = GenMedia(settings)
    print(f"GenMedia mode={'MOCK' if gm.mock else 'LIVE'}  out={h.out_dir}")
    ctx: dict[str, Any] = {}

    if "text" in steps:
        async def text() -> Row:
            data, res = await gm.generate_json([TEXT_PROMPT], TEXT_SCHEMA, temperature=0.7)
            raw = json.dumps(data, indent=2, ensure_ascii=False).encode()
            print(f"    json: {json.dumps(truncate(data))[:400]}")
            return Row("text/json", res.model, res.api_path, res.latency_ms, len(raw),
                       file=h.save("01_text", raw, "application/json"))
        await h.step("text/json", settings.model_text, text)

    if "image" in steps:
        async def image() -> Row:
            res = await gm.generate_image(IMAGE_PROMPT, aspect=args.aspect, size=args.size)
            ctx["image"] = res.data
            return Row("image", res.model, res.api_path, res.latency_ms, len(res.data or b""),
                       file=h.save("02_image", res.data, res.mime_type))
        await h.step("image", settings.model_image, image)

        async def image_ref() -> Row:
            if not ctx.get("image"):
                raise RuntimeError("skipped: no base image from the previous step")
            res = await gm.generate_image(IMAGE_REF_PROMPT, refs=[ctx["image"]], aspect=args.aspect, size=args.size)
            return Row("image+ref", res.model, res.api_path, res.latency_ms, len(res.data or b""),
                       file=h.save("03_image_ref", res.data, res.mime_type))
        await h.step("image+ref", settings.model_image, image_ref)

    if "music" in steps:
        async def music() -> Row:
            res = await gm.generate_music(MUSIC_PROMPT, seconds=args.music_seconds)
            ctx["music"] = (res.data, res.mime_type)
            return Row("music", res.model, res.api_path, res.latency_ms, len(res.data or b""),
                       file=h.save("04_music", res.data, res.mime_type))
        await h.step("music", settings.model_music, music)

    if "tts" in steps:
        async def tts() -> Row:
            res = await gm.generate_speech(TTS_LINE, voice=TTS_VOICE, style=TTS_STYLE)
            ctx["speech"] = (res.data, res.mime_type)
            print(f"    voice={TTS_VOICE}  mime={res.mime_type}  wav_seconds={wav_seconds(res.data)}")
            return Row("tts", res.model, res.api_path, res.latency_ms, len(res.data or b""),
                       file=h.save("04b_tts", res.data, res.mime_type))
        await h.step("tts", settings.model_tts, tts)

    if "video" in steps:
        async def video() -> Row:
            res = await gm.generate_video(VIDEO_PROMPT, image=ctx.get("image"), aspect=args.aspect,
                                          seconds=args.video_seconds, on_progress=progress_printer)
            ctx["video"] = res
            print(f"    meta: {json.dumps(truncate(res.meta), default=str)}")
            return Row("video i2v" if ctx.get("image") else "video t2v", res.model, res.api_path,
                       res.latency_ms, len(res.data or b""), file=h.save("05_video", res.data, res.mime_type))
        await h.step("video", settings.model_video, video)

    if "edit" in steps:
        for turn, instruction in enumerate(EDIT_TURNS, start=1):
            async def edit(turn: int = turn, instruction: str = instruction) -> Row:
                prev = ctx.get("video")
                res = await gm.edit_video(
                    instruction,
                    previous_interaction_id=(prev.meta or {}).get("interaction_id") if prev else None,
                    video=prev.data if prev else None,
                    image=ctx.get("image"),
                    aspect=args.aspect,
                    seconds=args.video_seconds,
                    on_progress=progress_printer,
                )
                ctx["video"] = res  # chain turn 2 onto turn 1
                print(f"    meta: {json.dumps(truncate(res.meta), default=str)}")
                path = res.api_path + (f" ({res.meta['fallback']})" if (res.meta or {}).get("fallback") else "")
                return Row(f"edit turn {turn}", res.model, path, res.latency_ms, len(res.data or b""),
                           file=h.save(f"06_edit_turn{turn}", res.data, res.mime_type))
            await h.step(f"edit turn {turn}", settings.model_video, edit)

    if "transcribe" in steps:
        async def transcribe() -> Row:
            audio = load_audio(args.audio) or ctx.get("speech") or ctx.get("music") or (synth_wav(), "audio/wav")
            if args.audio is None and "speech" in ctx:
                print(f"    note: transcribing the tts output; expect ~{TTS_LINE!r}")
            elif args.audio is None:
                print("    note: no --audio given; using non-speech audio (tests the request path only)")
            data, mime = audio
            res = await gm.transcribe(data, mime)
            print(f"    transcript: {(res.text or '')[:300]!r}")
            text_bytes = (res.text or "").encode()
            return Row("transcribe", res.model, res.api_path, res.latency_ms, len(text_bytes),
                       file=h.save("07_transcript", text_bytes, "text/plain"))
        await h.step("transcribe", settings.model_transcribe, transcribe)

    try:
        stats = gm.stats()
        (h.out_dir / "genmedia_stats.json").write_text(json.dumps(stats, indent=2, default=str))
        print("\nGenMedia.stats():\n" + json.dumps(stats, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001
        print(f"\nGenMedia.stats() failed: {short_error(exc)}")


# ---------------------------------------------------------------------- raw mode
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def _decode(client: Any, content: Any) -> tuple[bytes | None, str | None]:
    """Pull bytes out of an interactions content object (inline base64, else download its ``uri``)."""
    if content is None:
        return None, None
    data = getattr(content, "data", None)
    mime = getattr(content, "mime_type", None)
    if isinstance(data, str):
        try:
            return base64.b64decode(data), mime
        except ValueError:
            return None, mime
    if isinstance(data, (bytes, bytearray)):
        return bytes(data), mime
    uri = getattr(content, "uri", None)
    if uri:
        print(f"    output delivered by uri: {uri}; downloading via files.download", flush=True)
        try:
            return await client.aio.files.download(file=uri), mime
        except Exception as exc:  # noqa: BLE001 - report and keep the probe row
            print(f"    uri download failed: {short_error(exc, 200)}")
    return None, mime


def _gc_media(resp: Any) -> tuple[bytes | None, str | None]:
    """First inline_data part of a generate_content response."""
    try:
        for part in resp.candidates[0].content.parts or []:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                return part.inline_data.data, part.inline_data.mime_type
    except (AttributeError, IndexError, TypeError):
        pass
    return None, None


async def run_raw(args: argparse.Namespace, h: Harness, steps: list[str]) -> None:
    """Direct SDK calls with the primary request shapes, printing raw responses."""
    from dotenv import load_dotenv
    from google import genai
    from google.genai import types

    load_dotenv(ROOT / ".env")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise SystemExit("--raw needs GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment or .env")
    client = genai.Client(api_key=key)
    model = {role: os.getenv(env, default) for role, (env, default) in DEFAULT_MODELS.items()}
    ctx: dict[str, Any] = {}
    print(f"RAW SDK mode  out={h.out_dir}\nmodels: {json.dumps(model)}")

    async def interact(**kw: Any) -> Any:
        """interactions.create + poll get() until terminal (a queued reply must not read as empty)."""
        inter = await client.aio.interactions.create(**kw)
        t0 = time.perf_counter()
        while getattr(inter, "status", "completed") not in VIDEO_TERMINAL:
            if time.perf_counter() - t0 > h.timeout:
                raise TimeoutError(f"interaction {inter.id} still {inter.status}")
            await asyncio.sleep(3.0)
            inter = await client.aio.interactions.get(inter.id)
            print(f"    .. {inter.status} {time.perf_counter() - t0:.0f}s", flush=True)
        return inter

    async def timed(role: str, mdl: str, api: str, name: str, call: Callable[[], Awaitable[Any]],
                    extract: Callable[[Any], Any], key_: str | None = None) -> None:
        """Run one raw probe; ``extract`` maps the response to ``(bytes, mime)`` (may be async)."""
        async def probe() -> Row:
            t0 = time.perf_counter()
            resp = await call()
            ms = int((time.perf_counter() - t0) * 1000)
            print("    raw response:\n" + "\n".join("      " + ln for ln in dump(resp).splitlines()))
            out = extract(resp)
            data, mime = (await out) if asyncio.iscoroutine(out) else out
            # A video without fetchable bytes still has an id the edit chain can build on.
            if key_ and (data or (key_ == "video" and getattr(resp, "id", None))):
                ctx[key_] = (data or b"", mime, resp)
            return Row(role, mdl, api, ms, len(data or b""), file=h.save(name, data, mime))
        await h.step(f"{role} [{api}]", mdl, probe, api_path=api)

    def inter_text(r: Any) -> tuple[bytes | None, str | None]:
        return (getattr(r, "output_text", None) or "").encode() or None, "text/plain"

    def inter_media(kind: str) -> Callable[[Any], Awaitable[tuple[bytes | None, str | None]]]:
        return lambda r: _decode(client, getattr(r, f"output_{kind}", None))

    if "text" in steps:
        await timed("text/json", model["text"], "generate_content", "01_text_gc",
                    lambda: client.aio.models.generate_content(
                        model=model["text"], contents=TEXT_PROMPT,
                        config=types.GenerateContentConfig(response_mime_type="application/json",
                                                           response_json_schema=TEXT_SCHEMA)),
                    lambda r: ((r.text or "").encode() or None, "application/json"))
        await timed("text/json", model["text"], "interactions", "01_text_int",
                    lambda: interact(
                        model=model["text"], input=TEXT_PROMPT,
                        response_format={"type": "text", "mime_type": "application/json", "schema": TEXT_SCHEMA}),
                    inter_text)

    if "image" in steps:
        await timed("image", model["image"], "generate_content", "02_image_gc",
                    lambda: client.aio.models.generate_content(
                        model=model["image"], contents=IMAGE_PROMPT,
                        config=types.GenerateContentConfig(
                            response_modalities=["IMAGE"],
                            image_config=types.ImageConfig(aspect_ratio=args.aspect, image_size=args.size))),
                    _gc_media, key_="image")
        await timed("image", model["image"], "interactions", "02_image_int",
                    lambda: interact(
                        model=model["image"], input=IMAGE_PROMPT, response_modalities=["image"],
                        response_format={"type": "image", "aspect_ratio": args.aspect,
                                         "image_size": args.size, "delivery": "inline"}),
                    inter_media("image"), key_="image")

    if "music" in steps:
        await timed("music", model["music"], "interactions", "04_music_int",
                    lambda: interact(
                        model=model["music"], input=MUSIC_PROMPT, response_modalities=["audio"],
                        response_format={"type": "audio", "mime_type": "audio/mp3", "delivery": "inline"}),
                    inter_media("audio"), key_="music")
        await timed("music", model["music"], "generate_content", "04_music_gc",
                    lambda: client.aio.models.generate_content(
                        model=model["music"], contents=MUSIC_PROMPT,
                        config=types.GenerateContentConfig(response_modalities=["AUDIO"])),
                    _gc_media, key_="music")

    if "tts" in steps:
        prompt = f"Say in a {TTS_STYLE} voice: {TTS_LINE}"

        def speech(r_media: tuple[bytes | None, str | None]) -> tuple[bytes | None, str | None]:
            data, mime = r_media
            print(f"    tts mime={mime}")
            return (pcm_to_wav(data, mime), "audio/wav") if data else (None, mime)

        async def inter_speech(r: Any) -> tuple[bytes | None, str | None]:
            return speech(await _decode(client, getattr(r, "output_audio", None)))

        await timed("tts", model["tts"], "generate_content", "04b_tts_gc",
                    lambda: client.aio.models.generate_content(
                        model=model["tts"], contents=prompt,
                        config=types.GenerateContentConfig(
                            response_modalities=["AUDIO"],
                            speech_config=types.SpeechConfig(
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE)),
                                language_code="en-US"))),
                    lambda r: speech(_gc_media(r)), key_="speech")
        await timed("tts", model["tts"], "interactions", "04b_tts_int",
                    lambda: interact(
                        model=model["tts"], input=prompt, response_modalities=["audio"],
                        generation_config={"speech_config": [{"voice": TTS_VOICE, "language": "en-US"}]}),
                    inter_speech, key_="speech")

    if "video" in steps:
        image = ctx.get("image", (None, None, None))
        vinput: Any = VIDEO_PROMPT
        if image[0]:
            vinput = [{"type": "text", "text": VIDEO_PROMPT},
                      {"type": "image", "data": _b64(image[0]), "mime_type": image[1] or "image/png"}]
        await timed("video i2v" if image[0] else "video t2v", model["video"], "interactions", "05_video_int",
                    lambda: interact(
                        model=model["video"], input=vinput, response_modalities=["video"],
                        response_format={"type": "video", "aspect_ratio": args.aspect, "resolution": "720p",
                                         "duration": f"{args.video_seconds}s", "delivery": "inline"},
                        generation_config={"video_config": {"task": "image_to_video" if image[0] else "text_to_video"}},
                        background=True, store=True),
                    inter_media("video"), key_="video")

        async def legacy_video() -> Any:
            src = types.GenerateVideosSource(
                prompt=VIDEO_PROMPT,
                image=types.Image(image_bytes=image[0], mime_type=image[1] or "image/png") if image[0] else None)
            op = await client.aio.models.generate_videos(
                model=model["video"], source=src,
                config=types.GenerateVideosConfig(aspect_ratio=args.aspect, duration_seconds=args.video_seconds))
            t0 = time.perf_counter()
            while not op.done:
                if time.perf_counter() - t0 > h.timeout:
                    raise TimeoutError("generate_videos operation did not finish")
                await asyncio.sleep(5.0)
                op = await client.aio.operations.get(op)
                print(f"    .. operation pending {time.perf_counter() - t0:.0f}s", flush=True)
            return op

        def legacy_extract(op: Any) -> tuple[bytes | None, str | None]:
            try:
                vid = op.response.generated_videos[0].video
                return vid.video_bytes, vid.mime_type or "video/mp4"
            except (AttributeError, IndexError, TypeError):
                return None, None

        if args.legacy_video:
            await timed("video", model["video"], "generate_videos", "05_video_legacy", legacy_video, legacy_extract)

    if "edit" in steps:
        prev = ctx.get("video")
        prev_id = getattr(prev[2], "id", None) if prev else None
        for turn, instruction in enumerate(EDIT_TURNS, start=1):
            if prev_id:
                inp: Any = instruction
            elif prev:
                inp = [{"type": "text", "text": instruction},
                       {"type": "video", "data": _b64(prev[0]), "mime_type": prev[1] or "video/mp4"}]
            else:
                print(f"\n=== edit turn {turn}: skipped (no prior video / interaction id)")
                break
            pid = prev_id
            await timed(f"edit turn {turn}", model["video"], "interactions", f"06_edit_turn{turn}",
                        lambda inp=inp, pid=pid: interact(
                            model=model["video"], input=inp, previous_interaction_id=pid,
                            response_modalities=["video"],
                            response_format={"type": "video", "aspect_ratio": args.aspect, "delivery": "inline"},
                            generation_config={"video_config": {"task": "edit"}},
                            background=True, store=True),
                        inter_media("video"), key_="video")
            prev = ctx.get("video")
            new_id = getattr(prev[2], "id", None) if prev else None
            if new_id == prev_id:
                break  # this turn failed; don't pretend turn 2 chains on it
            prev_id = new_id

    if "transcribe" in steps:
        audio = load_audio(args.audio)
        if audio is None:
            m = ctx.get("speech") or ctx.get("music")
            audio = (m[0], m[1]) if m else (synth_wav(), "audio/wav")
            print("\nnote: no --audio given; " + (f"transcribing the tts output (expect ~{TTS_LINE!r})"
                                                  if "speech" in ctx else
                                                  "using non-speech audio (tests the request path only)"))
        data, mime = audio
        await timed("transcribe", model["transcribe"], "interactions", "07_transcript_int",
                    lambda: interact(
                        model=model["transcribe"],
                        input=[{"type": "audio", "data": _b64(data), "mime_type": mime}],
                        response_modalities=["text"],
                        generation_config={"transcription_config": {"mode": "verbatim"}}),
                    inter_text)
        await timed("transcribe", model["transcribe"], "generate_content", "07_transcript_gc",
                    lambda: client.aio.models.generate_content(
                        model=model["transcribe"],
                        contents=[types.Part.from_bytes(data=data, mime_type=mime),
                                  "Transcribe this audio verbatim. Return only the transcript."]),
                    lambda r: ((r.text or "").encode() or None, "text/plain"))


# -------------------------------------------------------------------------- main
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live smoke test of every AdMate model.")
    p.add_argument("--only", default=",".join(STEPS),
                   help=f"comma-separated subset of: {','.join(STEPS)} (default: all)")
    p.add_argument("--aspect", default="16:9", choices=["16:9", "9:16"])
    p.add_argument("--size", default="1K", help="image size: 512 | 1K | 2K | 4K (default 1K)")
    p.add_argument("--video-seconds", type=int, default=6)
    p.add_argument("--music-seconds", type=int, default=12)
    p.add_argument("--audio", help="path to a real speech recording for the transcribe probe")
    p.add_argument("--timeout", type=float, default=600.0, help="per-step timeout in seconds")
    p.add_argument("--mock", action="store_true", help="force ADMATE_MOCK=1 (offline harness check)")
    p.add_argument("--raw", action="store_true", help="bypass GenMedia; call the SDK directly and print raw responses")
    p.add_argument("--legacy-video", action="store_true", help="--raw only: also try models.generate_videos")
    args = p.parse_args(argv)
    steps = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = sorted(set(steps) - set(STEPS))
    if unknown:
        p.error(f"unknown step(s) {unknown}; choose from {list(STEPS)}")
    args.steps = [s for s in STEPS if s in steps]  # always run in pipeline order
    if args.mock and args.raw:
        p.error("--raw talks to the real API; it cannot be combined with --mock")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mock:
        os.environ["ADMATE_MOCK"] = "1"
    out_dir = ROOT / "data" / "smoke" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    h = Harness(out_dir, args.timeout)
    t0 = time.perf_counter()
    runner = run_raw if args.raw else run_genmedia
    try:
        asyncio.run(runner(args, h, args.steps))
    except KeyboardInterrupt:
        print("\ninterrupted — partial results below")
    except Exception as exc:  # noqa: BLE001 - setup failure (import, client init): report, don't crash
        print(f"\nharness setup failed before/between probes:\n{describe_exception(exc)}")
        h.rows.append(Row(role="setup", model="-", error=short_error(exc)))
    print_table(h.rows)
    failed = [r for r in h.rows if r.error]
    summary = {"mode": "raw" if args.raw else "genmedia", "aspect": args.aspect,
               "wall_s": round(time.perf_counter() - t0, 2), "rows": [asdict(r) for r in h.rows]}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\n{len(h.rows) - len(failed)}/{len(h.rows)} probes ok in {summary['wall_s']}s — artefacts in {out_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
