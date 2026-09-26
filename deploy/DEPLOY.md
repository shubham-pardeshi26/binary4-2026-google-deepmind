# Deploying AdMate

AdMate ships as a single Docker image (repo-root `Dockerfile`): FastAPI + uvicorn, system ffmpeg, non-root
UID 1000, listening on `$PORT` (default **7860**). The same image runs on all three targets below.

**Secrets rule:** the Gemini key is only ever a platform secret (`GEMINI_API_KEY`). It is never in the image,
the repo, or a committed file. `.dockerignore` excludes `.env`.

Before any deploy, check locally:

```bash
.venv/bin/python scripts/smoke_test.py            # live probe of every model (writes data/smoke/<ts>/)
.venv/bin/python scripts/bench_nb2.py -n 32 -c 16  # NB2 numbers for the README
docker build -t admate . && docker run --rm -p 7860:7860 -e GEMINI_API_KEY="$GEMINI_API_KEY" admate
# open http://localhost:7860  ->  /api/health should report "mode": "live"
```

---

## (a) Hugging Face Spaces (Docker SDK) — primary demo target

HF Spaces needs a `README.md` with Docker frontmatter at the Space root; ours lives in
`deploy/hf_space_README.md` (`sdk: docker`, `app_port: 7860`) and is swapped in at push time.

### Option 1 — one command (huggingface_hub)

```bash
pip install huggingface_hub
huggingface-cli login                                  # or: export HF_TOKEN=hf_...
GEMINI_API_KEY=... python deploy/push_hf_space.py <user-or-org>/admate
```

The script creates the Space (Docker SDK) if needed, stores `GEMINI_API_KEY` as a **Space secret**, uploads the
repo excluding `.env`, `.venv/`, `data/` and caches, and uploads `deploy/hf_space_README.md` as `README.md`.
It also forwards `ADMATE_MAX_CONCURRENT_RUNS`, `ADMATE_RUNS_PER_IP_PER_HOUR`, `ADMATE_SHOWCASE_RUN` and
`ADMATE_MOCK` as Space variables if they are set in your shell.

### Option 2 — git

```bash
# 1. Create the Space: https://huggingface.co/new-space  ->  SDK "Docker"  ->  Blank
# 2. Settings -> Variables and secrets -> New secret:  GEMINI_API_KEY = <key>
#    (optional variables: ADMATE_RUNS_PER_IP_PER_HOUR=4, ADMATE_MAX_CONCURRENT_RUNS=2)
git clone https://huggingface.co/spaces/<user>/admate hf-admate
rsync -a --exclude .env --exclude .venv --exclude data --exclude __pycache__ --exclude .git ./ hf-admate/
cp deploy/hf_space_README.md hf-admate/README.md
cd hf-admate && git add -A && git commit -m "Deploy AdMate" && git push
```

Build logs: `https://huggingface.co/spaces/<user>/admate?logs=build`.

Notes
- Free CPU Spaces have 2 vCPU / 16 GB — plenty; all heavy work happens in the Gemini API.
- Space storage is **ephemeral**: runs vanish on restart. Enable *Persistent storage* (mounted at `/data`) and set
  `ADMATE_DATA_DIR=/data/runs` to keep runs, or re-record the showcase after each rebuild.
- Spaces sleep after inactivity; wake it a few minutes before judging.

---

## (b) Google Cloud Run

```bash
PROJECT=<gcp-project>; REGION=asia-south1          # Mumbai, closest to Hyderabad
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com

# Store the key in Secret Manager (preferred over a plain env var).
printf %s "$GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor

gcloud run deploy admate \
  --source . \
  --region $REGION \
  --allow-unauthenticated \
  --timeout 3600 \
  --session-affinity \
  --max-instances 1 \
  --memory 2Gi --cpu 2 \
  --no-cpu-throttling \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest \
  --set-env-vars ADMATE_MAX_CONCURRENT_RUNS=2,ADMATE_RUNS_PER_IP_PER_HOUR=4
```

Quick alternative without Secret Manager: replace `--set-secrets ...` with
`--set-env-vars GEMINI_API_KEY=$GEMINI_API_KEY,ADMATE_MAX_CONCURRENT_RUNS=2` (the key is then visible to anyone
with Run viewer access on the project).

Why these flags
- `--timeout 3600`: the SSE event stream is a single long-lived request.
- `--session-affinity` + `--max-instances 1`: run state and assets live in the instance's memory/disk, so every
  request for a run must hit the same instance.
- `--no-cpu-throttling`: the pipeline keeps working in background tasks after `POST /api/runs` returns;
  without it Cloud Run throttles CPU between requests and ffmpeg stitches crawl.
- `--memory 2Gi`: Cloud Run's filesystem is in-memory, so run folders count against RAM.
- Cloud Run injects `PORT` (8080); the image's `CMD` honours it.

---

## (c) Render

`deploy/render.yaml` is a Blueprint for a Docker web service (health check `/api/health`, `GEMINI_API_KEY`
marked `sync: false` so Render prompts for it and never stores it in git).

1. Push the repo to GitHub.
2. Render dashboard -> **New -> Blueprint** -> pick the repo -> set **Blueprint Path** to `deploy/render.yaml`
   (or copy the file to the repo root, where Render looks by default).
3. Enter `GEMINI_API_KEY` when prompted -> **Apply**.

Render injects `PORT` (10000); the image honours it. The `standard` plan (2 GB) is recommended; uncomment the
`disk:` block (paid plans) to persist runs at `/app/data`.

---

## Demo hardening checklist

| Risk | Mitigation |
|---|---|
| Quota burn from strangers | `ADMATE_RUNS_PER_IP_PER_HOUR` (default 6, `0` = unlimited) and `ADMATE_MAX_CONCURRENT_RUNS` (default 3); excess requests get HTTP 429 `{"error": ...}`. The image runs uvicorn with `--proxy-headers` so limits see the real client IP behind the platform load balancer. |
| Preview model ids change / fail on the day | Every model id is an env var (`ADMATE_MODEL_*`); adapters try the Interactions API first and fall back to `generate_content` / `generate_videos`, remembering the working path. Run `scripts/smoke_test.py` after any change, then pin known-good paths with `ADMATE_<ROLE>_PATH` (e.g. `ADMATE_IMAGE_PATH=interactions`) to skip probing. |
| Key revoked, quota exhausted, network down | Set `ADMATE_MOCK=1` (or remove the key): the full UI and pipeline keep working with synthetic assets. |
| Live generation too slow on stage | "Watch sample run" replays a recorded live run from its `events.jsonl` (`/api/runs/{id}/events?replay=1&speed=4`). Pin it with `ADMATE_SHOWCASE_RUN=<run_id>`; otherwise the newest run with a final cut is used. Copy that run folder into persistent storage so it survives rebuilds. |
| Rate limits / 429s under fan-out | Per-modality semaphores (`ADMATE_IMAGE_CONCURRENCY`, `ADMATE_VIDEO_CONCURRENCY`, `ADMATE_TEXT_CONCURRENCY`, `ADMATE_TTS_CONCURRENCY`) plus exponential backoff (0.8 s / 1.6 s / 3.2 s). Lower image concurrency first if you see 429s. |
| Long renders stall a run | `ADMATE_VIDEO_TIMEOUT_SECONDS` (default 420) bounds every Omni render; a failed render falls back to a Ken Burns move over the winning keyframe, so the final cut still completes. |
| Voiceover (TTS) unavailable | A failed line is left out of the mix and never blocks the cut; re-voice it later from the clip card. Pin the working path with `ADMATE_TTS_PATH` or swap the model with `ADMATE_MODEL_TTS`. |
