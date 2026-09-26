"""AdLoop — a one-loop GenMedia ad studio.

Brief -> creative director plan -> Nano Banana storyboard fan-out -> vision-judge
tournament with self-repair -> Omni image-to-video + conversational editing ->
Lyria adaptive soundtrack + Flash TTS scene-by-scene voiceover -> ffmpeg final cut
-> localization.

Package layout (see CONTRACT.md for the full interface spec):

* ``app.config``       – environment-driven :class:`~app.config.Settings` singleton.
* ``app.genai_client`` – :class:`~app.genai_client.GenMedia`, the ONLY module that
  talks to Google (primary/fallback API paths, retries, semaphores, telemetry,
  and a fully offline mock mode).
* ``app.prompts``      – creative-direction prompts, JSON schemas and the
  high-level model calls (plan + narration, judge, direct, localize).
* ``app.mock``         – deterministic fake JSON payloads and synthetic media
  (keyframes, music, speech) used by mock mode.
* ``app.media`` / ``app.pipeline`` / ``app.events`` / ``app.main`` – ffmpeg,
  orchestration, event bus and the FastAPI server.
"""

__version__ = "1.0.0"
