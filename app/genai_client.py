"""GenMedia — the ONLY module in AdLoop that talks to Google's generative APIs.

Every modality (text/JSON, image, video, video edit, music, speech, transcription) is
exposed as one ``async`` method on :class:`GenMedia` returning a uniform
:class:`GenResult`. Because the GenMedia model ids we target are brand new, each
method is built as an ordered list of *API paths* (e.g. ``generate_content``
then ``interactions``) with automatic fallback:

* **Path memory** – the first path that succeeds for a model is remembered, so
  later calls go straight there. ``ADLOOP_<ROLE>_PATH`` pins the first choice.
* **Request-shape ladder** – on HTTP 400 INVALID_ARGUMENT (or an empty/unparseable
  response) the same path is retried with progressively *smaller* requests,
  dropping optional fields (``image_size``, ``resolution``, ``duration``,
  ``background``, JSON schema, thinking config, ...).
  The shape level that worked is remembered too, so a rejected optional field
  costs one wasted round trip per model, not one per call.
* **Backoff** – 429/500/502/503/504 and transport errors are retried with
  exponential backoff (0.8s, 1.6s, 3.2s; max 3 retries). The SDK's own retry
  layer is disabled so retries never multiply, and an exhausted transient error
  (quota is per model, not per path) is raised instead of hopping paths.
* **Refusals are final** – safety/policy blocks raise immediately: re-sending the
  same prompt on a smaller request or another path cannot change the verdict.
* **UI-safe errors** – failures surface as :class:`GenAIError` whose message is
  short and of the form ``"<model> via <path>: <reason>"`` (API keys redacted).
* **Telemetry** – per-model call/error counts, working path, p50/p95 latency and
  per-modality in-flight counters via :meth:`GenMedia.stats` / :attr:`GenMedia.inflight`.
* **Mock mode** – with ``ADLOOP_MOCK=1`` (or no key) every method returns
  deterministic synthetic assets after a realistic jittered delay (see
  :mod:`app.mock`), so the whole studio runs offline.

Media ``parts`` accepted by :meth:`GenMedia.generate_json` are a list of plain
strings and ``("image" | "audio" | "video", bytes, mime)`` tuples — build the
latter with :func:`img_part`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import random
import re
import struct
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

import httpx

from app import mock as mockgen

log = logging.getLogger("adloop.genai")

ProgressCB = Callable[[dict], Awaitable[None]] | None

#: HTTP status codes that are worth retrying with backoff.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
#: Backoff schedule (seconds) for retryable errors.
BACKOFF_SCHEDULE = (0.8, 1.6, 3.2)
#: Terminal failure states of an Interaction.
FAILED_STATES = {"failed", "cancelled", "incomplete", "budget_exceeded", "requires_action"}
#: Substrings of finish reasons / error messages that mean "refused by policy".
_BLOCK_MARKERS = ("SAFETY", "PROHIBITED", "BLOCKLIST", "SPII", "RECITATION", "POLICY", "BLOCKED")
#: Prebuilt Gemini TTS voices the director may pick (CONTRACT §9b).
TTS_VOICES = ("Kore", "Puck", "Charon", "Fenrir", "Aoede", "Zephyr", "Leda", "Orus")
#: Sample rate assumed for raw TTS PCM when the mime type does not say.
TTS_PCM_RATE = 24000
#: Market language name -> BCP-47 code for TTS ``language_code`` hints.
LANGUAGE_CODES = {
    "english": "en-US", "hindi": "hi-IN", "telugu": "te-IN", "tamil": "ta-IN", "kannada": "kn-IN",
    "malayalam": "ml-IN", "marathi": "mr-IN", "bengali": "bn-IN", "gujarati": "gu-IN", "japanese": "ja-JP",
    "korean": "ko-KR", "mandarin": "cmn-CN", "chinese": "cmn-CN", "spanish": "es-ES", "french": "fr-FR",
    "german": "de-DE", "italian": "it-IT", "portuguese": "pt-BR", "arabic": "ar-EG", "indonesian": "id-ID",
    "thai": "th-TH", "vietnamese": "vi-VN", "turkish": "tr-TR", "russian": "ru-RU", "dutch": "nl-NL",
}

_LANG_CODE_RE = re.compile(r"[a-z]{2,3}(-[A-Za-z]{2,4})?")
_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|(key=)[^&\s'\"]+")


# ─────────────────────────────────────────────────────────────────────────────
# Public data types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class GenResult:
    """Uniform result of any generation call (CONTRACT §3)."""

    data: bytes | None  #: media bytes (None for text-only results)
    mime_type: str  #: e.g. "image/png", "video/mp4", "audio/mpeg", "text/plain"
    latency_ms: int  #: wall time of the successful request (excludes queueing)
    model: str  #: model id that produced the asset
    api_path: str  #: "interactions" | "generate_content" | "generate_videos" | "mock"
    text: str | None = None  #: text output (transcripts, raw JSON text)
    meta: dict = field(default_factory=dict)  #: video: interaction_id / operation / fallback


class GenAIError(Exception):
    """A UI-safe generation failure.

    ``str(err)`` is short, human-readable and never contains credentials.
    """

    def __init__(self, message: str, *, model: str = "", api_path: str = "",
                 status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.model = model
        self.api_path = api_path
        self.status_code = status_code


class _EmptyOutput(Exception):
    """The API call succeeded but returned no usable media/JSON (treated like a 400)."""


class _Blocked(GenAIError):
    """The model refused the request on safety/policy grounds — final, never retried or re-routed."""


def _is_block_reason(reason: Any) -> bool:
    """True when a finish reason / error text names a safety or policy refusal."""
    text = str(reason or "").upper()
    return any(marker in text for marker in _BLOCK_MARKERS)


def img_part(data: bytes, mime: str | None = None) -> tuple[str, bytes, str]:
    """Wrap image bytes as a ``parts`` entry for :meth:`GenMedia.generate_json`."""
    return ("image", data, mime or sniff_mime(data))


def audio_part(data: bytes, mime: str = "audio/webm") -> tuple[str, bytes, str]:
    """Wrap audio bytes as a ``parts`` entry for :meth:`GenMedia.generate_json`."""
    return ("audio", data, _base_mime(mime))


def sniff_mime(data: bytes, default: str = "image/png") -> str:
    """Best-effort media type detection from magic bytes."""
    head = data[:16] if data else b""
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    return default


# ─────────────────────────────────────────────────────────────────────────────
# Error helpers
# ─────────────────────────────────────────────────────────────────────────────


def _redact(text: str) -> str:
    """Strip anything that looks like an API key from ``text``."""
    return _KEY_RE.sub(lambda m: (m.group(1) or "") + "***", text)


def _status_code(exc: BaseException) -> int | None:
    """Extract an HTTP status code from SDK / httpx exceptions (None if unknown)."""
    if isinstance(exc, GenAIError):
        return exc.status_code
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value < 600:
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _is_transient(exc: BaseException) -> bool:
    """True for errors worth retrying with backoff (rate limit, 5xx, transport)."""
    code = _status_code(exc)
    if code is not None:
        return code in RETRYABLE_STATUS
    if isinstance(exc, (httpx.TransportError, ConnectionError)):
        return True
    name = type(exc).__name__
    return "Connection" in name or name in {"APITimeoutError", "RemoteProtocolError"}


def _short_reason(exc: BaseException, limit: int = 160) -> str:
    """A compact, key-free, single-line description of ``exc`` for the UI."""
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    msg = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    if not isinstance(msg, str):
        msg = str(msg)
    msg = " ".join(msg.split())
    # google.genai APIError renders as "400 INVALID_ARGUMENT. {...details...}" – keep the head
    # and the human "message" inside the details when present.
    # The quote style varies (dict repr vs JSON; "can't" forces double quotes), so match either.
    inner = re.search(r"['\"]message['\"]:\s*(['\"])(.{3,}?)\1", msg)
    code = _status_code(exc)
    if inner:
        msg = inner.group(2)
    if code and not msg.startswith(str(code)):
        msg = f"{code} {msg}"
    msg = _redact(msg)
    return msg if len(msg) <= limit else msg[: limit - 1] + "…"


# ─────────────────────────────────────────────────────────────────────────────
# Small utilities
# ─────────────────────────────────────────────────────────────────────────────


def _base_mime(mime: str | None) -> str:
    """``"audio/webm;codecs=opus"`` -> ``"audio/webm"``."""
    return (mime or "").split(";")[0].strip().lower()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _as_bytes(data: Any) -> bytes | None:
    """Decode SDK media payloads that may arrive as bytes or base64 strings."""
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        s = "".join(data.split())
        padded = s + "=" * (-len(s) % 4)
        try:
            # Strict decoding: a lenient decode would silently drop '-'/'_' and corrupt the media.
            if "-" in s or "_" in s:
                return base64.urlsafe_b64decode(padded)
            return base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            try:
                return base64.urlsafe_b64decode(padded)
            except (binascii.Error, ValueError):
                return None
    return None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute-or-key access (SDK objects sometimes surface as plain dicts)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _pcm_to_wav(pcm: bytes, rate: int = 48000, channels: int = 1, width: int = 2) -> bytes:
    """Wrap raw little-endian PCM (e.g. ``audio/l16``) in a WAV container."""
    byte_rate = rate * channels * width
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, channels * width, width * 8)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def _normalize_audio(data: bytes, mime: str | None, *, pcm_rate: int = 48000,
                     unknown_is_pcm: bool = False) -> tuple[bytes, str]:
    """Return ``(bytes, mime)`` playable by browsers/ffmpeg (mp3 -> audio/mpeg; PCM -> WAV).

    Raw PCM (``audio/L16;codec=pcm;rate=24000``) is wrapped in a WAV header using the
    ``rate=`` parameter (``pcm_rate`` when absent). ``unknown_is_pcm`` treats an
    unlabelled, unrecognised payload as PCM — right for TTS, which only emits PCM.
    """
    raw = (mime or "").lower()
    base = _base_mime(raw)
    sniffed = sniff_mime(data, default="")
    is_pcm = base in ("audio/l16", "audio/pcm", "audio/raw") or "codec=pcm" in raw
    if is_pcm or (unknown_is_pcm and sniffed not in ("audio/wav", "audio/mpeg") and base in ("", "audio")):
        rate_m = re.search(r"rate=(\d+)", raw)
        return _pcm_to_wav(data, rate=int(rate_m.group(1)) if rate_m else pcm_rate), "audio/wav"
    if sniffed in ("audio/wav", "audio/mpeg"):
        return data, sniffed
    if base in ("audio/mp3", "audio/mpeg3", "audio/x-mp3"):
        return data, "audio/mpeg"
    if base in ("audio/x-wav", "audio/wave"):
        return data, "audio/wav"
    return data, base or "audio/mpeg"


def language_code(language: str | None) -> str | None:
    """Map ``"Telugu"`` / ``"te-IN"`` / ``"Hyderabad · Telugu"`` to a BCP-47 code (None if unknown)."""
    if not language:
        return None
    text = language.strip()
    if _LANG_CODE_RE.fullmatch(text):
        return text
    for word in re.split(r"[^A-Za-z]+", text.lower())[::-1]:
        if word in LANGUAGE_CODES:
            return LANGUAGE_CODES[word]
    return None


def parse_json_loose(text: str) -> dict:
    """Parse the first JSON object in ``text`` (tolerates code fences / prose around it)."""
    if not text:
        raise _EmptyOutput("empty text response")
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            value, _ = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise _EmptyOutput("response was not valid JSON")


def _percentile(values: Sequence[float], pct: float) -> int | None:
    """Nearest-rank percentile of ``values`` (None when empty)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = min(len(ordered), max(1, math.ceil(pct / 100.0 * len(ordered))))
    return int(ordered[rank - 1])


# ─────────────────────────────────────────────────────────────────────────────
# Internal bookkeeping
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _Attempt:
    """One API path for a call.

    ``run(level)`` performs the request at request-shape ``level`` (0 = full
    request; higher levels drop optional fields). ``levels`` is how many
    shapes exist. ``remember`` controls whether success updates path memory.
    """

    path: str
    run: Callable[[int], Awaitable[GenResult]]
    levels: int = 2
    remember: bool = True


@dataclass
class _ModelStats:
    role: str
    calls: int = 0
    errors: int = 0
    api_path: str | None = None
    last_error: str | None = None
    latencies: deque = field(default_factory=lambda: deque(maxlen=512))


# ─────────────────────────────────────────────────────────────────────────────
# GenMedia
# ─────────────────────────────────────────────────────────────────────────────


class GenMedia:
    """Async facade over every Google GenMedia model AdLoop uses.

    Parameters
    ----------
    settings:
        The :class:`app.config.Settings` instance.
    http_options:
        Optional ``google.genai.types.HttpOptions`` override (used by tests to
        inject an ``httpx.AsyncClient`` with a mock transport).
    """

    def __init__(self, settings: Any, *, http_options: Any = None) -> None:
        self.settings = settings
        self.mock: bool = bool(settings.mock)
        self._sems = {
            "image": asyncio.Semaphore(settings.image_concurrency),
            "video": asyncio.Semaphore(settings.video_concurrency),
            "text": asyncio.Semaphore(settings.text_concurrency),
            "music": asyncio.Semaphore(settings.music_concurrency),
            "tts": asyncio.Semaphore(settings.tts_concurrency),
        }
        self._inflight = {"image": 0, "video": 0, "music": 0, "text": 0, "tts": 0}
        self._stats: dict[str, _ModelStats] = {}
        self._paths: dict[str, str] = {}  # (model|role) key -> api path that last worked
        self._levels: dict[tuple[str, str], int] = {}  # (key, path) -> request-shape level that worked
        self._overrides: dict[str, str] = dict(getattr(settings, "path_overrides", {}) or {})
        self._timeout = float(getattr(settings, "request_timeout_seconds", 150.0))
        self._mock_counters: dict[str, int] = {}
        # Pre-register every configured model so /api/health shows them before the first call.
        for role, model in (("text", settings.model_text), ("image", settings.model_image),
                            ("video", settings.model_video), ("music", settings.model_music),
                            ("transcribe", settings.model_transcribe), ("tts", settings.model_tts)):
            self._stat(model, role)
        self._client = None
        self._types = None
        if not self.mock:
            from google import genai
            from google.genai import types

            self._types = types
            # GenMedia owns retries (_with_backoff); a single SDK attempt keeps them from multiplying
            # (the interactions client otherwise retries 3x on 429/5xx underneath ours).
            opts = http_options if http_options is not None else types.HttpOptions()
            if opts.retry_options is None:
                opts.retry_options = types.HttpRetryOptions(attempts=1)
            self._client = genai.Client(api_key=settings.api_key, http_options=opts)

    # ── telemetry ────────────────────────────────────────────────────────────

    @property
    def inflight(self) -> dict[str, int]:
        """Current in-flight request counts per modality (image/video/music/text/tts)."""
        return dict(self._inflight)

    def api_paths(self) -> dict[str, str]:
        """Model id -> working api path (only models that have succeeded at least once)."""
        return {m: s.api_path for m, s in self._stats.items() if s.api_path}

    def stats(self) -> dict:
        """Per-model telemetry.

        ``{model: {role, calls, errors, api_path, shape_level, p50_ms, p95_ms, last_error}}`` where
        ``role`` is text/image/video/music/transcribe/tts and ``shape_level`` is the request-shape
        level that last worked on that path (0 = full request).
        """
        out: dict[str, dict] = {}
        for model, st in self._stats.items():
            lat = list(st.latencies)
            out[model] = {
                "role": st.role,
                "calls": st.calls,
                "errors": st.errors,
                "api_path": st.api_path,
                "shape_level": self._levels.get((model, st.api_path)) if st.api_path else None,
                "p50_ms": _percentile(lat, 50),
                "p95_ms": _percentile(lat, 95),
                "last_error": st.last_error,
            }
        return out

    def _stat(self, model: str, role: str) -> _ModelStats:
        st = self._stats.get(model)
        if st is None:
            st = self._stats[model] = _ModelStats(role=role)
        return st

    @asynccontextmanager
    async def _slot(self, modality: str) -> AsyncIterator[None]:
        """Acquire the modality semaphore and track the in-flight counter."""
        async with self._sems[modality]:
            self._inflight[modality] += 1
            try:
                yield
            finally:
                self._inflight[modality] -= 1

    # ── generic execution engine ──────────────────────────────────────────────

    def _ordered(self, role: str, memory_key: str, attempts: list[_Attempt]) -> list[_Attempt]:
        """Order attempts: env override first, then the remembered winner, then declared order."""
        preferred = self._overrides.get(role) or self._paths.get(memory_key)
        if not preferred:
            return attempts
        first = [a for a in attempts if a.path == preferred]
        return first + [a for a in attempts if a.path != preferred]

    async def _with_backoff(self, fn: Callable[[], Awaitable[GenResult]], model: str, path: str) -> GenResult:
        """Run ``fn`` retrying transient failures on the BACKOFF_SCHEDULE."""
        for attempt in range(len(BACKOFF_SCHEDULE) + 1):
            try:
                return await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                if attempt >= len(BACKOFF_SCHEDULE) or not _is_transient(exc):
                    raise
                delay = BACKOFF_SCHEDULE[attempt] * (1 + random.uniform(-0.1, 0.1))
                log.warning("%s via %s: transient error (%s); retry in %.1fs",
                            model, path, _short_reason(exc), delay)
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _execute(self, role: str, model: str, attempts: list[_Attempt], *,
                       memory_key: str | None = None, reorder: bool = True,
                       deadline: float | None = None) -> GenResult:
        """Run ``attempts`` with path memory, shape ladder, backoff and stats.

        Returns the first successful :class:`GenResult`; raises :class:`GenAIError`
        (UI-safe) when every path failed. Fallback happens only for "this path or
        shape is wrong" failures (400/403/404/empty output/terminal status):
        refusals and exhausted transient errors are raised straight away.
        ``deadline`` (a ``time.perf_counter()`` value) stops the ladder once passed.
        """
        key = memory_key or model
        st = self._stat(model, role)
        st.calls += 1
        failures: list[tuple[str, BaseException]] = []
        for att in (self._ordered(role, key, attempts) if reorder else attempts):
            level = min(self._levels.get((key, att.path), 0), att.levels - 1)
            while True:
                if deadline is not None and time.perf_counter() > deadline and failures:
                    st.errors += 1
                    raise self._error(model, failures)
                t0 = time.perf_counter()
                try:
                    result = await self._with_backoff(lambda: att.run(level), model, att.path)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - every failure becomes a fallback/GenAIError
                    log.warning("%s via %s (level %d) failed: %s", model, att.path, level, _short_reason(exc))
                    failures.append((att.path, exc))
                    if isinstance(exc, _Blocked) or _is_transient(exc):
                        # Refusals are deterministic and quota is per model: another path won't help.
                        st.errors += 1
                        raise self._error(model, failures[-1:]) from exc
                    code = _status_code(exc)
                    shrinkable = code in (400, 422) or isinstance(exc, _EmptyOutput)
                    if shrinkable and level + 1 < att.levels:
                        level += 1
                        continue
                    break
                if not result.latency_ms:
                    result.latency_ms = int((time.perf_counter() - t0) * 1000)
                if att.remember:
                    self._paths[key] = att.path
                    self._levels[(key, att.path)] = level
                st.api_path = result.api_path
                st.latencies.append(result.latency_ms)
                return result
        st.errors += 1
        raise self._error(model, failures)

    def _error(self, model: str, failures: list[tuple[str, BaseException]]) -> GenAIError:
        """Build the UI-safe error: prefer the most informative (non-404) failure."""
        if not failures:
            err = GenAIError(f"{model}: no API path available", model=model)
        else:
            path, exc = next(((p, e) for p, e in failures if _status_code(e) != 404), failures[-1])
            if isinstance(exc, GenAIError):
                err = GenAIError(exc.message, model=model, api_path=exc.api_path or path,
                                 status_code=exc.status_code)
            else:
                err = GenAIError(f"{model} via {path}: {_short_reason(exc)}", model=model,
                                 api_path=path, status_code=_status_code(exc))
        self._stats[model].last_error = err.message if model in self._stats else None
        return err

    async def _timed(self, coro: Awaitable[Any], timeout: float | None = None) -> Any:
        """Await ``coro`` with the per-request timeout."""
        return await asyncio.wait_for(coro, timeout or self._timeout)

    # ── content converters ────────────────────────────────────────────────────

    def _gc_parts(self, parts: Sequence[Any]) -> list[Any]:
        """Convert contract ``parts`` into generate_content contents (str | types.Part)."""
        types = self._types
        out: list[Any] = []
        for p in parts:
            if isinstance(p, str):
                if p:
                    out.append(p)
            elif isinstance(p, (bytes, bytearray)):
                out.append(types.Part.from_bytes(data=bytes(p), mime_type=sniff_mime(bytes(p))))
            elif isinstance(p, tuple) and len(p) == 3:
                _, data, mime = p
                out.append(types.Part.from_bytes(data=data, mime_type=_base_mime(mime) or sniff_mime(data)))
        return out

    @staticmethod
    def _ia_parts(parts: Sequence[Any]) -> list[dict]:
        """Convert contract ``parts`` into Interactions API content dicts."""
        out: list[dict] = []
        for p in parts:
            if isinstance(p, str):
                if p:
                    out.append({"type": "text", "text": p})
            elif isinstance(p, (bytes, bytearray)):
                out.append({"type": "image", "data": _b64(bytes(p)), "mime_type": sniff_mime(bytes(p))})
            elif isinstance(p, tuple) and len(p) == 3:
                kind, data, mime = p
                out.append({"type": kind, "data": _b64(data), "mime_type": _base_mime(mime) or sniff_mime(data)})
        return out

    @staticmethod
    def _ref_tuple(ref: Any) -> tuple[bytes, str]:
        """Accept refs as raw bytes or ``(bytes, mime)`` / ``("image", bytes, mime)`` tuples."""
        if isinstance(ref, tuple):
            data = ref[-2] if len(ref) == 3 else ref[0]
            mime = ref[-1]
            return data, _base_mime(mime) or sniff_mime(data)
        return bytes(ref), sniff_mime(bytes(ref))

    # ── response extraction ───────────────────────────────────────────────────

    @staticmethod
    def _gc_media(resp: Any, prefix: str) -> tuple[bytes, str] | None:
        """First inline media part whose mime starts with ``prefix`` in a generate_content response."""
        for cand in _get(resp, "candidates") or []:
            content = _get(cand, "content")
            for part in _get(content, "parts") or []:
                blob = _get(part, "inline_data")
                mime = _get(blob, "mime_type") or ""
                if blob is not None and mime.startswith(prefix):
                    data = _as_bytes(_get(blob, "data"))
                    if data:
                        return data, mime
        return None

    @staticmethod
    def _gc_empty_error(resp: Any, model: str) -> Exception:
        """Exception for a generate_content response without media.

        Safety/policy refusals become :class:`_Blocked` (final); anything else is an
        :class:`_EmptyOutput` so the shape ladder / fallback can try again.
        """
        feedback = _get(resp, "prompt_feedback")
        block = _get(feedback, "block_reason")
        if block:
            return _Blocked(f"{model}: request blocked by safety filters ({block})", model=model,
                            api_path="generate_content")
        for cand in _get(resp, "candidates") or []:
            reason = _get(cand, "finish_reason")
            if reason and _is_block_reason(reason):
                return _Blocked(f"{model}: output blocked by safety filters ({reason})", model=model,
                                api_path="generate_content")
            if reason and str(reason) not in ("FinishReason.STOP", "STOP"):
                return _EmptyOutput(f"finish reason {reason}")
        try:
            text = (resp.text or "").strip()
        except Exception:  # noqa: BLE001 - .text raises on some multi-part responses
            text = ""
        return _EmptyOutput(f"no media returned ({text[:80]})" if text else "no media returned")

    @staticmethod
    def _ia_find(ia: Any, kind: str) -> Any:
        """Last output content block of ``kind`` (``"image" | "audio" | "video"``) in an interaction.

        Prefers the SDK convenience prop (``output_<kind>``, built from ``steps``) but also scans
        the legacy top-level ``outputs`` list — the SDK only normalises that shape for two named
        Lyria preview ids, so other models answering in it would otherwise look empty.
        """
        found = _get(ia, f"output_{kind}")
        if found is not None:
            return found
        extra = getattr(ia, "model_extra", None) or {}
        outputs = _get(ia, "outputs") or extra.get("outputs") or []
        for step in reversed(_get(ia, "steps") or []):
            if _get(step, "type") == "model_output":
                outputs = list(outputs) + list(_get(step, "content") or [])
        for item in reversed(list(outputs)):
            if _get(item, "type") == kind:
                return item
        return None

    @staticmethod
    def _ia_text(ia: Any) -> str:
        """Text output of an interaction (``output_text`` or the legacy ``outputs`` text items)."""
        text = _get(ia, "output_text")
        if text:
            return text
        extra = getattr(ia, "model_extra", None) or {}
        outputs = _get(ia, "outputs") or extra.get("outputs") or []
        return "".join(str(_get(item, "text") or "") for item in outputs if _get(item, "type") == "text")

    async def _ia_content_bytes(self, content: Any, what: str) -> tuple[bytes, str]:
        """Bytes + mime from an Interactions content block (inline ``data`` or ``uri``)."""
        if content is None:
            raise _EmptyOutput(f"no {what} in interaction output")
        mime = _get(content, "mime_type") or ""
        data = _as_bytes(_get(content, "data"))
        if data:
            return data, mime
        uri = _get(content, "uri")
        if uri:
            return await self._fetch_media(uri), mime
        raise _EmptyOutput(f"{what} output had neither data nor uri")

    async def _fetch_media(self, uri: str) -> bytes:
        """Download a *finished* render, retrying only the download.

        Generation is never re-run from here: a final download failure becomes a
        non-transient :class:`GenAIError`, so the outer backoff does not start a new
        (expensive) render just because fetching the old one flaked.
        """
        last: BaseException | None = None
        for attempt in range(len(BACKOFF_SCHEDULE) + 1):
            try:
                return await self._download(uri)
            except asyncio.CancelledError:
                raise
            except _EmptyOutput:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if attempt >= len(BACKOFF_SCHEDULE) or not (_is_transient(exc) or isinstance(exc, asyncio.TimeoutError)):
                    break
                await asyncio.sleep(BACKOFF_SCHEDULE[attempt])
        raise GenAIError(f"render finished but download failed: {_short_reason(last or Exception('unknown'))}",
                         status_code=None)

    @staticmethod
    def _trusted_host(uri: str) -> bool:
        """Only Google API hosts may receive the API key header."""
        try:
            host = httpx.URL(uri).host or ""
        except Exception:  # noqa: BLE001 - malformed URI: never attach the key
            return False
        return host == "googleapis.com" or host.endswith(".googleapis.com")

    async def _http_get(self, http: httpx.AsyncClient, uri: str) -> httpx.Response:
        """GET following redirects manually so the API key never leaves ``*.googleapis.com``."""
        for _ in range(5):
            headers = {"x-goog-api-key": self.settings.api_key or ""} if self._trusted_host(uri) else {}
            resp = await http.get(uri, headers=headers)
            if resp.is_redirect and resp.headers.get("location"):
                uri = str(resp.url.join(resp.headers["location"]))
                continue
            return resp
        raise GenAIError("download failed: too many redirects", status_code=None)

    async def _download(self, uri: str) -> bytes:
        """Download a generated asset URI (Files API names or HTTPS URLs needing the API key)."""
        if uri.startswith("gs://"):
            raise _EmptyOutput("output was a gs:// URI (needs delivery=inline)")
        if "files/" in uri and (not uri.startswith("http") or "generativelanguage" in uri):
            try:  # Files API resource (name or URI): let the SDK resolve ':download?alt=media'.
                data = await self._timed(self._client.aio.files.download(file=uri), 180)
                if data:
                    return data
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - fall through to a raw authenticated GET
                log.info("files.download(%s) failed (%s); trying direct GET", _redact(uri), _short_reason(exc))
            if not uri.startswith("http"):
                raise _EmptyOutput("could not download generated file")
        async with httpx.AsyncClient(follow_redirects=False, timeout=120.0) as http:
            resp = await self._http_get(http, uri)
            if resp.status_code >= 400 and "alt=media" not in uri and "generativelanguage" in uri:
                resp = await self._http_get(http, uri + ("&" if "?" in uri else "?") + "alt=media")
            resp.raise_for_status()
            if resp.headers.get("content-type", "").startswith("application/json"):
                # Files API metadata instead of media: follow its download link if present.
                meta = resp.json()
                link = meta.get("downloadUri") or _get(meta.get("file"), "downloadUri")
                if link and link != uri:
                    resp = await self._http_get(http, link)
                    resp.raise_for_status()
            return resp.content

    @staticmethod
    def _ia_error(ia: Any) -> str:
        """Human-readable reason for a failed interaction."""
        errs = _get(ia, "errors") or []
        msgs = [str(_get(e, "message") or e) for e in errs if e]
        return "; ".join(msgs)[:160] or f"status {_get(ia, 'status')}"

    async def _await_interaction(self, ia: Any, *, model: str, timeout: float,
                                 on_progress: ProgressCB = None, t0: float | None = None) -> Any:
        """Poll ``interactions.get`` until ``ia`` is terminal; raise on failure/timeout.

        Policy failures raise :class:`_Blocked`; a poll that fails for good raises a
        non-HTTP :class:`GenAIError` (so a 404 on ``get`` is not mistaken for "model
        not on this path"). On timeout or cancellation the server-side interaction is
        cancelled best-effort so abandoned renders stop consuming quota.
        """
        t0 = t0 or time.perf_counter()
        poll = max(0.2, float(self.settings.video_poll_seconds))
        misses = 0
        try:
            while True:
                status = str(_get(ia, "status") or "").lower()
                if status == "completed":
                    return ia
                if status in FAILED_STATES:
                    reason = self._ia_error(ia)
                    cls = _Blocked if _is_block_reason(reason) else GenAIError
                    raise cls(f"{model} via interactions: {status}: {reason}", model=model, api_path="interactions")
                elapsed = time.perf_counter() - t0
                if elapsed > timeout:
                    await self._cancel_interaction(ia)
                    raise GenAIError(f"{model} via interactions: timed out after {int(elapsed)}s",
                                     model=model, api_path="interactions")
                await self._progress(on_progress, "queued" if status == "queued" else "in_progress", t0)
                ia_id = _get(ia, "id")
                if not ia_id:
                    raise _EmptyOutput(f"interaction pending ({status or 'unknown'}) but has no id to poll")
                await asyncio.sleep(poll)
                try:
                    ia = await self._timed(self._client.aio.interactions.get(ia_id), 60)
                    misses = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - tolerate flaky polls
                    misses += 1
                    if misses >= 5 or not (_is_transient(exc) or isinstance(exc, asyncio.TimeoutError)):
                        raise GenAIError(f"{model} via interactions: lost track of {ia_id}: {_short_reason(exc)}",
                                         model=model, api_path="interactions", status_code=None) from exc
                    log.warning("%s poll error (%s); continuing", model, _short_reason(exc))
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_interaction(ia))
            raise

    async def _cancel_interaction(self, ia: Any) -> None:
        """Best-effort server-side cancel of a pending interaction (never raises)."""
        ia_id = _get(ia, "id")
        if not ia_id or str(_get(ia, "status") or "").lower() in FAILED_STATES | {"completed"}:
            return
        try:
            await asyncio.wait_for(self._client.aio.interactions.cancel(ia_id), 10)
        except Exception:  # noqa: BLE001 - cancellation is advisory
            log.info("could not cancel interaction %s", ia_id)

    @staticmethod
    async def _progress(cb: ProgressCB, status: str, t0: float) -> None:
        """Invoke a progress callback, never letting it break the generation."""
        if cb is None:
            return
        try:
            await cb({"status": status, "elapsed_ms": int((time.perf_counter() - t0) * 1000)})
        except Exception:  # noqa: BLE001 - UI callback failures must not kill renders
            log.exception("on_progress callback failed")

    # ── request builders (pure; unit-testable without network) ───────────────

    def _gc_config(self, **fields: Any) -> Any:
        """GenerateContentConfig with automatic function calling disabled (we never pass tools)."""
        types = self._types
        return types.GenerateContentConfig(
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True), **fields)

    def build_image_gc_config(self, *, aspect: str, size: str, seed: int | None, level: int) -> Any:
        """GenerateContentConfig for NB2 images. Level 0: IMAGE only + size + seed; level 1: minimal."""
        types = self._types
        if level == 0:
            return self._gc_config(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect, image_size=size),
                seed=seed,
            )
        return self._gc_config(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect),
        )

    @staticmethod
    def build_image_ia_request(model: str, prompt: str, refs: list[tuple[bytes, str]], *, aspect: str,
                               size: str, seed: int | None, level: int) -> dict:
        """Interactions create() kwargs for an image. Refs (base image first) precede the text."""
        content = [{"type": "image", "data": _b64(d), "mime_type": m} for d, m in refs]
        content.append({"type": "text", "text": prompt})
        fmt: dict[str, Any] = {"type": "image", "aspect_ratio": aspect}
        req: dict[str, Any] = {"model": model, "input": content, "response_modalities": ["image"],
                               "response_format": fmt}
        if level == 0:
            fmt.update({"image_size": size, "delivery": "inline"})
            if seed is not None:
                req["generation_config"] = {"seed": int(seed)}
        return req

    def build_video_ia_request(self, model: str, prompt: str | None, *, image: tuple[bytes, str] | None,
                               video: bytes | None, aspect: str, seconds: int, task: str,
                               previous_interaction_id: str | None, level: int) -> dict:
        """Interactions create() kwargs for Omni video.

        Level 0: full (resolution, duration, inline delivery, background+store);
        level 1: same but synchronous (no ``background``); level 2: minimal format.
        """
        content: list[dict] = []
        if video is not None:
            content.append({"type": "video", "data": _b64(video), "mime_type": "video/mp4"})
        if prompt:
            content.append({"type": "text", "text": prompt})
        if image is not None:
            content.append({"type": "image", "data": _b64(image[0]), "mime_type": image[1]})
        fmt: dict[str, Any] = {"type": "video", "aspect_ratio": aspect}
        if level < 2:
            fmt.update({"resolution": self.settings.video_resolution, "duration": f"{int(seconds)}s",
                        "delivery": "inline"})
        req: dict[str, Any] = {
            "model": model,
            "input": content if len(content) != 1 or content[0]["type"] != "text" else prompt,
            "response_modalities": ["video"],
            "response_format": fmt,
            "generation_config": {"video_config": {"task": task}},
            "store": True,
        }
        if previous_interaction_id:
            req["previous_interaction_id"] = previous_interaction_id
        if level == 0:
            req["background"] = True
        return req

    def build_video_gv_args(self, model: str, prompt: str, *, image: tuple[bytes, str] | None,
                            aspect: str, seconds: int, level: int) -> dict:
        """models.generate_videos kwargs (legacy Veo-style path). Level 1 drops duration/resolution."""
        types = self._types
        src = types.GenerateVideosSource(
            prompt=prompt,
            image=types.Image(image_bytes=image[0], mime_type=image[1]) if image else None,
        )
        cfg: dict[str, Any] = {"aspect_ratio": aspect, "number_of_videos": 1}
        if level == 0:
            cfg.update({"duration_seconds": int(seconds), "resolution": self.settings.video_resolution})
        return {"model": model, "source": src, "config": types.GenerateVideosConfig(**cfg)}

    @staticmethod
    def build_music_ia_request(model: str, prompt: str, *, level: int) -> dict:
        """Interactions create() kwargs for Lyria. Level 1 drops response_format."""
        req: dict[str, Any] = {"model": model, "input": prompt, "response_modalities": ["audio"]}
        if level == 0:
            req["response_format"] = {"type": "audio", "mime_type": "audio/mp3", "delivery": "inline"}
        return req

    @staticmethod
    def speech_prompt(text: str, *, style: str | None, language: str | None) -> str:
        """Gemini TTS prompt: a natural-language delivery directive, then the exact line to speak."""
        style = (style or "").strip().rstrip(".")
        article = "an" if style[:1].lower() in "aeiou" else "a"
        directive = f"Say in {article} {style} voice" if style else "Say"
        # Name non-English languages ("in Telugu"; "Hyderabad · Telugu" -> Telugu); codes go only in the config.
        name = next((w for w in re.split(r"[^A-Za-z]+", (language or "").lower())[::-1] if w in LANGUAGE_CODES), "")
        if name and LANGUAGE_CODES[name] != "en-US":
            directive += f", in {name.capitalize()}"
        return f"{directive}: {text.strip()}"

    def build_speech_gc_config(self, *, voice: str, language: str | None, level: int) -> Any:
        """GenerateContentConfig for Gemini TTS. Level 0: voice + language_code; level 1: voice only."""
        types = self._types
        speech: dict[str, Any] = {"voice_config": types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))}
        code = language_code(language)
        if level == 0 and code:
            speech["language_code"] = code
        return self._gc_config(response_modalities=["AUDIO"], speech_config=types.SpeechConfig(**speech))

    @staticmethod
    def build_speech_ia_request(model: str, prompt: str, *, voice: str, language: str | None, level: int) -> dict:
        """Interactions create() kwargs for TTS. Level 0: speech_config (voice + language); level 1: bare."""
        req: dict[str, Any] = {"model": model, "input": prompt, "response_modalities": ["audio"]}
        if level == 0:
            speaker: dict[str, str] = {"voice": voice}
            code = language_code(language)
            if code:
                speaker["language"] = code
            req["generation_config"] = {"speech_config": [speaker]}
        return req

    @staticmethod
    def build_transcribe_ia_request(model: str, audio: bytes, mime: str, *, level: int) -> dict:
        """Interactions create() kwargs for transcription. Level 1 swaps config for a text instruction."""
        content: list[dict] = [{"type": "audio", "data": _b64(audio), "mime_type": mime}]
        req: dict[str, Any] = {"model": model, "input": content, "response_modalities": ["text"]}
        if level == 0:
            req["generation_config"] = {"transcription_config": {"mode": "verbatim"}}
        else:
            content.append({"type": "text", "text": TRANSCRIBE_INSTRUCTION})
        return req

    def build_json_gc_config(self, schema: dict | None, *, system: str | None, temperature: float,
                             level: int) -> Any:
        """GenerateContentConfig for JSON. Levels: 0 schema+low thinking, 1 schema, 2 JSON mime, 3 plain."""
        types = self._types
        cfg: dict[str, Any] = {"temperature": temperature}
        if system:
            cfg["system_instruction"] = system
        if level <= 2:
            cfg["response_mime_type"] = "application/json"
        if level <= 1 and schema:
            cfg["response_json_schema"] = schema
        if level == 0:
            try:
                cfg["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
            except Exception:  # noqa: BLE001 - older SDKs lack thinking_level
                pass
        return self._gc_config(**cfg)

    # ── public API: text / JSON ────────────────────────────────────────────────

    async def generate_json(self, parts: list, schema: dict, *, system: str | None = None,
                            temperature: float = 0.8, model: str | None = None,
                            mock_context: dict | None = None) -> tuple[dict, GenResult]:
        """Structured JSON generation (planner, judge, direction, localization).

        ``parts`` is a list of strings and :func:`img_part` tuples. Returns the
        parsed dict and the :class:`GenResult` (``mime_type="application/json"``).
        ``mock_context`` (optional, additive) tells mock mode which fake payload to build.
        """
        model = model or self.settings.model_text
        if self.mock:
            return await self._mock_json(parts, schema, model, mock_context)

        async def via_gc(level: int) -> GenResult:
            t0 = time.perf_counter()
            resp = await self._timed(self._client.aio.models.generate_content(
                model=model, contents=self._gc_parts(parts),
                config=self.build_json_gc_config(schema, system=system, temperature=temperature, level=level)))
            text = getattr(resp, "text", None) or ""
            parsed = getattr(resp, "parsed", None)
            data = parsed if isinstance(parsed, dict) else parse_json_loose(text)
            return GenResult(None, "application/json", int((time.perf_counter() - t0) * 1000), model,
                             "generate_content", text=json.dumps(data, ensure_ascii=False), meta={"parsed": data})

        async def via_ia(level: int) -> GenResult:
            t0 = time.perf_counter()
            content = self._ia_parts(parts)
            req: dict[str, Any] = {"model": model, "input": content}
            if system:
                req["system_instruction"] = system
            if level == 0:
                req["response_format"] = {"type": "text", "mime_type": "application/json", "schema": schema}
                req["generation_config"] = {"thinking_level": "low"}
            else:
                content.append({"type": "text", "text": "Respond with ONLY a JSON object matching this JSON "
                                                        "Schema:\n" + json.dumps(schema)})
            ia = await self._timed(self._client.aio.interactions.create(**req))
            ia = await self._await_interaction(ia, model=model, timeout=self._timeout)
            text = self._ia_text(ia)
            data = parse_json_loose(text)
            return GenResult(None, "application/json", int((time.perf_counter() - t0) * 1000), model,
                             "interactions", text=json.dumps(data, ensure_ascii=False), meta={"parsed": data})

        async with self._slot("text"):
            res = await self._execute("text", model, [
                _Attempt("generate_content", via_gc, levels=4),
                _Attempt("interactions", via_ia, levels=2),
            ])
        return res.meta.pop("parsed"), res

    # ── public API: transcription ────────────────────────────────────────────────

    async def transcribe(self, audio: bytes, mime_type: str) -> GenResult:
        """Voice brief -> text (``GenResult.text``)."""
        mime = _base_mime(mime_type) or "audio/webm"
        model = self.settings.model_transcribe
        if self.mock:
            return await self._mock_transcribe(audio, model)

        async def via_ia(level: int) -> GenResult:
            t0 = time.perf_counter()
            ia = await self._timed(self._client.aio.interactions.create(
                **self.build_transcribe_ia_request(model, audio, mime, level=level)))
            ia = await self._await_interaction(ia, model=model, timeout=self._timeout)
            text = self._ia_text(ia).strip()
            if not text:
                raise _EmptyOutput("empty transcript")
            return GenResult(None, "text/plain", int((time.perf_counter() - t0) * 1000), model,
                             "interactions", text=text)

        text_model = self.settings.model_text

        async def via_gc(level: int) -> GenResult:
            t0 = time.perf_counter()
            resp = await self._timed(self._client.aio.models.generate_content(
                model=text_model,
                contents=[self._types.Part.from_bytes(data=audio, mime_type=mime), TRANSCRIBE_INSTRUCTION],
                config=self._gc_config(temperature=0.0)))
            text = (getattr(resp, "text", None) or "").strip()
            if not text:
                raise _EmptyOutput("empty transcript")
            return GenResult(None, "text/plain", int((time.perf_counter() - t0) * 1000), text_model,
                             "generate_content", text=text)

        async with self._slot("text"):
            return await self._execute("transcribe", model, [
                _Attempt("interactions", via_ia, levels=2),
                _Attempt("generate_content", via_gc, levels=1),
            ])

    # ── public API: images ──────────────────────────────────────────────────────

    async def generate_image(self, prompt: str, *, refs: Sequence[Any] = (), aspect: str = "16:9",
                             size: str = "1K", seed: int | None = None) -> GenResult:
        """Nano Banana 2 Lite image (storyboard variant, anchor, repair edit, localization).

        ``refs`` are continuity references — for edits the BASE image must be first.
        They are sent as inline parts BEFORE the text prompt.
        """
        model = self.settings.model_image
        ref_list = [self._ref_tuple(r) for r in refs if r]
        if self.mock:
            return await self._mock_image(prompt, ref_list, aspect, model)

        async def via_gc(level: int) -> GenResult:
            t0 = time.perf_counter()
            types = self._types
            contents = [types.Part.from_bytes(data=d, mime_type=m) for d, m in ref_list] + [prompt]
            resp = await self._timed(self._client.aio.models.generate_content(
                model=model, contents=contents,
                config=self.build_image_gc_config(aspect=aspect, size=size, seed=seed, level=level)))
            media = self._gc_media(resp, "image/")
            if not media:
                raise self._gc_empty_error(resp, model)
            data, mime = media
            return GenResult(data, _base_mime(mime) or sniff_mime(data), int((time.perf_counter() - t0) * 1000),
                             model, "generate_content")

        async def via_ia(level: int) -> GenResult:
            t0 = time.perf_counter()
            ia = await self._timed(self._client.aio.interactions.create(**self.build_image_ia_request(
                model, prompt, ref_list, aspect=aspect, size=size, seed=seed, level=level)))
            ia = await self._await_interaction(ia, model=model, timeout=self._timeout, t0=t0)
            data, mime = await self._ia_content_bytes(self._ia_find(ia, "image"), "image")
            return GenResult(data, _base_mime(mime) or sniff_mime(data), int((time.perf_counter() - t0) * 1000),
                             model, "interactions", meta={"interaction_id": _get(ia, "id")})

        async with self._slot("image"):
            return await self._execute("image", model, [
                _Attempt("generate_content", via_gc, levels=2),
                _Attempt("interactions", via_ia, levels=2),
            ])

    # ── public API: video ─────────────────────────────────────────────────────────

    def _video_ia_attempt(self, model: str, prompt: str | None, *, image: tuple[bytes, str] | None,
                          video: bytes | None, aspect: str, seconds: int, task: str,
                          previous_interaction_id: str | None, on_progress: ProgressCB,
                          fallback: str | None, path_label: str = "interactions",
                          remember: bool = True) -> _Attempt:
        """An Omni Interactions attempt (create -> poll -> fetch mp4) with the 3-level shape ladder."""
        timeout = float(self.settings.video_timeout_seconds)

        async def run(level: int) -> GenResult:
            t0 = time.perf_counter()
            req = self.build_video_ia_request(model, prompt, image=image, video=video, aspect=aspect,
                                              seconds=seconds, task=task,
                                              previous_interaction_id=previous_interaction_id, level=level)
            await self._progress(on_progress, "queued", t0)
            ia = await self._timed(self._client.aio.interactions.create(**req), timeout)
            ia = await self._await_interaction(ia, model=model, timeout=timeout, on_progress=on_progress, t0=t0)
            data, mime = await self._ia_content_bytes(self._ia_find(ia, "video"), "video")
            return GenResult(data, _base_mime(mime) or "video/mp4", int((time.perf_counter() - t0) * 1000),
                             model, "interactions",
                             meta={"interaction_id": _get(ia, "id"), "operation": None, "fallback": fallback})

        return _Attempt(path_label, run, levels=3, remember=remember)

    def _video_gv_attempt(self, model: str, prompt: str, *, image: tuple[bytes, str] | None, aspect: str,
                          seconds: int, on_progress: ProgressCB, fallback: str | None) -> _Attempt:
        """A legacy ``generate_videos`` attempt (operation polling + Files API download)."""
        timeout = float(self.settings.video_timeout_seconds)
        poll = max(0.2, float(self.settings.video_poll_seconds))

        async def run(level: int) -> GenResult:
            t0 = time.perf_counter()
            await self._progress(on_progress, "queued", t0)
            op = await self._timed(self._client.aio.models.generate_videos(
                **self.build_video_gv_args(model, prompt, image=image, aspect=aspect, seconds=seconds, level=level)))
            misses = 0
            while not _get(op, "done"):
                if time.perf_counter() - t0 > timeout:
                    raise GenAIError(f"{model} via generate_videos: timed out after {int(timeout)}s",
                                     model=model, api_path="generate_videos")
                await self._progress(on_progress, "in_progress", t0)
                await asyncio.sleep(poll)
                try:
                    op = await self._timed(self._client.aio.operations.get(op), 60)
                    misses = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - tolerate flaky polls
                    misses += 1
                    if misses >= 5 or not (_is_transient(exc) or isinstance(exc, asyncio.TimeoutError)):
                        raise
            if _get(op, "error"):
                err = _get(op, "error")
                raise GenAIError(f"{model} via generate_videos: {str(_get(err, 'message') or err)[:160]}",
                                 model=model, api_path="generate_videos")
            resp = _get(op, "response") or _get(op, "result")
            vids = _get(resp, "generated_videos") or []
            if not vids:
                reasons = _get(resp, "rai_media_filtered_reasons")
                if reasons:
                    raise _Blocked(f"{model}: video blocked by safety filters ({str(reasons)[:120]})",
                                   model=model, api_path="generate_videos")
                raise _EmptyOutput("no video returned")
            vid = _get(vids[0], "video")
            data = _as_bytes(_get(vid, "video_bytes"))
            if not data and vid is not None:
                try:
                    data = await self._timed(self._client.aio.files.download(file=vid), 180)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - fall back to a direct URI download below
                    log.info("%s: files.download failed (%s); trying the URI", model, _short_reason(exc))
            if not data and _get(vid, "uri"):
                data = await self._fetch_media(_get(vid, "uri"))
            if not data:
                raise _EmptyOutput("video had no downloadable bytes")
            return GenResult(data, _get(vid, "mime_type") or "video/mp4", int((time.perf_counter() - t0) * 1000),
                             model, "generate_videos",
                             meta={"interaction_id": None, "operation": _get(op, "name"), "fallback": fallback})

        return _Attempt("generate_videos", run, levels=2)

    async def generate_video(self, prompt: str, *, image: bytes | None, aspect: str, seconds: int,
                             on_progress: ProgressCB = None) -> GenResult:
        """Omni image-to-video (or text-to-video when ``image`` is None) -> mp4 bytes.

        ``meta.interaction_id`` is set when the Interactions path was used (needed
        for multi-turn :meth:`edit_video`). ``on_progress`` receives
        ``{"status": "queued|in_progress", "elapsed_ms": int}`` every poll.
        """
        model = self.settings.model_video
        if self.mock:
            return await self._mock_video(prompt, image, aspect, seconds, model, on_progress)
        img = (image, sniff_mime(image)) if image else None
        task = "image_to_video" if img else "text_to_video"
        async with self._slot("video"):
            return await self._execute("video", model, [
                self._video_ia_attempt(model, prompt, image=img, video=None, aspect=aspect, seconds=seconds,
                                       task=task, previous_interaction_id=None, on_progress=on_progress,
                                       fallback=None),
                self._video_gv_attempt(model, prompt, image=img, aspect=aspect, seconds=seconds,
                                       on_progress=on_progress, fallback=None),
            ])

    async def edit_video(self, instruction: str, *, previous_interaction_id: str | None,
                         video: bytes | None, image: bytes | None, aspect: str, seconds: int,
                         on_progress: ProgressCB = None, base_prompt: str | None = None) -> GenResult:
        """Conversational Omni edit of an existing clip.

        Ladder: (1) multi-turn ``previous_interaction_id`` + task ``edit``
        (``meta.fallback=None``); (2) single-turn edit with the prior clip bytes
        (``"single_turn"``); (3) re-render image-to-video from ``image`` with the
        instruction merged into ``base_prompt`` (``"rerender"``). ``base_prompt``
        is an additive optional kwarg (the scene's motion prompt).
        """
        model = self.settings.model_video
        if self.mock:
            return await self._mock_edit(instruction, previous_interaction_id, image, aspect, seconds, model,
                                         on_progress)
        img = (image, sniff_mime(image)) if image else None
        edit_text = (f"Edit this clip: {instruction.strip()}. Keep everything else identical — subject identity, "
                     f"product shape, palette, framing and timing — unless the instruction changes it.")
        rerender_prompt = (f"{base_prompt.strip()}\n\nDirector's note for this take (takes priority): "
                           f"{instruction.strip()}") if base_prompt else instruction.strip()
        attempts: list[_Attempt] = []
        # Mock-mode ids (runs replayed from disk) mean nothing to the live API.
        if previous_interaction_id and not previous_interaction_id.startswith("mock-"):
            attempts.append(self._video_ia_attempt(
                model, edit_text, image=None, video=None, aspect=aspect, seconds=seconds, task="edit",
                previous_interaction_id=previous_interaction_id, on_progress=on_progress, fallback=None,
                path_label="interactions:multi_turn", remember=False))
        if video:
            attempts.append(self._video_ia_attempt(
                model, edit_text, image=None, video=video, aspect=aspect, seconds=seconds, task="edit",
                previous_interaction_id=None, on_progress=on_progress, fallback="single_turn",
                path_label="interactions:single_turn", remember=False))
        if img:
            rerender = [self._video_ia_attempt(model, rerender_prompt, image=img, video=None, aspect=aspect,
                                               seconds=seconds, task="image_to_video",
                                               previous_interaction_id=None, on_progress=on_progress,
                                               fallback="rerender", remember=False),
                        self._video_gv_attempt(model, rerender_prompt, image=img, aspect=aspect, seconds=seconds,
                                               on_progress=on_progress, fallback="rerender")]
            # Re-render straight on the path that already works for plain generation.
            if self._paths.get(model) == "generate_videos":
                rerender.reverse()
            for att in rerender:
                att.remember = False
            attempts.extend(rerender)
        if not attempts:
            raise GenAIError(f"{model}: nothing to edit (no interaction id, clip or keyframe)", model=model)
        async with self._slot("video"):
            # Edit ladder order is semantic (multi-turn > single-turn > re-render): never reorder it.
            # The deadline bounds how long one edit can hold a video slot across the whole ladder.
            deadline = time.perf_counter() + 1.5 * float(self.settings.video_timeout_seconds)
            return await self._execute("video", model, attempts, reorder=False, deadline=deadline)

    # ── public API: music ─────────────────────────────────────────────────────────

    async def generate_music(self, prompt: str, *, seconds: int) -> GenResult:
        """Lyria soundtrack -> mp3 (``audio/mpeg``) or wav bytes."""
        model = self.settings.model_music
        full_prompt = prompt if f"{int(seconds)} second" in prompt else (
            f"{prompt.rstrip()}\nTotal length: {int(seconds)} seconds.")
        if self.mock:
            return await self._mock_music(full_prompt, seconds, model)

        music_timeout = max(self._timeout, 240.0)  # full-length Lyria renders can be slow

        async def via_ia(level: int) -> GenResult:
            t0 = time.perf_counter()
            ia = await self._timed(self._client.aio.interactions.create(
                **self.build_music_ia_request(model, full_prompt, level=level)), music_timeout)
            ia = await self._await_interaction(ia, model=model, timeout=music_timeout, t0=t0)
            data, mime = await self._ia_content_bytes(self._ia_find(ia, "audio"), "audio")
            data, mime = _normalize_audio(data, mime)
            return GenResult(data, mime, int((time.perf_counter() - t0) * 1000), model, "interactions",
                             meta={"interaction_id": _get(ia, "id")})

        async def via_gc(level: int) -> GenResult:
            t0 = time.perf_counter()
            resp = await self._timed(self._client.aio.models.generate_content(
                model=model, contents=full_prompt,
                config=self._gc_config(response_modalities=["AUDIO"])), music_timeout)
            media = self._gc_media(resp, "audio/")
            if not media:
                raise self._gc_empty_error(resp, model)
            data, mime = _normalize_audio(*media)
            return GenResult(data, mime, int((time.perf_counter() - t0) * 1000), model, "generate_content")

        async with self._slot("music"):
            return await self._execute("music", model, [
                _Attempt("interactions", via_ia, levels=2),
                _Attempt("generate_content", via_gc, levels=1),
            ])

    # ── public API: speech (voiceover) ─────────────────────────────────────────────

    async def generate_speech(self, text: str, *, voice: str = "Kore", style: str | None = None,
                              language: str | None = None) -> GenResult:
        """Gemini Flash TTS narration line -> WAV bytes (``audio/wav``).

        ``voice`` is a prebuilt voice name (see :data:`TTS_VOICES`; unknown names fall
        back to Kore), ``style`` a natural-language delivery note ("warm, confident")
        and ``language`` a language name or BCP-47 code used as a pronunciation hint.
        Raw PCM from the API is wrapped in a WAV header; wav/mp3 pass through.
        """
        model = self.settings.model_tts
        text = " ".join((text or "").split())
        if not text:
            raise GenAIError(f"{model}: nothing to say (empty voiceover line)", model=model)
        voice = next((v for v in TTS_VOICES if v.lower() == (voice or "").strip().lower()), "Kore")
        if self.mock:
            return await self._mock_speech(text, voice, model)
        prompt = self.speech_prompt(text, style=style, language=language)
        meta = {"voice": voice, "text": text}

        def finish(data: bytes, mime: str, t0: float, path: str) -> GenResult:
            wav, wav_mime = _normalize_audio(data, mime, pcm_rate=TTS_PCM_RATE, unknown_is_pcm=True)
            return GenResult(wav, wav_mime, int((time.perf_counter() - t0) * 1000), model, path, text=text,
                             meta=dict(meta))

        async def via_gc(level: int) -> GenResult:
            t0 = time.perf_counter()
            resp = await self._timed(self._client.aio.models.generate_content(
                model=model, contents=prompt,
                config=self.build_speech_gc_config(voice=voice, language=language, level=level)))
            media = self._gc_media(resp, "audio/")
            if not media:
                raise self._gc_empty_error(resp, model)
            return finish(*media, t0, "generate_content")

        async def via_ia(level: int) -> GenResult:
            t0 = time.perf_counter()
            ia = await self._timed(self._client.aio.interactions.create(
                **self.build_speech_ia_request(model, prompt, voice=voice, language=language, level=level)))
            ia = await self._await_interaction(ia, model=model, timeout=self._timeout, t0=t0)
            data, mime = await self._ia_content_bytes(self._ia_find(ia, "audio"), "audio")
            return finish(data, mime, t0, "interactions")

        async with self._slot("tts"):
            return await self._execute("tts", model, [
                _Attempt("generate_content", via_gc, levels=2),
                _Attempt("interactions", via_ia, levels=2),
            ])

    # ── mock implementations ────────────────────────────────────────────────────────

    async def _mock_sleep(self, lo: float, hi: float | None = None) -> float:
        """Sleep a jittered, speed-scaled duration; returns the nominal seconds."""
        seconds = random.uniform(lo, hi) if hi is not None else lo * random.uniform(0.9, 1.1)
        await asyncio.sleep(seconds * self.settings.mock_speed)
        return seconds

    def _mock_done(self, role: str, model: str, t0: float) -> int:
        """Record a mock call in stats and return its latency in ms."""
        latency = int((time.perf_counter() - t0) * 1000)
        st = self._stat(model, role)
        st.calls += 1
        st.api_path = "mock"
        st.latencies.append(latency)
        return latency

    async def _mock_json(self, parts: list, schema: dict, model: str,
                         ctx: dict | None) -> tuple[dict, GenResult]:
        async with self._slot("text"):
            t0 = time.perf_counter()
            kind = (ctx or {}).get("kind") or mockgen.detect_kind(schema)
            await self._mock_sleep(1.6 if kind == "plan" else 0.8)
            data = mockgen.build_json(kind, ctx or {}, parts)
            latency = self._mock_done("text", model, t0)
        return data, GenResult(None, "application/json", latency, model, "mock",
                               text=json.dumps(data, ensure_ascii=False))

    async def _mock_transcribe(self, audio: bytes, model: str) -> GenResult:
        async with self._slot("text"):
            t0 = time.perf_counter()
            await self._mock_sleep(0.8)
            text = mockgen.fake_transcript(audio)
            return GenResult(None, "text/plain", self._mock_done("transcribe", model, t0), model, "mock", text=text)

    async def _mock_image(self, prompt: str, refs: list[tuple[bytes, str]], aspect: str, model: str) -> GenResult:
        async with self._slot("image"):
            t0 = time.perf_counter()
            key = mockgen.prompt_key(prompt)
            k = self._mock_counters.get(key, 0)
            self._mock_counters[key] = k + 1
            render = asyncio.to_thread(mockgen.render_image, prompt, aspect=aspect, variant=k,
                                       has_refs=bool(refs))
            data, _ = await asyncio.gather(render, self._mock_sleep(0.6, 1.8))
            return GenResult(data, "image/png", self._mock_done("image", model, t0), model, "mock")

    async def _mock_clip(self, image: bytes, aspect: str, seconds: int, on_progress: ProgressCB) -> bytes:
        """Ken-Burns an image into an mp4 while emitting progress ticks for 6–12 s (speed-scaled)."""
        try:
            from app.media import ken_burns  # lazy: owned by builder B
        except Exception as exc:  # noqa: BLE001
            raise GenAIError(f"mock video unavailable: {_short_reason(exc)}", api_path="mock") from exc
        t0 = time.perf_counter()
        target = random.uniform(6.0, 12.0) * self.settings.mock_speed
        render = asyncio.create_task(ken_burns(image, int(seconds), aspect))
        try:
            await self._progress(on_progress, "queued", t0)
            tick = max(0.05, min(1.0, target / 6))
            while time.perf_counter() - t0 < target or not render.done():
                await asyncio.sleep(tick)
                await self._progress(on_progress, "in_progress", t0)
            return await render
        except GenAIError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - ffmpeg failures become UI-safe errors
            raise GenAIError(f"mock video render failed: {_short_reason(exc)}", api_path="mock") from exc
        finally:
            if not render.done():
                render.cancel()

    async def _mock_video(self, prompt: str, image: bytes | None, aspect: str, seconds: int, model: str,
                          on_progress: ProgressCB) -> GenResult:
        async with self._slot("video"):
            t0 = time.perf_counter()
            frame = image or await asyncio.to_thread(mockgen.render_image, prompt, aspect=aspect, variant=0)
            data = await self._mock_clip(frame, aspect, seconds, on_progress)
            return GenResult(data, "video/mp4", self._mock_done("video", model, t0), model, "mock",
                             meta={"interaction_id": f"mock-{uuid.uuid4().hex[:12]}", "operation": None,
                                   "fallback": None})

    async def _mock_edit(self, instruction: str, previous_interaction_id: str | None, image: bytes | None,
                         aspect: str, seconds: int, model: str, on_progress: ProgressCB) -> GenResult:
        async with self._slot("video"):
            t0 = time.perf_counter()
            if image:
                frame = await asyncio.to_thread(mockgen.tint_image, image, instruction)
            else:
                frame = await asyncio.to_thread(mockgen.render_image, instruction, aspect=aspect, variant=1)
            data = await self._mock_clip(frame, aspect, seconds, on_progress)
            return GenResult(data, "video/mp4", self._mock_done("video", model, t0), model, "mock",
                             meta={"interaction_id": f"mock-{uuid.uuid4().hex[:12]}", "operation": None,
                                   "fallback": None if previous_interaction_id else "rerender"})

    async def _mock_speech(self, text: str, voice: str, model: str) -> GenResult:
        async with self._slot("tts"):
            t0 = time.perf_counter()
            synth = asyncio.to_thread(mockgen.synth_speech, text, voice)
            data, _ = await asyncio.gather(synth, self._mock_sleep(0.4, 1.0))
            return GenResult(data, "audio/wav", self._mock_done("tts", model, t0), model, "mock", text=text,
                             meta={"voice": voice, "text": text})

    async def _mock_music(self, prompt: str, seconds: int, model: str) -> GenResult:
        async with self._slot("music"):
            t0 = time.perf_counter()
            synth = asyncio.to_thread(mockgen.synth_music, prompt, int(seconds))
            data, _ = await asyncio.gather(synth, self._mock_sleep(4.0))
            return GenResult(data, "audio/wav", self._mock_done("music", model, t0), model, "mock")


#: Instruction used when a transcription model needs an explicit text prompt.
TRANSCRIBE_INSTRUCTION = (
    "Transcribe this voice recording verbatim in its original language. Output only the transcript text — "
    "no timestamps, speaker labels, quotes or commentary."
)
