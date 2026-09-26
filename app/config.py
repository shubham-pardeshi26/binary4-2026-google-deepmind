"""Runtime configuration for AdMate.

All knobs are read once from the process environment (optionally populated from
the gitignored ``.env`` file at the project root via python-dotenv) into a single
:data:`settings` instance that every other module imports.

Design notes
------------
* **Mock mode** is on when ``ADMATE_MOCK=1`` *or* when no API key is present, so
  the whole app runs offline with synthetic assets out of the box.
* Model ids are env-overridable because the GenMedia model ids are new; the
  per-role *API path* can also be pinned with ``ADMATE_<ROLE>_PATH`` (see
  :attr:`Settings.path_overrides`), e.g. ``ADMATE_IMAGE_PATH=interactions``.
* Parsing is defensive: a malformed numeric env var falls back to its default
  instead of crashing the server at import time.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

#: Project root (the directory that contains ``app/``).
ROOT: Path = Path(__file__).resolve().parent.parent

# Load .env without overriding variables already exported by the shell/container.
load_dotenv(ROOT / ".env", override=False)

#: Roles whose API path can be pinned via ``ADMATE_<ROLE>_PATH``.
PATH_ROLES = ("image", "video", "music", "transcribe", "text", "tts")


def _env_str(name: str, default: str) -> str:
    """Return a stripped env var, or ``default`` when unset/blank."""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    """Parse an int env var, clamped to ``[lo, hi]``; invalid values use ``default``."""
    try:
        value = int(float(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _env_float(name: str, default: float, *, lo: float | None = None) -> float:
    """Parse a float env var (optionally lower-bounded); invalid values use ``default``."""
    try:
        value = float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        value = default
    if lo is not None:
        value = max(lo, value)
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    """Interpret ``1/true/yes/on`` (case-insensitive) as True."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Process-wide settings, instantiated once as :data:`settings`.

    Attribute names are part of the build contract (CONTRACT.md §2); extra
    attributes (``music_concurrency``, ``mock_speed``, ``path_overrides``,
    ``request_timeout_seconds``) are additive; ``model_tts`` / ``tts_concurrency``
    come from the v2 voiceover addendum (§9a).
    """

    def __init__(self) -> None:
        # --- credentials & mode -------------------------------------------------
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        self.api_key: str | None = key.strip() or None
        self.mock: bool = _env_bool("ADMATE_MOCK") or self.api_key is None

        # --- models ---------------------------------------------------------------
        self.model_text: str = _env_str("ADMATE_MODEL_TEXT", "gemini-3.8-flash")
        self.model_image: str = _env_str("ADMATE_MODEL_IMAGE", "gemini-3.1-flash-lite-image")
        self.model_video: str = _env_str("ADMATE_MODEL_VIDEO", "gemini-omni-1.1-flash")
        self.model_music: str = _env_str("ADMATE_MODEL_MUSIC", "lyria-3.5")
        self.model_transcribe: str = _env_str("ADMATE_MODEL_TRANSCRIBE", "gemini-3.5-transcribe")
        self.model_tts: str = _env_str("ADMATE_MODEL_TTS", "gemini-3.8-flash-tts")

        # --- concurrency (per-modality semaphores inside GenMedia) -----------------
        self.image_concurrency: int = _env_int("ADMATE_IMAGE_CONCURRENCY", 8, lo=1)
        self.video_concurrency: int = _env_int("ADMATE_VIDEO_CONCURRENCY", 4, lo=1)
        self.text_concurrency: int = _env_int("ADMATE_TEXT_CONCURRENCY", 6, lo=1)
        self.music_concurrency: int = _env_int("ADMATE_MUSIC_CONCURRENCY", 3, lo=1)
        self.tts_concurrency: int = _env_int("ADMATE_TTS_CONCURRENCY", 6, lo=1)

        # --- video ---------------------------------------------------------------
        self.video_resolution: str = _env_str("ADMATE_VIDEO_RESOLUTION", "720p")
        self.video_seconds: int = _env_int("ADMATE_VIDEO_SECONDS", 6, lo=2, hi=20)
        self.video_poll_seconds: float = _env_float("ADMATE_VIDEO_POLL_SECONDS", 3.0, lo=0.2)
        self.video_timeout_seconds: int = _env_int("ADMATE_VIDEO_TIMEOUT_SECONDS", 420, lo=30)

        # --- request hygiene -------------------------------------------------------
        #: Upper bound for a single non-video request (image/text/music/transcribe).
        self.request_timeout_seconds: float = _env_float("ADMATE_REQUEST_TIMEOUT_SECONDS", 150.0, lo=5.0)

        # --- judge / repair loop ------------------------------------------------------
        self.judge_threshold: float = _env_float("ADMATE_JUDGE_THRESHOLD", 7.0)
        self.max_repair_rounds: int = _env_int("ADMATE_MAX_REPAIR_ROUNDS", 1, lo=0)

        # --- abuse protection -------------------------------------------------------
        self.max_concurrent_runs: int = _env_int("ADMATE_MAX_CONCURRENT_RUNS", 3, lo=1)
        self.runs_per_ip_per_hour: int = _env_int("ADMATE_RUNS_PER_IP_PER_HOUR", 6, lo=0)

        # --- mock mode ----------------------------------------------------------------
        #: Multiplier on mock latencies (0.1 = ten times faster; handy for tests).
        self.mock_speed: float = _env_float("ADMATE_MOCK_SPEED", 1.0, lo=0.0)

        # --- API path pinning: ADMATE_IMAGE_PATH=interactions|generate_content, ... ---
        self.path_overrides: dict[str, str] = {}
        for role in PATH_ROLES:
            pinned = os.getenv(f"ADMATE_{role.upper()}_PATH", "").strip().lower()
            if pinned:
                self.path_overrides[role] = pinned

        # --- filesystem ------------------------------------------------------------------
        self.data_dir: Path = Path(os.getenv("ADMATE_DATA_DIR") or (ROOT / "data" / "runs"))
        self.static_dir: Path = ROOT / "static"

    @property
    def mode(self) -> str:
        """``"mock"`` or ``"live"`` — the value surfaced in run state and /api/health."""
        return "mock" if self.mock else "live"

    def models(self) -> dict[str, str]:
        """Role -> model id mapping (used by /api/health and telemetry)."""
        return {
            "text": self.model_text,
            "image": self.model_image,
            "video": self.model_video,
            "music": self.model_music,
            "transcribe": self.model_transcribe,
            "tts": self.model_tts,
        }

    def __repr__(self) -> str:  # never leak the key into logs
        return (
            f"Settings(mode={self.mode!r}, models={self.models()!r}, "
            f"api_key={'set' if self.api_key else 'unset'})"
        )


#: The process-wide settings singleton.
settings = Settings()
