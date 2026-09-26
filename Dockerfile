# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------------
# AdMate — single-container image for Hugging Face Spaces (Docker SDK),
# Google Cloud Run and Render.
#
#   * python:3.11-slim base, system ffmpeg from apt (imageio-ffmpeg stays
#     installed as a fallback binary if the apt one is ever missing).
#   * Runs as a non-root user with UID 1000 (required by HF Spaces).
#   * Listens on $PORT (Cloud Run / Render inject it), default 7860 (HF Spaces).
#   * The API key is NEVER baked in: pass GEMINI_API_KEY as a platform secret.
#     Without a key the app starts in mock mode, so the container always boots.
# -----------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=7860

# ffmpeg: stitching, clip normalisation, Ken Burns mock clips.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (HF Spaces runs containers as UID 1000).
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin admate

WORKDIR /app

# Dependencies first so code edits don't invalidate the pip layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code.
COPY --chown=admate:admate app/ ./app/
COPY --chown=admate:admate static/ ./static/
COPY --chown=admate:admate scripts/ ./scripts/

# Writable runtime data (run folders, event logs, stitched cuts).
RUN mkdir -p /app/data/runs && chown -R admate:admate /app/data

USER admate

EXPOSE 7860

# Liveness: /api/health is cheap and never calls a model.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','7860'), timeout=4)" || exit 1

# Shell form so ${PORT} expands; `exec` makes uvicorn PID 1 for clean SIGTERM.
# --proxy-headers + forwarded-allow-ips: per-IP rate limits see the real client
# IP behind the HF / Cloud Run / Render load balancers.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 75"]
