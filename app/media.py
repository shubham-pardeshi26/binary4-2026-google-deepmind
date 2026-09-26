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


# --------------------------------------------------------------------------- voiceover timeline
#: Narration starts this long after its scene begins (lets the cut land before the voice does).
VO_LEAD_IN_S = 0.25
#: A line must end this long before its scene ends, otherwise it is sped up (up to VO_MAX_TEMPO).
VO_TAIL_S = 0.3
VO_MAX_TEMPO = 1.2
#: Minimum silence kept between two consecutive lines when one spills into the next scene.
VO_GAP_S = 0.1


@dataclass
class VoicePlacement:
    """Where one scene's narration line sits in the final cut (seconds on the output timeline)."""

    index: int          # scene / clip index the line belongs to
    start: float        # output time the (possibly sped-up) line starts
    end: float          # output time it ends
    tempo: float        # atempo factor applied (1.0 = untouched)


def place_voiceovers(scene_durs: list[float], fade_s: float,
                     vo_durs: list[float | None]) -> list[VoicePlacement]:
    """Lay narration lines out on the cut's timeline (pure function; also used to time the captions).

    Scene ``i`` starts at ``sum(scene_durs[:i]) - i * fade_s`` (crossfades overlap consecutive clips). Each line
    starts ``VO_LEAD_IN_S`` into its scene; if it is longer than its scene minus ``VO_TAIL_S`` it is sped up with
    atempo (capped at ``VO_MAX_TEMPO``) and otherwise allowed to spill slightly. A spilled line pushes the next
    one back so two lines never talk over each other.
    """
    n = len(scene_durs)
    placements: list[VoicePlacement] = []
    offset = 0.0
    prev_end = 0.0
    for i, d in enumerate(scene_durs):
        seg = d - (fade_s if i < n - 1 else 0.0)
        vd = vo_durs[i] if i < len(vo_durs) else None
        if vd and vd > 0:
            room = max(0.5, seg - VO_TAIL_S)
            tempo = min(VO_MAX_TEMPO, vd / room) if vd > room else 1.0
            start = max(offset + VO_LEAD_IN_S, prev_end + VO_GAP_S if placements else 0.0)
            end = start + vd / tempo
            placements.append(VoicePlacement(i, round(start, 3), round(end, 3), round(tempo, 4)))
            prev_end = end
        offset += seg
    return placements


def _vtt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def write_vtt(path: Path, cues: list[tuple[float, float, str]]) -> Path:
    """Atomically write a WebVTT captions file with one cue per ``(start_s, end_s, text)``."""
    lines = ["WEBVTT", ""]
    for i, (start, end, text) in enumerate(cues, start=1):
        text = " ".join(str(text).split())
        if not text:
            continue
        lines += [str(i), f"{_vtt_time(start)} --> {_vtt_time(end)}", text, ""]
    path = Path(path)
    return save_bytes(path.parent, path.name, "\n".join(lines).encode("utf-8"))


# --------------------------------------------------------------------------- stitch
def _keep_clip_audio() -> bool:
    return os.getenv("ADLOOP_KEEP_CLIP_AUDIO", "0").strip().lower() in {"1", "true", "yes", "on"}


_AFMT = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"


def _silence(seconds: float) -> str:
    return f"anullsrc=r=44100:cl=stereo,atrim=duration={seconds:.3f},{_AFMT}"


def _audio_graph(*, n: int, durs: list[float], fade: float, total: float, infos: list[MediaInfo],
                 keep_audio: bool, music_idx: int | None, vo_inputs: list[tuple[int, VoicePlacement]],
                 duck: str) -> tuple[list[str], str | None]:
    """Build the audio half of the filter graph; returns (filters, output label or None).

    Buses: narration, music and optional clip audio. The narration bus is a gapless ``concat`` of
    ``silence, line, silence, line, ..., silence`` (placements never overlap), each line loudness-normalised and
    sped up if needed. Timestamps are regenerated from the sample count after every resampling filter
    (``asetpts=N/SR/TB``): ``adelay``/``amix`` on loudnorm output mis-time streams in ffmpeg 7, so they are avoided.
    With narration present the music is ducked under it -- ``duck="sidechain"`` uses a sidechain compressor keyed
    on the narration bus, ``duck="volume"`` simply plays the music at 0.3 (fallback if the compressor fails).
    """
    fc: list[str] = []
    beds: list[str] = []
    if keep_audio:
        for i, (d, info) in enumerate(zip(durs, infos)):
            seg = d - (fade if i < n - 1 else 0.0)
            src = f"[{i}:a:0]{_AFMT},apad," if info.has_audio else "anullsrc=r=44100:cl=stereo,"
            fc.append(f"{src}atrim=duration={seg:.3f},asetpts=PTS-STARTPTS[ca{i}]")
        fc.append("".join(f"[ca{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1,volume=0.35[clipa]")
        beds.append("clipa")

    vo_bus = None
    if vo_inputs:
        parts: list[str] = []
        cursor = 0.0
        for j, (inp, pl) in enumerate(vo_inputs):
            start = min(pl.start, total)
            length = max(0.0, min(pl.end, total) - start)
            if length <= 0.05:
                continue
            if start - cursor > 0.001:
                fc.append(f"{_silence(start - cursor)}[vg{j}]")
                parts.append(f"[vg{j}]")
            tempo = f"atempo={pl.tempo:.4f}," if pl.tempo > 1.001 else ""
            fc.append(f"[{inp}:a:0]{_AFMT},loudnorm=I=-15:TP=-1.5:LRA=7,{_AFMT},asetpts=N/SR/TB,{tempo}"
                      f"apad=whole_dur={length:.3f},atrim=duration={length:.3f},asetpts=N/SR/TB[vl{j}]")
            parts.append(f"[vl{j}]")
            cursor = start + length
        if parts:
            if total - cursor > 0.001:
                fc.append(f"{_silence(total - cursor)}[vgend]")
                parts.append("[vgend]")
            fc.append("".join(parts) + f"concat=n={len(parts)}:v=0:a=1[vobus]")
            vo_bus = "vobus"

    if music_idx is not None:
        fade_out = min(1.2, max(0.1, total / 4))
        level = ",volume=0.3" if (vo_bus and duck == "volume") else ""
        fc.append(
            f"[{music_idx}:a:0]{_AFMT},loudnorm=I=-16:TP=-1.5:LRA=11,{_AFMT},asetpts=N/SR/TB,"
            f"apad=whole_dur={total:.3f},atrim=duration={total:.3f},"
            f"afade=t=out:st={max(0.0, total - fade_out):.3f}:d={fade_out:.3f}{level}[mus]"
        )
        if vo_bus and duck == "sidechain":
            fc.append("[vobus]asplit=2[vomix][vokey]")
            fc.append("[mus][vokey]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=500:makeup=1[musd]")
            beds.insert(0, "musd")
            vo_bus = "vomix"
        else:
            beds.insert(0, "mus")
    if vo_bus:
        beds.append(vo_bus)

    if not beds:
        return fc, None
    if len(beds) == 1:
        fc.append(f"[{beds[0]}]alimiter=limit=0.95[aout]")
    else:
        fc.append("".join(f"[{b}]" for b in beds) +
                  f"amix=inputs={len(beds)}:duration=first:normalize=0,alimiter=limit=0.95[aout]")
    return fc, "aout"


async def stitch(clips: list[Path], music: Path | None, out: Path, *, aspect: str, fade_s: float = 0.35,
                 voiceovers: list[tuple[Path | None, float]] | None = None,
                 captions: list[str] | None = None, captions_out: Path | None = None) -> float:
    """Concatenate ``clips`` into one H.264 MP4 at ``out`` with soundtrack + narration; return its duration.

    Video: every clip is scaled-to-fit + padded to the target frame (1280x720 or 720x1280), resampled to 30fps /
    SAR 1 / yuv420p, and padded-then-trimmed to exactly its probed duration so the crossfade offsets are exact
    even if a stream ends a few frames early. Consecutive clips are joined with an ``xfade`` of ``fade_s`` seconds
    (hard ``concat`` if ``fade_s`` <= 0 or any clip is too short to fade).

    Audio: the music (EBU R128 -16 LUFS, padded/trimmed to the cut, 1.2s fade-out) is ducked under the narration.
    ``voiceovers`` is aligned with ``clips``: one ``(wav_path | None, duration_s)`` per scene (a missing line is
    simply silent); lines are placed by :func:`place_voiceovers`. Clip audio is dropped unless
    ``ADLOOP_KEEP_CLIP_AUDIO=1`` (then mixed at 0.35). No music, narration or clip audio -> no audio stream.

    Captions: when ``captions_out`` is given, a WebVTT file with one cue per placed line (text from ``captions``,
    aligned with ``clips``) is written next to the cut, timed exactly like the audio.
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

    # Narration lines that actually exist on disk, with a probed duration when the caller did not know it.
    vos = list(voiceovers or [])[:n]
    vo_paths: list[Path | None] = []
    vo_durs: list[float | None] = []
    for item in vos:
        path, dur = (item if isinstance(item, (tuple, list)) else (item, None))
        path = Path(path) if path else None
        if path is None or not path.exists():
            vo_paths.append(None)
            vo_durs.append(None)
            continue
        vo_paths.append(path)
        vo_durs.append(float(dur) if dur else await probe_duration(path))
    placements = place_voiceovers(durs, fade, vo_durs)

    music_ok = music is not None and Path(music).exists()
    args: list[str] = []
    for c in clips:
        args += ["-i", str(c)]
    music_idx = None
    if music_ok:
        music_idx = n
        args += ["-i", str(music)]
    vo_inputs: list[tuple[int, VoicePlacement]] = []
    next_idx = n + (1 if music_ok else 0)
    for pl in placements:
        args += ["-i", str(vo_paths[pl.index])]
        vo_inputs.append((next_idx, pl))
        next_idx += 1

    vfc: list[str] = []
    for i, d in enumerate(durs):
        vfc.append(
            f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={FPS},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration=2,trim=duration={d:.3f},setpts=PTS-STARTPTS,"
            # setpts drops the link's frame-rate metadata, which xfade requires -> re-assert CFR + timebase.
            f"fps={FPS},settb=1/{FPS}[v{i}]"
        )
    if n == 1:
        vfc.append("[v0]null[vout]")
    elif fade > 0:
        prev = "v0"
        offset = 0.0
        for i in range(1, n):
            offset += durs[i - 1] - fade
            label = "vout" if i == n - 1 else f"x{i}"
            vfc.append(f"[{prev}][v{i}]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}[{label}]")
            prev = label
    else:
        vfc.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.stem}.{uuid.uuid4().hex[:8]}.tmp.mp4")

    async def encode(duck: str) -> None:
        afc, a_out = _audio_graph(n=n, durs=durs, fade=fade, total=total, infos=infos,
                                  keep_audio=_keep_clip_audio(), music_idx=music_idx, vo_inputs=vo_inputs,
                                  duck=duck)
        cmd = [*args, "-filter_complex", ";".join(vfc + afc), "-map", "[vout]"]
        if a_out:
            cmd += ["-map", f"[{a_out}]", "-c:a", "aac", "-b:a", "192k", "-ar", "44100"]
        else:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(FPS),
                "-t", f"{total:.3f}", "-movflags", "+faststart", str(tmp)]
        await run_ffmpeg(cmd)

    try:
        try:
            await encode("sidechain")
        except MediaError as exc:
            if not (vo_inputs and music_ok):
                raise
            log.warning("sidechain ducking failed (%s); retrying with fixed music level", exc)
            await encode("volume")
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    if captions_out is not None:
        texts = list(captions or [])
        cues = [(pl.start, min(pl.end, total), texts[pl.index] if pl.index < len(texts) else "")
                for pl in placements]
        await asyncio.to_thread(write_vtt, captions_out, cues)
    return await probe_duration(out)


async def mean_volume_db(path: Path, start: float, duration: float) -> float | None:
    """Mean loudness (dBFS, via ``volumedetect``) of ``path``'s audio in ``[start, start + duration]``.

    Used by tests / diagnostics to prove narration is audible in the final cut. None if undetectable.
    """
    _, err = await _exec(["-hide_banner", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                          "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"], timeout=60)
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", err)
    if not m or m.group(1) == "-inf":
        return None
    return float(m.group(1))
