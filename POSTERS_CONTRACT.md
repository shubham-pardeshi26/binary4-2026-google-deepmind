# AdLoop — Campaign Kit (posters) plugin contract

> Separate workstream from CONTRACT.md §9 (v2 agents: ignore this file). Built as a **self-contained plugin** in NEW files only, because
> other engineers are concurrently editing app/pipeline.py, app/main.py, app/events.py, app/genai_client.py, app/prompts.py, static/app.js,
> static/index.html, static/styles.css. You may READ those files (their core APIs are stable) but must NOT edit them. Integration into them
> is a tiny patch applied later by the integrator — you must write it down precisely (see §5).

User request (verbatim): "also i want some posters or images for marketing campaign".

## 1. What it does
When every scene of a run has a winning keyframe (storyboard done — Omni clips are still rendering, so this adds ~zero wall-clock), the
plugin generates a **Campaign Kit** with **Nano Banana 2 Lite** — print/social/web posters in several formats, each with rendered headline/
CTA typography, continuity-anchored to the hero keyframe + anchor (+ product photo), **2 variants per format in parallel**, judged by
Gemini 3.8 Flash (poster rubric) → winner per format. Localize: whenever a market's localization plan lands, the plugin NB2-edits every
winning poster into that market's language/script (headline/CTA translated, layout/product kept). Plus a one-click **campaign kit ZIP**.

## 2. Backend — NEW files `app/posters.py` (+ optional `app/poster_prompts.py`), test `scripts/e2e_posters.py`
- Uses the existing `run.gm` (GenMedia: `generate_image(prompt, refs=[...], aspect=..., size="1K")`, `generate_json(parts, schema, system=...)`;
  mock mode works automatically) — never the SDK directly. Uses Run helpers: `run.emit`, `run.save`, `run.save_asset`, `run.read_url`,
  `run.spawn(coro, stage=...)` (check its exact signature), `run.log`, `run.anchor_bytes()`, `run.product_image`, `run.plan`, `run.state`,
  `Run.winner_variant(sc)`; `RunManager.get(run_id)`; `app.state.manager` in routes.
- `class PosterStudio: __init__(self, manager)`, `on_event(self, run, event: dict) -> None` (sync, never raises, never blocks: spawns tasks)
  reacting to: `winner` (when ALL scenes have a winner and posters not yet started → start kit), `localize_plan` (→ localized posters for that
  market, after the kit is done — wait for it), optionally `direction` (ignore for now).
- Formats (constant, ordered): `ig_square` "Instagram post" 1:1 · `ig_story` "Story / Reel cover" 9:16 · `print_poster` "Print poster" 4:5 ·
  `web_banner` "Web hero banner" 16:9 · `billboard` "Billboard" 21:9 (if an aspect is rejected by the model, fall back to the nearest supported one
  and record it).
- Copy: one quick Flash call `poster_copy(plan)` → `{"headline": "≤6 words", "subline": "≤12 words", "cta": "≤4 words", "art_direction": {"<format>": "layout line"}}`
  (mock: derived from plan.tagline/cta). Poster prompt per format: exact strings in double quotes for NB2 to render, typographic hierarchy,
  safe margins for the format, brand palette hexes + typography from the plan, product/hero identical to refs, no extra text, no watermarks.
- Refs order: [hero keyframe = winner of the `reveal` scene (else highest-energy scene), anchor, product photo if any].
- Judge: own `generate_json` call per format with both variants (labeled Variant 0/1) → `{"scores": [{"index", "legibility", "brand", "composition",
  "impact", "overall", "notes"}], "winner_index", "rationale"}`; legibility = exact spelling of the quoted copy (garbled text → heavy penalty).
- State `run.state["posters"]` (create if missing; persist with run.save()):
  `{"status": "idle|rendering|done|error", "copy": {...}|null, "items": [{"format", "label", "aspect", "status", "variants": [{"idx", "url", "latency_ms", "api_path"}],
  "winner": 0|null, "score": 0.0|null, "rationale": "", "error": null}], "started_ms": 0, "done_ms": 0}`; localized:
  `run.state["localizations"][market]["posters"] = [{"format", "url", "latency_ms"}]` (create the market dict if missing; don't clobber other keys).
- Events: `poster_status` {status: start|done|error, format?, error?} · `posters_copy` {copy} · `poster_variant` {format, idx, url, latency_ms, api_path} ·
  `poster` {format, label, aspect, winner, url, score, rationale, by: judge|user} · `localize_poster` {market, format, url, latency_ms}.
  Asset names: `poster_<format>_<idx>.png`, `poster_<market-slug>_<format>.png` (use run.save_asset with the right extension from the mime).
- `router = APIRouter()` with:
  - `POST /api/runs/{run_id}/posters` body `{"formats": [str]?, "instruction": str?}` → regenerate all/selected formats (instruction appended to prompts) → `{"ok": true}`
  - `POST /api/runs/{run_id}/posters/{format}/select` body `{"idx": int}` → set winner (by=user) → `{"ok": true}`
  - `GET /api/runs/{run_id}/kit.zip` → streaming/attachment ZIP (build in a thread): current final mp4 + captions .vtt, current music, voiceovers,
    winning keyframes, winning posters, localized posters/animatics/VO, `plan.json`, `README.txt` (campaign name, tagline, file list). Missing files skipped.
  Validate run/format ids; JSON errors `{"error": msg}`.
- Every exception → `poster_status` error + `log` event; never crash the run.

## 3. Frontend — NEW files `static/posters.js` (ES module) + `static/posters.css`
- Self-mounting "Campaign Kit" section: render into `#posters-mount` if present, else insert right after the final-cut section (read
  static/index.html to find a stable anchor; fallback: append to `<main>`).
- Reads the run id from `location.hash` (`#run=<id>`, listen to `hashchange`), loads `GET /api/runs/{id}` (`state.posters`,
  `state.localizations[*].posters`), then opens its OWN `EventSource('/api/runs/{id}/events')` handling the §2 events idempotently
  (dedupe by format+idx / market+format). Close/reopen on run change.
- UI: section header "Campaign Kit — Nano Banana 2 Lite" + live counter ("10 posters · 3.1 s") + "⬇ Download kit (.zip)" (→ kit.zip); copy block
  (headline/subline/CTA); a responsive row/masonry of format cards with correct aspect ratios (shimmer while rendering, pop-in with latency badge,
  winner with score badge + rationale tooltip, variant thumbnails to swap → POST select, ↻ regenerate with optional instruction → POST posters,
  click → lightbox with full-size + PNG download); a "Localized posters" grid market × format from `localize_poster`.
- Style: reuse the CSS variables/fonts defined in static/styles.css (read it: e.g. --grad, --panel, --border, font vars) so it looks native; all
  model text escaped (XSS-safe).
- Expose `window.AdLoopPosters = { getState(), onChange(cb), renderSlide(el) }` — `renderSlide` draws a screen-share-ready "Campaign kit" poster
  wall into a presentation slide element (used later by app.js presentation mode).

## 4. Tests
`scripts/e2e_posters.py --inprocess --data-dir /tmp/<dir> --mock-speed <x>`: in-process ASGI (see scripts/e2e_mock.py for the pattern). Because
the integration patch isn't applied yet, the test itself must wire the plugin into `app.main.app` at runtime (include the router; register
`studio.on_event` by wrapping `Run.emit` IN THE TEST ONLY) — and must also work unchanged once the real integration (§5) exists (detect and skip
self-wiring). Assert: posters_copy, 2 poster_variant per format, poster per format, state shape, select → poster by=user, regenerate one format,
localize 1 market → localize_poster per format, kit.zip downloads and contains the expected files. Frontend: `node --check static/posters.js`.

## 5. Integration patch (write it EXACTLY in your report as unified diffs or precise insert instructions)
- app/pipeline.py: an observer hook — `RunManager.observers: list[Callable[[Run, dict], None]]`; `Run.emit` calls each observer with the emitted
  event inside try/except (logged, never raises).
- app/main.py: import `PosterStudio`, `router`; in lifespan after the manager is created: `studio = PosterStudio(manager); manager.observers.append(studio.on_event); app.state.posters = studio`; `app.include_router(posters_router)`.
- static/index.html: `<link rel="stylesheet" href="/static/posters.css">`, `<section id="posters-mount"></section>` after the final cut section, `<script type="module" src="/static/posters.js"></script>`.
- static/app.js presentation mode: insert a "Campaign kit" slide before the stats slide that calls `window.AdLoopPosters?.renderSlide(slideEl)`.
