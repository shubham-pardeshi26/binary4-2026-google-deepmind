"""ffmpeg-backed media utilities: probing, Ken-Burns mock clips, and the final-cut stitcher.

We rely on the static ffmpeg binary bundled by ``imageio-ffmpeg`` (no system ffmpeg / ffprobe is required).
Because there is no ffprobe, :func:`probe` parses the human-readable stream summary that ``ffmpeg -i <file>``
prints on stderr (``Duration: 00:00:06.03`` / ``Stream #0:0: Video: h264 ... 1280x720 ... 30 fps`` / ``Audio:``).

All ffmpeg invocations run as asyncio subprocesses with a timeout, so they never block the event loop, and
every output file is written to a temporary name and atomically renamed into place so the HTTP layer can never
serve a half-written asset.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("adloop.media")

FPS = 30
#: Default per-command wall-clock limit for ffmpeg (seconds). A 6-scene 1080p stitch takes well under 60s.
FFMPEG_TIMEOUT_S = 300

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/L16": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/aac": ".aac",
    "audio/mp4": ".m4a",
    "application/json": ".json",
    "text/plain": ".txt",
}


class MediaError(RuntimeError):
    """Raised when an ffmpeg command fails; the message carries a short tail of ffmpeg's stderr."""


@dataclass
class MediaInfo:
    """What we could learn about a media file from ``ffmpeg -i``."""

    duration: float
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None
    fps: float | None = None


# --------------------------------------------------------------------------- basics
@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """Path to an ffmpeg binary: system ffmpeg if on PATH, else the imageio-ffmpeg bundled binary."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # imported lazily: only needed when no system ffmpeg exists

    return imageio_ffmpeg.get_ffmpeg_exe()


def ffmpeg_available() -> bool:
    """True if an ffmpeg binary can be located (used by /api/health)."""
    try:
        return bool(ffmpeg_exe()) and os.path.exists(ffmpeg_exe())
    except Exception:
        return False


def ext_for_mime(mime: str) -> str:
    """File extension (with dot) for a MIME type; unknown types fall back to ``.bin``."""
    base = (mime or "").split(";")[0].strip().lower()
    if base in _MIME_EXT:
        return _MIME_EXT[base]
    for key, ext in _MIME_EXT.items():  # case-insensitive match for things like audio/L16
        if key.lower() == base:
            return ext
    return ".bin"


def save_bytes(run_dir: Path, name: str, data: bytes) -> Path:
    """Atomically write ``data`` to ``run_dir/name`` and return the final path."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / name
    tmp = run_dir / f".{name}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def frame_size(aspect: str) -> tuple[int, int]:
    """Output frame size for an aspect ratio: 1280x720 (16:9) or 720x1280 (9:16)."""
    return (720, 1280) if str(aspect).strip() == "9:16" else (1280, 720)


async def _exec(args: list[str], timeout: float) -> tuple[int, str]:
    """Run ffmpeg with ``args``; return (returncode, stderr text). Kills the process on timeout."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_exe(), *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return proc.returncode or 0, err.decode("utf-8", "replace")


async def run_ffmpeg(args: list[str], *, timeout: float = FFMPEG_TIMEOUT_S) -> None:
    """Run ffmpeg (``-hide_banner -y -loglevel error`` prepended); raise MediaError with a stderr tail on failure."""
    try:
        code, err = await _exec(["-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args], timeout)
    except asyncio.TimeoutError as exc:
        raise MediaError(f"ffmpeg timed out after {timeout:.0f}s") from exc
    if code != 0:
        tail = " | ".join(line.strip() for line in err.strip().splitlines()[-4:])
        log.debug("ffmpeg stderr: %s", err)
        raise MediaError(f"ffmpeg failed ({code}): {tail[-400:] or 'no stderr'}")


# --------------------------------------------------------------------------- probing
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_TIME_RE = re.compile(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+.*?: Video:.*?(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


def _hms(m: re.Match) -> float:
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


async def probe(path: Path) -> MediaInfo:
    """Inspect a media file via ``ffmpeg -i`` stderr. Falls back to a full decode if Duration is N/A."""
    path = Path(path)
    if not path.exists():
        raise MediaError(f"missing media file: {path.name}")
    _, err = await _exec(["-hide_banner", "-nostdin", "-i", str(path)], timeout=30)
    duration = 0.0
    m = _DUR_RE.search(err)
    if m:
        duration = _hms(m)
    has_video = bool(re.search(r"Stream #\S+.*?: Video:", err))
    has_audio = bool(re.search(r"Stream #\S+.*?: Audio:", err))
    width = height = None
    fps = None
    vm = _VIDEO_RE.search(err)
    if vm:
        width, height = int(vm.group(1)), int(vm.group(2))
        line = err[vm.start(): err.find("\n", vm.start())]
        fm = _FPS_RE.search(line)
        if fm:
            fps = float(fm.group(1))
    if duration <= 0 and (has_video or has_audio):
        # Container lacks a duration header (e.g. some streamed WAV/WebM) -> decode to null and read last time=.
        _, err2 = await _exec(["-hide_banner", "-nostdin", "-i", str(path), "-f", "null", "-"], timeout=120)
        times = _TIME_RE.findall(err2)
        if times:
            h, mnt, s = times[-1]
            duration = int(h) * 3600 + int(mnt) * 60 + float(s)
    if not (has_video or has_audio):
        raise MediaError(f"unreadable media file: {path.name}")
    return MediaInfo(duration=duration, has_video=has_video, has_audio=has_audio,
                     width=width, height=height, fps=fps)


async def probe_duration(path: Path) -> float:
    """Duration of a media file in seconds (0.0 if it cannot be determined)."""
    return (await probe(path)).duration


# --------------------------------------------------------------------------- ken burns
async def ken_burns(image_bytes: bytes, seconds: int, aspect: str) -> bytes:
    """Render a still image as a slow push-in MP4 (h264, yuv420p, 30fps, 1280x720 or 720x1280, no audio).

    Used by mock mode to stand in for Omni image-to-video. The image is cover-scaled to 2x the output size first
    (so zoompan's integer crop offsets don't produce visible jitter), then zoomed 1.00 -> ~1.12 around the centre.
    """
    w, h = frame_size(aspect)
    seconds = max(1, int(seconds or 6))
    frames = seconds * FPS
    zstep = 0.12 / frames
    vf = (
        f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
        f"zoompan=z='min(1+{zstep:.6f}*on,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={w}x{h}:fps={FPS},setsar=1,format=yuv420p"
    )
    with tempfile.TemporaryDirectory(prefix="adloop_kb_") as td:
        src = Path(td) / "src.img"
        dst = Path(td) / "out.mp4"
        src.write_bytes(image_bytes)
        await run_ffmpeg([
            "-i", str(src), "-vf", vf, "-frames:v", str(frames), "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-an", str(dst),
        ], timeout=120)
        return dst.read_bytes()


# --------------------------------------------------------------------------- stitch
def _keep_clip_audio() -> bool:
    return os.getenv("ADLOOP_KEEP_CLIP_AUDIO", "0").strip().lower() in {"1", "true", "yes", "on"}


async def stitch(clips: list[Path], music: Path | None, out: Path, *, aspect: str,
                 fade_s: float = 0.35) -> float:
    """Concatenate ``clips`` into one H.264 MP4 at ``out`` with an optional soundtrack; return its duration.

    Robust to heterogeneous inputs: every clip is scaled-to-fit + padded to the target frame (1280x720 or
    720x1280), resampled to 30fps / SAR 1 / yuv420p, and padded-then-trimmed to exactly its probed duration so
    the crossfade offsets are exact even if a stream ends a few frames early. Consecutive clips are joined with
    an ``xfade`` of ``fade_s`` seconds (hard ``concat`` if ``fade_s`` <= 0 or any clip is too short to fade).

    Audio: the music track (if any) is loudness-normalised (EBU R128, -16 LUFS), padded/trimmed to the video
    length and faded out over the final 1.2s. Clip audio is dropped unless ``ADLOOP_KEEP_CLIP_AUDIO=1``, in
    which case it is mixed under the music at 0.35 (clips without an audio stream contribute silence). If
    there is neither music nor kept clip audio, the output has no audio stream.
    """
    clips = [Path(c) for c in clips]
    if not clips:
        raise MediaError("nothing to stitch: no clips")
    out = Path(out)
    w, h = frame_size(aspect)
    infos = [await probe(c) for c in clips]
    for c, info in zip(clips, infos):
        if not info.has_video:
            raise MediaError(f"clip has no video stream: {c.name}")
    durs = [max(0.5, info.duration or 0.0) for info in infos]
    n = len(clips)

    fade = max(0.0, float(fade_s or 0.0))
    if n < 2 or any(d < 2 * fade + 0.2 for d in durs):
        fade = 0.0
    total = sum(durs) - fade * (n - 1)

    keep_audio = _keep_clip_audio()
    music_ok = music is not None and Path(music).exists()

    args: list[str] = []
    for c in clips:
        args += ["-i", str(c)]
    if music_ok:
        args += ["-i", str(music)]

    fc: list[str] = []
    for i, d in enumerate(durs):
        fc.append(
            f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={FPS},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration=2,trim=duration={d:.3f},setpts=PTS-STARTPTS,"
            # setpts drops the link's frame-rate metadata, which xfade requires -> re-assert CFR + timebase.
            f"fps={FPS},settb=1/{FPS}[v{i}]"
        )
    if n == 1:
        fc.append("[v0]null[vout]")
    elif fade > 0:
        prev = "v0"
        offset = 0.0
        for i in range(1, n):
            offset += durs[i - 1] - fade
            label = "vout" if i == n - 1 else f"x{i}"
            fc.append(f"[{prev}][v{i}]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}[{label}]")
            prev = label
    else:
        fc.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]")

    audio_labels: list[str] = []
    afmt = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if keep_audio:
        for i, (d, info) in enumerate(zip(durs, infos)):
            seg = d - (fade if i < n - 1 else 0.0)
            if info.has_audio:
                fc.append(f"[{i}:a:0]{afmt},apad,atrim=duration={seg:.3f},asetpts=PTS-STARTPTS[ca{i}]")
            else:
                fc.append(f"anullsrc=r=44100:cl=stereo,atrim=duration={seg:.3f},asetpts=PTS-STARTPTS[ca{i}]")
        fc.append("".join(f"[ca{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1,volume=0.35[clipa]")
        audio_labels.append("clipa")
    if music_ok:
        fade_out = min(1.2, max(0.1, total / 4))
        fc.append(
            f"[{n}:a:0]{afmt},loudnorm=I=-16:TP=-1.5:LRA=11,{afmt},apad,atrim=duration={total:.3f},"
            f"afade=t=out:st={max(0.0, total - fade_out):.3f}:d={fade_out:.3f},asetpts=PTS-STARTPTS[mus]"
        )
        audio_labels.append("mus")
    if len(audio_labels) == 2:
        fc.append("[mus][clipa]amix=inputs=2:duration=first:normalize=0[aout]")
        a_out = "aout"
    elif audio_labels:
        a_out = audio_labels[0]
    else:
        a_out = None

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.stem}.{uuid.uuid4().hex[:8]}.tmp.mp4")
    args += ["-filter_complex", ";".join(fc), "-map", "[vout]"]
    if a_out:
        args += ["-map", f"[{a_out}]", "-c:a", "aac", "-b:a", "192k", "-ar", "44100"]
    else:
        args += ["-an"]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-t", f"{total:.3f}", "-movflags", "+faststart", str(tmp),
    ]
    try:
        await run_ffmpeg(args)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return await probe_duration(out)
