/**
 * AdMate · Campaign Kit plugin (POSTERS_CONTRACT §3).
 *
 * A self-contained ES module that renders the "Campaign Kit — Nano Banana 2 Lite" section: print / social / web
 * posters in five formats (2 variants each, judged by Gemini Flash), a copy block, per-format regenerate and
 * variant swap, a full-size lightbox with PNG download, a market × format grid of localized posters, and a
 * one-click campaign-kit ZIP.
 *
 * It deliberately shares nothing with static/app.js at runtime:
 *  - the run id comes from `location.hash` (`#run=<id>`). app.js updates the hash with `history.replaceState`,
 *    which does not fire `hashchange`, so the hash is also polled (cheap string compare);
 *  - it loads `GET /api/runs/{id}` (`posters`, `localizations[*].posters`) for an instant first paint, then opens
 *    its OWN `EventSource('/api/runs/{id}/events')`. The server replays the full history on every (re)connect, so
 *    events are deduped by `seq` and every handler is an idempotent upsert keyed by format+idx / market+format;
 *  - it mounts into `#posters-mount` if present, else right after the final-cut section, else at the end of <main>.
 *
 * All model-provided text is inserted with text nodes (XSS-safe); every URL passes `safeUrl`.
 *
 * Public API (window.AdMatePosters):
 *  - getState()            -> plain snapshot {runId, status, copy, items[], localized{}, count, elapsed_ms, error}
 *  - onChange(cb)          -> subscribe to snapshots after each render; returns an unsubscribe function
 *  - renderSlide(el)       -> draw a screen-share-ready "Campaign kit" poster wall into a presentation slide
 *                             element; the slide keeps updating live while it stays in the document
 *  - attach(runId, opts?)  -> optional: follow a run explicitly; `{replay: true, speed}` mirrors app.js's paced
 *                             sample-run replay so posters appear in step with the rest of the studio
 *  - refresh()             -> re-load the snapshot for the current run
 */

// ─────────────────────────────────────────────────────────── constants

/** Poster formats in display order (mirrors the backend constant; unknown formats from the server are appended). */
const FORMATS = [
  { format: 'ig_square', label: 'Instagram post', aspect: '1:1' },
  { format: 'ig_story', label: 'Story / Reel cover', aspect: '9:16' },
  { format: 'print_poster', label: 'Print poster', aspect: '4:5' },
  { format: 'web_banner', label: 'Web hero banner', aspect: '16:9' },
  { format: 'billboard', label: 'Billboard', aspect: '21:9' },
];

/** Variants rendered per format (POSTERS_CONTRACT §1). */
const VARIANTS_PER_FORMAT = 2;

/** Formats wider than this (w/h) share the "wide" row; the rest share the "tall" row. */
const WIDE_MIN_RATIO = 1.3;

/** Same pattern as app.js `hashRunId()`. */
const HASH_RE = /(?:^#|&)run=([a-z0-9]+)/i;

/** Event types this plugin consumes; everything else only advances the seq/clock bookkeeping. */
const EVENT_TYPES = new Set(['poster_status', 'posters_copy', 'poster_variant', 'poster', 'localize_poster', 'localize_plan', 'localize_status']);

const HASH_POLL_MS = 400;
const CLOCK_TICK_MS = 250;

// ─────────────────────────────────────────────────────────── tiny DOM + format helpers

/**
 * Hyperscript builder. Strings become text nodes (XSS-safe); `on*` functions become listeners; `dataset` and
 * `style` objects are merged (`--vars` via setProperty); `true` booleans become empty attributes.
 */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'style' && typeof v === 'object') {
        for (const [sk, sv] of Object.entries(v)) {
          if (sv == null) continue;
          if (sk.startsWith('--')) el.style.setProperty(sk, String(sv));
          else el.style[sk] = sv;
        }
      } else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  addKids(el, kids);
  return el;
}

function addKids(el, kids) {
  for (const c of kids) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) addKids(el, c);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** Set textContent only when it changed. */
function setText(el, text) {
  const t = String(text ?? '');
  if (el.textContent !== t) el.textContent = t;
}

/** Allow same-origin paths, http(s), blob: and data:image URLs only. */
/** True when the "Behind the scenes" (technical) view is on. */
function isPro() {
  return document.body.classList.contains('pro');
}

function safeUrl(u) {
  if (typeof u !== 'string' || !u) return '';
  if (u.startsWith('/') && !u.startsWith('//')) return u;
  if (/^https?:\/\//i.test(u) || u.startsWith('blob:') || /^data:image\//i.test(u)) return u;
  return '';
}

/** Append a cache-busting version (regenerated posters overwrite the same asset name). */
function versioned(url, v) {
  const u = safeUrl(url);
  if (!u || v == null || u.startsWith('data:') || u.startsWith('blob:')) return u;
  return `${u}${u.includes('?') ? '&' : '?'}v=${encodeURIComponent(String(v))}`;
}

function fmtMs(ms) {
  if (ms == null || !Number.isFinite(Number(ms))) return '';
  const v = Number(ms);
  if (v < 1000) return `${Math.round(v)} ms`;
  if (v < 60000) return `${(v / 1000).toFixed(1)} s`;
  return `${Math.floor(v / 60000)}m ${String(Math.floor((v % 60000) / 1000)).padStart(2, '0')}s`;
}

function fmtScore(v) {
  return v != null && Number.isFinite(Number(v)) ? Number(v).toFixed(1) : '';
}

/** "16:9" -> {w: 16, h: 9, r: 1.777…}; anything unparsable -> 1:1. */
function parseAspect(a) {
  const m = /^\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*$/i.exec(String(a || ''));
  const w = m ? Number(m[1]) : 1;
  const hh = m ? Number(m[2]) : 1;
  return w > 0 && hh > 0 ? { w, h: hh, r: w / hh } : { w: 1, h: 1, r: 1 };
}

function slug(s) {
  return String(s || '').toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'market';
}

/** File extension of an asset URL (png default) for download names. */
function extOf(url) {
  const m = /\.(png|jpe?g|webp)(?:[?#]|$)/i.exec(String(url || ''));
  return m ? m[1].toLowerCase().replace('jpeg', 'jpg') : 'png';
}

function runIdFromHash() {
  const m = HASH_RE.exec(location.hash);
  return m ? m[1] : null;
}

function runPath(runId, suffix = '') {
  return `/api/runs/${encodeURIComponent(runId)}${suffix}`;
}

/** JSON fetch (GET, or POST when `json` is given); throws Error(msg) with `.status` on failure. */
async function api(path, json) {
  const opts = json === undefined ? { method: 'GET' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(json) };
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw Object.assign(new Error('Network error — is the AdMate server running?'), { status: 0 });
  }
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  // A run snapshot may legitimately carry an `error` field, so a 2xx body only counts as an error for actions.
  if (!res.ok || (json !== undefined && body && typeof body === 'object' && body.error && body.ok !== true)) {
    const detail = body?.error || (typeof body?.detail === 'string' ? body.detail : '') || `HTTP ${res.status}`;
    throw Object.assign(new Error(String(detail)), { status: res.status });
  }
  return body;
}

/** Toast in the studio's #toasts host (styled by styles.css `.toast`); silently skipped if absent. */
function toast(msg, kind = 'info') {
  const host = document.getElementById('toasts');
  if (!host) return;
  const el = h('div', { class: `toast ${kind}`, role: kind === 'error' ? 'alert' : 'status' }, h('span', { class: 'toast-dot' }), h('span', { class: 'toast-msg' }, String(msg).slice(0, 280)));
  const dismiss = () => {
    el.classList.remove('in');
    el.classList.add('out');
    setTimeout(() => el.remove(), 320);
  };
  el.addEventListener('click', dismiss);
  host.appendChild(el);
  while (host.children.length > 5) host.firstChild.remove();
  requestAnimationFrame(() => el.classList.add('in'));
  setTimeout(dismiss, kind === 'error' ? 6500 : 4000);
}

// ─────────────────────────────────────────────────────────── state

/**
 * One format card's model. The server keeps every variant ever rendered (a regenerate appends new `idx` values),
 * so a regeneration marks the item `stale` with `staleFrom` = the highest idx before it started: until a newer
 * variant lands, the previous winner stays on screen (dimmed, with a shimmer).
 */
function newItem(def) {
  return {
    format: def.format,
    label: def.label,
    aspect: def.aspect,
    defaultAspect: def.aspect,
    status: 'idle',
    variants: new Map(), // idx -> {idx, url, latency_ms, api_path, v}
    winner: null,
    score: null,
    rationale: '',
    by: null,
    error: null,
    stale: false,
    staleFrom: null,
  };
}

function freshState(runId) {
  return {
    runId,
    status: 'idle', // idle | rendering | done | error
    error: null,
    copy: null,
    items: new Map(FORMATS.map((f) => [f.format, newItem(f)])),
    localized: new Map(), // market -> Map(format -> {format, url, latency_ms, v})
    pendingMarkets: new Set(), // localize_plan seen, posters not yet in
    failedMarkets: new Set(),
    startedT: null, // run-relative ms of the kit start event
    doneT: null,
    partial: false, // a subset of formats is regenerating after the kit was done (counter keeps the kit time)
    startedMs: 0, // from the snapshot (fallback when no events carry t)
    doneMs: 0,
    lastT: null,
    lastAt: 0,
    busy: new Set(), // formats with a local request in flight
    kitBusy: false,
  };
}

let S = freshState(null);

const stream = { es: null, gen: 0, lastSeq: 0, applied: 0, replay: false };
const listeners = new Set();
const slides = new Set();

function item(format, def) {
  let it = S.items.get(format);
  if (!it) {
    it = newItem({ format, label: def?.label || format.replace(/_/g, ' '), aspect: def?.aspect || '1:1' });
    S.items.set(format, it);
  }
  return it;
}

function winnerVariant(it) {
  if (it.winner != null && it.variants.has(it.winner)) return it.variants.get(it.winner);
  return null;
}

function sortedVariants(it) {
  return [...it.variants.values()].filter((v) => v.url).sort((a, b) => a.idx - b.idx);
}

/** Variants of the render in flight (all of them unless a regeneration is running). */
function freshVariants(it) {
  const all = sortedVariants(it);
  return it.stale && it.staleFrom != null ? all.filter((v) => v.idx > it.staleFrom) : all;
}

/** True while a regeneration runs and none of its variants has landed yet (show the old poster dimmed). */
function isDimmed(it) {
  return it.stale && freshVariants(it).length === 0;
}

/** The variant a card shows: a fresh variant of a running regeneration, else the winner, else the first one. */
function shownVariant(it) {
  if (it.stale) {
    const fresh = freshVariants(it);
    if (fresh.length) return fresh[0];
  }
  return winnerVariant(it) || sortedVariants(it)[0] || null;
}

/** Display status: adds the derived "judging" phase (all variants of this render in, no verdict yet). */
function displayStatus(it) {
  if (it.error && it.status === 'error') return 'error';
  if (it.status === 'rendering') {
    const verdictPending = it.stale || it.winner == null;
    if (verdictPending && freshVariants(it).length >= VARIANTS_PER_FORMAT) return 'judging';
    return 'rendering';
  }
  if (it.status === 'done' || it.winner != null) return 'done';
  return it.status || 'idle';
}

function posterCount() {
  let n = 0;
  for (const it of S.items.values()) for (const v of it.variants.values()) if (v.url) n++;
  for (const m of S.localized.values()) for (const p of m.values()) if (p.url) n++;
  return n;
}

function elapsedMs() {
  if (S.startedT != null) {
    if (S.doneT != null && S.doneT >= S.startedT && (S.status !== 'rendering' || S.partial)) return S.doneT - S.startedT;
    if (S.status === 'rendering' && S.lastT != null) {
      const drift = stream.replay ? 0 : performance.now() - S.lastAt;
      return Math.max(0, S.lastT - S.startedT + drift);
    }
    return S.lastT != null ? Math.max(0, S.lastT - S.startedT) : null;
  }
  if (S.startedMs > 0 && S.doneMs >= S.startedMs) return S.doneMs - S.startedMs;
  return null;
}

/** Mark items as (re-)rendering; their old images stay visible (dimmed) until the first new variant lands. */
function beginRender(formats) {
  for (const it of S.items.values()) {
    if (formats && !formats.includes(it.format)) continue;
    it.status = 'rendering';
    it.error = null;
    if (!it.stale) {
      const all = sortedVariants(it);
      it.stale = all.length > 0;
      it.staleFrom = all.length ? all[all.length - 1].idx : null;
    }
  }
}

function endRender(it) {
  it.stale = false;
  it.staleFrom = null;
}

/** Load `run.state.posters` + `localizations[*].posters` from a GET /api/runs/{id} snapshot. */
function loadSnapshot(snap) {
  const root = snap?.state && typeof snap.state === 'object' ? snap.state : snap || {};
  const p = root.posters;
  if (p && typeof p === 'object') {
    S.status = p.status || S.status;
    S.copy = p.copy && typeof p.copy === 'object' ? p.copy : S.copy;
    S.startedMs = Number(p.started_ms) || 0;
    S.doneMs = Number(p.done_ms) || 0;
    for (const raw of Array.isArray(p.items) ? p.items : []) {
      if (!raw?.format) continue;
      const it = item(String(raw.format), raw);
      if (raw.label) it.label = String(raw.label);
      if (raw.aspect) it.aspect = String(raw.aspect);
      if (raw.aspect_requested) it.defaultAspect = String(raw.aspect_requested);
      it.status = raw.status || it.status;
      it.error = raw.error || null;
      it.variants = new Map();
      for (const v of Array.isArray(raw.variants) ? raw.variants : []) {
        const idx = Number(v?.idx);
        if (!Number.isInteger(idx) || !v.url) continue;
        it.variants.set(idx, { idx, url: String(v.url), latency_ms: v.latency_ms ?? null, api_path: v.api_path || '', v: v.latency_ms ?? null });
      }
      it.winner = Number.isInteger(raw.winner) ? raw.winner : null;
      it.score = raw.score ?? null;
      it.rationale = raw.rationale || '';
      it.by = raw.by || it.by;
      endRender(it);
    }
  }
  const locs = root.localizations && typeof root.localizations === 'object' ? root.localizations : {};
  for (const [market, loc] of Object.entries(locs)) {
    if (Array.isArray(loc?.posters) && loc.posters.length) {
      const row = S.localized.get(market) || new Map();
      for (const lp of loc.posters) if (lp?.format && lp.url) row.set(String(lp.format), { format: String(lp.format), url: String(lp.url), latency_ms: lp.latency_ms ?? null, v: lp.latency_ms ?? null });
      S.localized.set(market, row);
    } else if (loc?.plan && Object.keys(loc.plan).length && loc.status !== 'error') S.pendingMarkets.add(market);
  }
}

/** Apply one SSE event. Every branch is an idempotent upsert, so history replays are harmless. */
function applyEvent(ev) {
  if (Number.isFinite(ev.t)) {
    S.lastT = ev.t;
    S.lastAt = performance.now();
  }
  if (!EVENT_TYPES.has(ev.type)) return false;
  switch (ev.type) {
    case 'poster_status': {
      const st = ev.status;
      if (ev.market) {
        // Localized-poster failure for one market: never a kit-level state change.
        if (st === 'error') S.failedMarkets.add(String(ev.market));
        return true;
      }
      if (ev.format) {
        const it = item(String(ev.format));
        if (st === 'start') beginRender([it.format]);
        else if (st === 'done') {
          it.status = 'done';
          endRender(it);
        } else if (st === 'error') {
          it.status = it.winner != null ? 'done' : 'error';
          it.error = ev.error || 'render failed';
          endRender(it);
        }
        S.busy.delete(it.format);
      } else if (st === 'start') {
        const formats = Array.isArray(ev.formats) && ev.formats.length ? ev.formats.map(String) : null;
        const partial = !!formats && S.status === 'done' && FORMATS.some((f) => !formats.includes(f.format));
        if (!partial) {
          S.startedT = Number.isFinite(ev.t) ? ev.t : S.startedT;
          S.doneT = null;
        }
        S.partial = partial;
        S.status = 'rendering';
        S.error = null;
        beginRender(formats);
      } else if (st === 'done') {
        if (!S.partial || S.doneT == null) S.doneT = Number.isFinite(ev.t) ? ev.t : S.doneT;
        S.status = 'done';
        S.partial = false;
        for (const it of S.items.values()) {
          if (it.status === 'rendering') it.status = it.winner != null ? 'done' : it.variants.size ? 'done' : 'idle';
          endRender(it);
        }
        S.kitBusy = false;
        S.busy.clear();
      } else if (st === 'error') {
        S.status = 'error';
        S.partial = false;
        S.error = ev.error || 'campaign kit failed';
        for (const it of S.items.values()) {
          if (it.status === 'rendering') it.status = it.winner != null ? 'done' : 'error';
          endRender(it);
        }
        S.kitBusy = false;
        S.busy.clear();
      }
      return true;
    }
    case 'posters_copy':
      if (ev.copy && typeof ev.copy === 'object') S.copy = ev.copy;
      return true;
    case 'poster_variant': {
      if (!ev.format) return false;
      const idx = Number(ev.idx);
      if (!Number.isInteger(idx)) return false;
      const it = item(String(ev.format));
      if (it.status !== 'done') it.status = 'rendering';
      // Version = latency (stable between the snapshot and the replayed event, different after a regenerate).
      it.variants.set(idx, { idx, url: String(ev.url || ''), latency_ms: ev.latency_ms ?? null, api_path: ev.api_path || '', v: ev.latency_ms ?? ev.seq ?? null });
      return true;
    }
    case 'poster': {
      if (!ev.format) return false;
      const it = item(String(ev.format), ev);
      if (ev.label) it.label = String(ev.label);
      if (ev.aspect) it.aspect = String(ev.aspect);
      const w = Number(ev.winner);
      if (Number.isInteger(w)) {
        it.winner = w;
        const known = it.variants.get(w);
        if (!known && ev.url) it.variants.set(w, { idx: w, url: String(ev.url), latency_ms: null, api_path: '', v: ev.seq ?? ev.t });
      }
      if (ev.score != null) it.score = ev.score;
      if (ev.rationale != null) it.rationale = String(ev.rationale);
      it.by = ev.by || 'judge';
      it.status = 'done';
      it.error = null;
      endRender(it);
      S.busy.delete(it.format);
      return true;
    }
    case 'localize_poster': {
      if (!ev.market || !ev.format) return false;
      const market = String(ev.market);
      const row = S.localized.get(market) || new Map();
      row.set(String(ev.format), { format: String(ev.format), url: String(ev.url || ''), latency_ms: ev.latency_ms ?? null, v: ev.latency_ms ?? ev.seq ?? null });
      S.localized.set(market, row);
      S.failedMarkets.delete(market);
      return true;
    }
    case 'localize_plan':
      if (!ev.market) return false;
      S.pendingMarkets.add(String(ev.market));
      S.failedMarkets.delete(String(ev.market));
      return true;
    case 'localize_status':
      if (ev.market && ev.status === 'error') S.failedMarkets.add(String(ev.market));
      return true;
    default:
      return false;
  }
}

/** Plain, serializable snapshot for `getState()` / `onChange` subscribers. */
function snapshot() {
  const items = [...S.items.values()].map((it) => {
    const w = winnerVariant(it);
    return {
      format: it.format,
      label: it.label,
      aspect: it.aspect,
      status: displayStatus(it),
      winner: it.winner,
      url: w ? safeUrl(w.url) : null,
      score: it.score,
      rationale: it.rationale,
      by: it.by,
      error: it.error,
      variants: [...it.variants.values()].sort((a, b) => a.idx - b.idx).map((v) => ({ idx: v.idx, url: safeUrl(v.url), latency_ms: v.latency_ms, api_path: v.api_path })),
    };
  });
  const localized = {};
  for (const [market, row] of S.localized) localized[market] = [...row.values()].map((p) => ({ format: p.format, url: safeUrl(p.url), latency_ms: p.latency_ms }));
  return { runId: S.runId, status: S.status, error: S.error, copy: S.copy ? { ...S.copy } : null, items, localized, count: posterCount(), elapsed_ms: elapsedMs() };
}

// ─────────────────────────────────────────────────────────── render scheduling

let dirty = false;

function markDirty() {
  if (dirty) return;
  dirty = true;
  requestAnimationFrame(flush);
}

function flush() {
  dirty = false;
  try {
    render();
  } catch (err) {
    console.error('[posters] render failed', err);
  }
  for (const el of [...slides]) {
    if (el.isConnected) {
      el.dataset.pkSeen = '1';
      drawSlide(el);
    } else if (el.dataset.pkSeen === '1') slides.delete(el);
  }
  if (listeners.size) {
    const snap = snapshot();
    for (const cb of listeners) {
      try {
        cb(snap);
      } catch (err) {
        console.error('[posters] onChange listener failed', err);
      }
    }
  }
}

// ─────────────────────────────────────────────────────────── section DOM

const ui = { root: null, cards: new Map(), locRows: new Map() };

/** Find/create the section: #posters-mount → after the final-cut section → end of <main>. */
function mount() {
  let root = document.getElementById('posters-mount');
  if (!root) {
    root = h('section', { id: 'posters-mount' });
    const finalPanel = document.getElementById('final-panel');
    const anchor = finalPanel ? (finalPanel.parentElement?.classList.contains('duo') ? finalPanel.parentElement : finalPanel) : null;
    if (anchor?.parentElement) anchor.after(root);
    else (document.getElementById('run-view') || document.querySelector('main') || document.body).appendChild(root);
  }
  root.classList.add('panel', 'glass', 'pad', 'pk-panel');
  root.setAttribute('aria-label', 'Campaign kit');
  root.hidden = true;

  const r = {};
  r.chip = h('span', { class: 'status-chip', dataset: { status: 'idle' } }, 'waiting');
  r.counter = h('span', { class: 'pk-counter mono' });
  r.zip = h('a', { class: 'btn grad sm pk-zip', href: '#', download: 'campaign_kit.zip', title: 'Your film, music, voice, pictures and posters in one .zip' }, '⬇ Download everything (.zip)');
  r.head = h(
    'div',
    { class: 'section-head pk-head' },
    h('h2', {}, h('span', { class: 'step-no pro-only' }, 'KIT'), 'Campaign Kit ', h('span', { class: 'muted small pro-only' }, '— Nano Banana 2 Lite · print · social · web'), h('span', { class: 'muted small simple-only' }, '— posters for social, print and web')),
    h('div', { class: 'pk-head-right' }, r.chip, h('span', { class: 'pro-only' }, r.counter), r.zip),
  );

  r.headline = h('div', { class: 'pk-headline' });
  r.subline = h('div', { class: 'pk-subline' });
  r.cta = h('span', { class: 'cta-chip pk-cta' });
  r.copyText = h('div', { class: 'pk-copy-text' }, h('span', { class: 'pk-eyebrow mono pro-only' }, 'POSTER COPY · rendered in-image by NB2'), h('span', { class: 'pk-eyebrow mono simple-only' }, 'THE WORDS ON YOUR POSTERS'), r.headline, r.subline, r.cta);
  r.kitInput = h('input', { type: 'text', maxlength: '300', placeholder: 'Want a different look? e.g. bolder text, night-time colours', 'aria-label': 'What should change on all posters' });
  r.kitBtn = h('button', { type: 'submit', class: 'btn sm ghost' }, '↻ All formats');
  r.kitForm = h('form', { class: 'pk-kit-form inline-form', autocomplete: 'off' }, r.kitInput, r.kitBtn);
  r.kitForm.addEventListener('submit', (e) => {
    e.preventDefault();
    regenerate(null, r.kitInput.value);
  });
  r.copy = h('div', { class: 'pk-copy' }, r.copyText);

  r.note = h('p', { class: 'pk-note muted small pro-only' });
  r.toolbar = h('div', { class: 'pk-toolbar' }, r.note, r.kitForm);
  r.error = h('p', { class: 'pk-error small', role: 'alert', hidden: true });
  r.tall = h('div', { class: 'pk-row pk-row-tall' });
  r.wide = h('div', { class: 'pk-row pk-row-wide' });
  r.wall = h('div', { class: 'pk-wall' }, r.tall, r.wide);

  r.locGrid = h('div', { class: 'pk-loc-grid' });
  r.loc = h(
    'div',
    { class: 'pk-loc', hidden: true },
    h('div', { class: 'pk-loc-head' }, h('h3', {}, 'Localized posters'), h('span', { class: 'muted small pro-only' }, 'NB2 edits of every winner · headline & CTA translated, layout and product kept'), h('span', { class: 'muted small simple-only' }, 'Your posters, adapted for each market')),
    r.locGrid,
  );

  root.replaceChildren(r.head, r.toolbar, r.error, r.copy, r.wall, r.loc);
  ui.root = root;
  ui.r = r;
}

/** Build one format card (once per format per run); `updateCard` keeps it in sync. */
function buildCard(it) {
  const c = { format: it.format };
  c.shimmer = h('div', { class: 'pk-shimmer' });
  c.img = h('img', { class: 'pk-img', alt: '', decoding: 'async', draggable: 'false' });
  c.img.addEventListener('load', () => c.img.classList.add('pk-in'));
  c.img.addEventListener('error', () => c.frame.classList.add('pk-broken'));
  c.lat = h('span', { class: 'badge pk-lat mono pro-only' });
  c.score = h('span', { class: 'badge pk-score mono pro-only' });
  c.by = h('span', { class: 'badge pk-by pro-only' });
  c.state = h('span', { class: 'pk-state mono' });
  c.frame = h('button', { type: 'button', class: 'pk-frame' }, c.shimmer, c.img, c.lat, c.score, c.by, c.state);
  c.frame.addEventListener('click', () => {
    const v = shownVariant(S.items.get(it.format));
    if (v?.url) openLightbox({ kind: 'format', format: it.format });
  });
  c.label = h('b', { class: 'pk-label' });
  c.ar = h('span', { class: 'pk-ar mono' });
  c.variants = h('div', { class: 'pk-variants', role: 'group', 'aria-label': 'Variants — click to make one the winner' });
  c.regen = h('button', { type: 'button', class: 'btn xs ghost pk-regen', title: 'Make another version of this poster' }, h('span', { class: 'pk-ico', 'aria-hidden': 'true' }, '↻'), h('span', { class: 'pk-btn-lbl' }, 'New version'));
  c.dl = h('a', { class: 'btn xs ghost pk-dl', href: '#', download: `poster_${it.format}.png`, hidden: true, title: 'Download this poster' }, h('span', { class: 'pk-ico', 'aria-hidden': 'true' }, '⬇'), h('span', { class: 'pk-btn-lbl' }, 'Download'));
  c.input = h('input', { type: 'text', maxlength: '300', placeholder: 'Optional: what should change?', 'aria-label': 'What should change on this poster' });
  c.form = h('form', { class: 'pk-regen-form', autocomplete: 'off', hidden: true }, c.input, h('button', { type: 'submit', class: 'btn xs grad' }, 'Go'));
  c.regen.addEventListener('click', () => {
    c.form.hidden = !c.form.hidden;
    if (!c.form.hidden) c.input.focus();
  });
  c.form.addEventListener('submit', (e) => {
    e.preventDefault();
    regenerate([it.format], c.input.value).then((ok) => {
      if (ok) {
        c.input.value = '';
        c.form.hidden = true;
      }
    });
  });
  c.form.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      c.form.hidden = true;
    }
  });
  c.rationale = h('p', { class: 'pk-rationale pro-only' });
  c.meta = h(
    'div',
    { class: 'pk-meta' },
    h('div', { class: 'pk-title' }, c.label, c.ar),
    c.variants,
    h('div', { class: 'pk-actions' }, c.dl, c.regen),
    c.form,
    c.rationale,
  );
  c.el = h('article', { class: 'pk-card', dataset: { format: it.format } }, c.frame, c.meta);
  c.sig = { vars: '', src: '' };
  return c;
}

function updateCard(c, it) {
  const st = displayStatus(it);
  const ar = parseAspect(it.aspect);
  c.el.dataset.status = st;
  const dimmed = isDimmed(it);
  c.el.classList.toggle('pk-stale', dimmed);
  c.el.classList.toggle('pk-busy', S.busy.has(it.format));
  c.el.style.setProperty('--pk-r', ar.r.toFixed(4));
  c.frame.style.aspectRatio = `${ar.w} / ${ar.h}`;
  setText(c.label, it.label);
  setText(c.ar, it.aspect !== it.defaultAspect ? `${it.aspect} (asked ${it.defaultAspect})` : it.aspect);
  c.ar.title = it.aspect !== it.defaultAspect ? 'The model rejected the requested aspect; the nearest supported one was used.' : '';

  const v = shownVariant(it);
  const src = v?.url ? versioned(v.url, v.v) : '';
  if (src !== c.sig.src) {
    c.sig.src = src;
    c.frame.classList.remove('pk-broken');
    c.img.classList.remove('pk-in');
    if (src) {
      c.img.src = src;
      c.img.hidden = false;
    } else {
      c.img.removeAttribute('src');
      c.img.hidden = true;
    }
  }
  c.img.alt = v?.url ? `${it.label} poster` : '';
  c.frame.setAttribute('aria-label', v?.url ? `Open the ${it.label} poster full size` : `${it.label} — not rendered yet`);
  c.frame.disabled = !v?.url;
  c.shimmer.hidden = !((st === 'rendering' || st === 'judging') && (!v?.url || dimmed));

  if (v?.url) {
    const dlHref = safeUrl(v.url);
    if (c.dl.getAttribute('href') !== dlHref) c.dl.setAttribute('href', dlHref);
    c.dl.setAttribute('download', `poster_${it.format}.${extOf(v.url)}`);
  }
  c.dl.hidden = !v?.url;
  setText(c.lat, v?.latency_ms != null ? `⚡ ${fmtMs(v.latency_ms)}` : '');
  c.lat.title = v?.api_path ? `API path: ${v.api_path}` : '';
  const showWinner = it.winner != null && !it.stale && v?.idx === it.winner;
  const showScore = showWinner && it.score != null;
  setText(c.score, showScore ? fmtScore(it.score) : '');
  c.score.title = showScore ? `${it.by === 'user' ? 'Your pick' : 'Judge winner'} · ${it.rationale || ''}` : '';
  c.frame.title = showWinner && it.rationale ? `${it.by === 'user' ? 'Your pick' : 'Judge'}: ${it.rationale}` : '';
  setText(c.by, showWinner && it.by === 'user' ? 'your pick' : '');

  const done = freshVariants(it).length;
  const stateText =
    st === 'rendering'
      ? dimmed
        ? (isPro() ? 're-rendering…' : 'making a new version…')
        : (isPro() ? `rendering ${Math.min(done, VARIANTS_PER_FORMAT)}/${VARIANTS_PER_FORMAT}` : 'designing…')
      : st === 'judging'
        ? (isPro() ? 'judging…' : 'picking the best…')
        : st === 'error'
          ? `⚠ ${it.error || 'failed'}`
          : st === 'idle'
            ? S.status === 'error' ? 'not rendered' : 'queued'
            : '';
  setText(c.state, stateText);
  c.state.title = st === 'error' ? String(it.error || '') : '';

  const rationale = it.stale ? '' : it.rationale || '';
  setText(c.rationale, rationale);
  c.rationale.title = rationale;
  c.rationale.hidden = !rationale;

  const list = sortedVariants(it);
  const vsig = list.map((x) => `${x.idx}:${x.url}:${x.v}:${x.latency_ms}`).join('|') + `#${it.winner}#${it.stale}#${ar.r}`;
  if (vsig !== c.sig.vars) {
    c.sig.vars = vsig;
    c.variants.replaceChildren(
      ...list.map((x) =>
        h(
          'button',
          {
            type: 'button',
            class: `pk-vthumb ${x.idx === it.winner && !it.stale ? 'on' : ''}`,
            style: { aspectRatio: `${ar.w} / ${ar.h}` },
            title: `Variant ${x.idx}${x.latency_ms != null ? ` · ${fmtMs(x.latency_ms)}` : ''}${x.idx === it.winner ? ' · winner' : ' · click to make it the winner'}`,
            'aria-pressed': x.idx === it.winner ? 'true' : 'false',
            onclick: () => selectVariant(it.format, x.idx),
          },
          h('img', { src: versioned(x.url, x.v), alt: `Variant ${x.idx}`, loading: 'lazy', decoding: 'async', draggable: 'false' }),
          h('span', { class: 'pk-vidx mono pro-only' }, `V${x.idx}`),
        ),
      ),
    );
  }
  c.regen.disabled = !S.runId || st === 'rendering' || S.busy.has(it.format);
  for (const b of c.variants.querySelectorAll('button')) b.disabled = st === 'rendering' || S.busy.has(it.format);
}

/** Localized poster grid: one row per market, one aspect-true cell per format (in format order). */
function renderLocalized(r) {
  const kitLive = S.status === 'rendering' || S.status === 'done';
  const markets = [...new Set([...S.localized.keys(), ...(kitLive ? [...S.pendingMarkets] : [])])];
  r.loc.hidden = markets.length === 0;
  const formats = [...S.items.values()].filter((it) => it.winner != null || [...S.localized.values()].some((m) => m.has(it.format)));
  const seen = new Set();
  markets.forEach((market, i) => {
    seen.add(market);
    let row = ui.locRows.get(market);
    if (!row) {
      row = { cells: new Map(), cellsEl: h('div', { class: 'pk-loc-cells' }) };
      row.el = h('div', { class: 'pk-loc-row' }, h('div', { class: 'pk-loc-market' }, h('b', {}, market), (row.sub = h('span', { class: 'muted small mono' }))), row.cellsEl);
      ui.locRows.set(market, row);
    }
    if (r.locGrid.children[i] !== row.el) r.locGrid.insertBefore(row.el, r.locGrid.children[i] || null);
    const got = S.localized.get(market) || new Map();
    const failed = S.failedMarkets.has(market) && !got.size;
    setText(row.sub, failed ? 'localization failed' : `${got.size}/${formats.length || FORMATS.length} posters`);
    const want = formats.map((it) => it.format);
    for (const f of got.keys()) if (!want.includes(f)) want.push(f);
    want.forEach((format, j) => {
      let cell = row.cells.get(format);
      const it = S.items.get(format);
      const ar = parseAspect(it?.aspect || '1:1');
      if (!cell) {
        cell = { src: '' };
        cell.img = h('img', { alt: '', decoding: 'async', draggable: 'false' });
        cell.img.addEventListener('load', () => cell.img.classList.add('pk-in'));
        cell.shimmer = h('div', { class: 'pk-shimmer' });
        cell.lat = h('span', { class: 'badge pk-lat mono pro-only' });
        cell.cap = h('span', { class: 'pk-cell-cap mono' });
        cell.el = h('button', { type: 'button', class: 'pk-loc-cell' }, cell.shimmer, cell.img, cell.lat, cell.cap);
        cell.el.addEventListener('click', () => openLightbox({ kind: 'loc', market, format }));
        row.cells.set(format, cell);
      }
      if (row.cellsEl.children[j] !== cell.el) row.cellsEl.insertBefore(cell.el, row.cellsEl.children[j] || null);
      cell.el.style.aspectRatio = `${ar.w} / ${ar.h}`;
      const p = got.get(format);
      const src = p?.url ? versioned(p.url, p.v) : '';
      if (src !== cell.src) {
        cell.src = src;
        cell.img.classList.remove('pk-in');
        if (src) {
          cell.img.src = src;
          cell.img.hidden = false;
        } else {
          cell.img.removeAttribute('src');
          cell.img.hidden = true;
        }
      }
      cell.img.alt = p?.url ? `${it?.label || format} · ${market}` : '';
      cell.shimmer.hidden = !!p?.url || failed;
      cell.el.disabled = !p?.url;
      cell.el.title = `${it?.label || format} · ${market}`;
      setText(cell.lat, p?.latency_ms != null ? fmtMs(p.latency_ms) : '');
      setText(cell.cap, it?.label || format);
    });
    for (const [format, cell] of row.cells) {
      if (!want.includes(format)) {
        cell.el.remove();
        row.cells.delete(format);
      }
    }
  });
  for (const [market, row] of ui.locRows) {
    if (!seen.has(market)) {
      row.el.remove();
      ui.locRows.delete(market);
    }
  }
}

function render() {
  if (!ui.root) return;
  const r = ui.r;
  ui.root.hidden = !S.runId;
  if (!S.runId) return;

  // Header: status chip, live counter, kit link.
  const chipStatus = S.status === 'rendering' ? 'rendering' : S.status === 'done' ? 'done' : S.status === 'error' ? 'error' : 'idle';
  r.chip.dataset.status = chipStatus;
  setText(r.chip, { rendering: 'designing…', done: 'ready', error: 'something went wrong', idle: 'waiting for your scenes' }[chipStatus]);
  const n = posterCount();
  const el = elapsedMs();
  setText(r.counter, n || el != null ? `${n} poster${n === 1 ? '' : 's'}${el != null ? ` · ${fmtMs(el)}` : ''}` : '');
  const href = runPath(S.runId, '/kit.zip');
  if (r.zip.getAttribute('href') !== href) r.zip.setAttribute('href', href);
  r.zip.setAttribute('download', `campaign_kit_${S.runId}.zip`);
  r.zip.classList.toggle('pk-ready', S.status === 'done');

  // Copy block.
  const copy = S.copy || {};
  r.copy.hidden = !S.copy && S.status !== 'rendering';
  setText(r.headline, copy.headline || '');
  setText(r.subline, copy.subline || '');
  setText(r.cta, copy.cta || '');
  r.cta.hidden = !copy.cta;
  r.copyText.classList.toggle('pk-empty', !S.copy);
  if (!S.copy && S.status === 'rendering') setText(r.headline, 'Writing poster copy…');
  r.kitBtn.disabled = S.kitBusy || S.status === 'rendering';
  r.kitInput.disabled = S.kitBusy;
  setText(r.kitBtn, S.status === 'idle' || S.status === 'error' ? '✦ Make my posters' : '↻ Make another version');

  // Idle / error notes.
  const note =
    S.status === 'idle'
      ? 'Posters start the moment every scene has a winning keyframe — they render while Omni is still animating, so the kit adds ~zero wall-clock.'
      : S.status === 'rendering'
        ? `${VARIANTS_PER_FORMAT} variants per format in parallel, continuity-anchored to the hero keyframe · Gemini Flash judges legibility, brand, composition and impact.`
        : '';
  setText(r.note, note);
  setText(r.error, S.error ? (document.body.classList.contains('pro') ? `Campaign kit error: ${S.error}` : 'We couldn\u2019t finish your posters. Try \u201cMake another version\u201d.') : '');
  r.error.hidden = !S.error;

  // Format cards, split into a tall row and a wide row; each row is justified (flex-grow ∝ aspect ratio).
  const sums = { tall: [0, 0], wide: [0, 0] };
  for (const it of S.items.values()) {
    let c = ui.cards.get(it.format);
    if (!c) {
      c = buildCard(it);
      ui.cards.set(it.format, c);
    }
    updateCard(c, it);
    const ratio = parseAspect(it.aspect).r;
    const kind = ratio >= WIDE_MIN_RATIO ? 'wide' : 'tall';
    const row = kind === 'wide' ? r.wide : r.tall;
    if (c.el.parentElement !== row) row.appendChild(c.el);
    sums[kind][0] += ratio;
    sums[kind][1] += 1;
  }
  for (const [kind, row] of [['tall', r.tall], ['wide', r.wide]]) {
    row.style.setProperty('--pk-sum', sums[kind][0].toFixed(4));
    row.style.setProperty('--pk-n', String(sums[kind][1]));
    row.hidden = sums[kind][1] === 0;
  }
  renderLocalized(r);
  if (lightbox.open) updateLightbox();
}

// ─────────────────────────────────────────────────────────── actions

async function selectVariant(format, idx) {
  const runId = S.runId;
  const it = S.items.get(format);
  if (!runId || !it || it.winner === idx || S.busy.has(format)) return;
  const prev = { winner: it.winner, by: it.by };
  it.winner = idx; // optimistic; the server confirms with `poster` {by: "user"}
  it.by = 'user';
  S.busy.add(format);
  markDirty();
  try {
    await api(runPath(runId, `/posters/${encodeURIComponent(format)}/select`), { idx });
  } catch (err) {
    if (S.runId === runId) {
      it.winner = prev.winner;
      it.by = prev.by;
      toast(`Could not select variant: ${err.message}`, 'error');
    }
  } finally {
    if (S.runId === runId) {
      S.busy.delete(format);
      markDirty();
    }
  }
}

/** Regenerate the given formats (null = all) with an optional instruction. Resolves true on success. */
async function regenerate(formats, instruction) {
  const runId = S.runId;
  if (!runId) return false;
  const body = {};
  if (formats && formats.length) body.formats = formats;
  const text = String(instruction || '').trim().slice(0, 300);
  if (text) body.instruction = text;
  if (formats) for (const f of formats) S.busy.add(f);
  else S.kitBusy = true;
  markDirty();
  try {
    await api(runPath(runId, '/posters'), body);
    if (S.runId === runId) {
      toast(formats ? `Regenerating ${formats.map((f) => S.items.get(f)?.label || f).join(', ')}…` : 'Regenerating the campaign kit…', 'ok');
      if (!formats && ui.r) ui.r.kitInput.value = '';
      if (formats) {
        // Safety net: never leave a card locked if the server emits no per-format events.
        setTimeout(() => {
          if (S.runId !== runId) return;
          for (const f of formats) S.busy.delete(f);
          markDirty();
        }, 90000);
      }
    }
    return true;
  } catch (err) {
    if (S.runId === runId) toast(`Regenerate failed: ${err.message}`, 'error');
    if (formats) for (const f of formats) S.busy.delete(f);
    S.kitBusy = false;
    return false;
  } finally {
    if (S.runId === runId) {
      if (!formats) S.kitBusy = false;
      markDirty();
    }
  }
}

// ─────────────────────────────────────────────────────────── lightbox

const lightbox = { open: false, el: null, list: [], idx: 0, r: null };

/** Everything viewable full-size: the shown poster of each format, then localized posters by market. */
function galleryList() {
  const out = [];
  for (const it of S.items.values()) {
    const v = shownVariant(it);
    if (!v?.url) continue;
    const winner = it.winner != null && v.idx === it.winner && !it.stale;
    out.push({
      key: `format:${it.format}`,
      url: versioned(v.url, v.v),
      raw: v.url,
      title: it.label,
      sub: [it.aspect, isPro() && winner && it.score != null ? `score ${fmtScore(it.score)}` : '', winner ? (it.by === 'user' ? 'your pick' : isPro() ? 'judge winner' : 'our pick') : `option ${v.idx}`, isPro() && v.latency_ms != null ? fmtMs(v.latency_ms) : ''].filter(Boolean).join(' · '),
      rationale: winner ? it.rationale : '',
      filename: `poster_${it.format}.${extOf(v.url)}`,
    });
  }
  for (const [market, row] of S.localized) {
    for (const it of S.items.values()) {
      const p = row.get(it.format);
      if (!p?.url) continue;
      out.push({ key: `loc:${market}:${it.format}`, url: versioned(p.url, p.v), raw: p.url, title: `${it.label} · ${market}`, sub: [it.aspect, 'localized', isPro() && p.latency_ms != null ? fmtMs(p.latency_ms) : ''].filter(Boolean).join(' · '), rationale: '', filename: `poster_${slug(market)}_${it.format}.${extOf(p.url)}` });
    }
  }
  return out;
}

function buildLightbox() {
  const r = {};
  r.img = h('img', { class: 'pk-lb-img', alt: '' });
  r.title = h('b', { class: 'pk-lb-title' });
  r.sub = h('span', { class: 'pk-lb-sub mono' });
  r.rationale = h('p', { class: 'pk-lb-rationale pro-only' });
  r.dl = h('a', { class: 'btn grad sm', href: '#', download: 'poster.png' }, '⬇ Download');
  r.open = h('a', { class: 'btn ghost sm', href: '#', target: '_blank', rel: 'noopener' }, 'Open ↗');
  r.prev = h('button', { type: 'button', class: 'pk-lb-nav prev', 'aria-label': 'Previous poster' }, '‹');
  r.next = h('button', { type: 'button', class: 'pk-lb-nav next', 'aria-label': 'Next poster' }, '›');
  r.close = h('button', { type: 'button', class: 'btn ghost sm', 'aria-label': 'Close' }, 'Esc');
  r.count = h('span', { class: 'mono muted small' });
  const el = h(
    'div',
    { class: 'pk-lightbox', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Poster viewer', hidden: true },
    h('figure', { class: 'pk-lb-figure' }, r.img, h('figcaption', { class: 'pk-lb-cap' }, h('div', { class: 'pk-lb-text' }, r.title, r.sub, r.rationale), h('div', { class: 'pk-lb-actions' }, r.count, r.open, r.dl, r.close))),
    r.prev,
    r.next,
  );
  el.addEventListener('click', (e) => {
    if (e.target === el) closeLightbox();
  });
  r.close.addEventListener('click', closeLightbox);
  r.prev.addEventListener('click', () => stepLightbox(-1));
  r.next.addEventListener('click', () => stepLightbox(1));
  document.body.appendChild(el);
  lightbox.el = el;
  lightbox.r = r;
}

function openLightbox(target) {
  if (!lightbox.el) buildLightbox();
  lightbox.list = galleryList();
  const key = target.kind === 'loc' ? `loc:${target.market}:${target.format}` : `format:${target.format}`;
  const i = lightbox.list.findIndex((x) => x.key === key);
  if (i < 0) return;
  lightbox.idx = i;
  lightbox.open = true;
  lightbox.el.hidden = false;
  lightbox.returnFocus = document.activeElement;
  updateLightbox();
  lightbox.r.close.focus({ preventScroll: true });
}

function closeLightbox() {
  if (!lightbox.open) return;
  lightbox.open = false;
  lightbox.el.hidden = true;
  lightbox.returnFocus?.focus?.({ preventScroll: true });
}

function stepLightbox(d) {
  const n = lightbox.list.length;
  if (!n) return;
  lightbox.idx = (lightbox.idx + d + n) % n;
  updateLightbox();
}

function updateLightbox() {
  const cur = lightbox.list[lightbox.idx];
  const fresh = galleryList();
  if (cur) {
    const j = fresh.findIndex((x) => x.key === cur.key);
    lightbox.idx = j >= 0 ? j : Math.min(lightbox.idx, fresh.length - 1);
  }
  lightbox.list = fresh;
  const x = lightbox.list[lightbox.idx];
  if (!x) return closeLightbox();
  const r = lightbox.r;
  if (r.img.getAttribute('src') !== x.url) r.img.src = x.url;
  r.img.alt = x.title;
  setText(r.title, x.title);
  setText(r.sub, x.sub);
  setText(r.rationale, x.rationale ? `“${x.rationale}”` : '');
  r.rationale.hidden = !x.rationale;
  r.dl.href = safeUrl(x.raw);
  r.dl.setAttribute('download', x.filename);
  r.open.href = x.url;
  setText(r.count, `${lightbox.idx + 1} / ${lightbox.list.length}`);
  r.prev.hidden = r.next.hidden = lightbox.list.length < 2;
}

// ─────────────────────────────────────────────────────────── presentation slide

function slideSignature() {
  const parts = [S.runId, S.status, S.copy?.headline, S.copy?.cta];
  for (const it of S.items.values()) {
    const v = winnerVariant(it) || shownVariant(it);
    parts.push(`${it.format}:${it.aspect}:${v?.url}:${v?.v}:${it.score}:${it.stale}`);
  }
  for (const [m, row] of S.localized) parts.push(`${m}:${[...row.values()].map((p) => `${p.format}${p.v}`).join(',')}`);
  return parts.join('|');
}

/** Draw the "Campaign kit" poster wall into `el` (a `.slide` section built by app.js presentation mode). */
function drawSlide(el) {
  const sig = slideSignature();
  if (el._pkSig === sig) return;
  el._pkSig = sig;
  const items = [...S.items.values()];
  const done = items.filter((it) => shownVariant(it)?.url);
  const figure = (it) => {
    const v = winnerVariant(it) || shownVariant(it);
    const ar = parseAspect(it.aspect);
    return h(
      'figure',
      { class: 'pk-slide-poster', style: { '--pk-r': ar.r.toFixed(4) } },
      h(
        'div',
        { class: 'pk-slide-frame', style: { aspectRatio: `${ar.w} / ${ar.h}` } },
        v?.url ? h('img', { src: versioned(v.url, v.v), alt: it.label, decoding: 'async' }) : h('div', { class: 'pk-shimmer' }),
        it.winner != null && it.score != null && !it.stale ? h('span', { class: 'badge pk-score mono pro-only' }, fmtScore(it.score)) : null,
      ),
      h('figcaption', {}, h('b', {}, it.label), h('span', { class: 'mono muted' }, it.aspect)),
    );
  };
  const tall = items.filter((it) => parseAspect(it.aspect).r < WIDE_MIN_RATIO);
  const wide = items.filter((it) => parseAspect(it.aspect).r >= WIDE_MIN_RATIO);
  const markets = [...S.localized.entries()].filter(([, row]) => row.size);
  const copy = S.copy || {};
  el.classList.add('slide-kit');
  el.classList.toggle('pk-has-loc', markets.length > 0);
  // replaceChildren() would stringify null placeholders into "null" text nodes: filter them first.
  const parts = [
    h(
      'div',
      { class: 'pk-slide-head' },
      h('h2', {}, 'Campaign kit ', h('span', { class: 'muted' }, `Nano Banana 2 Lite · ${done.length} formats · judged by Gemini Flash`)),
      copy.headline ? h('div', { class: 'pk-slide-copy' }, h('span', { class: 'pk-slide-headline grad-text' }, copy.headline), copy.cta ? h('span', { class: 'cta-chip' }, copy.cta) : null) : null,
    ),
    h('div', { class: 'pk-slide-wall' }, tall.length ? h('div', { class: 'pk-slide-row tall' }, tall.map(figure)) : null, wide.length ? h('div', { class: 'pk-slide-row wide' }, wide.map(figure)) : null),
    markets.length
      ? h(
          'div',
          { class: 'pk-slide-loc' },
          h('span', { class: 'mono muted' }, 'LOCALIZED'),
          markets.map(([market, row]) => {
            const first = FORMATS.map((f) => row.get(f.format)).find((p) => p?.url) || [...row.values()].find((p) => p.url);
            const it = first ? S.items.get(first.format) : null;
            const ar = parseAspect(it?.aspect || '1:1');
            return h(
              'figure',
              { class: 'pk-slide-locitem' },
              first ? h('img', { src: versioned(first.url, first.v), alt: market, style: { aspectRatio: `${ar.w} / ${ar.h}` } }) : null,
              h('figcaption', {}, h('b', {}, market), h('span', { class: 'muted mono' }, `${row.size} posters`)),
            );
          }),
        )
      : null,
    S.status === 'idle' && !done.length ? h('p', { class: 'muted' }, 'The campaign kit renders as soon as the storyboard is locked.') : null,
  ];
  el.replaceChildren(...parts.filter(Boolean));
}

// ─────────────────────────────────────────────────────────── run lifecycle + SSE

function closeStream() {
  stream.gen++;
  if (stream.es) {
    try {
      stream.es.close();
    } catch {
      /* already closed */
    }
  }
  stream.es = null;
}

function connect(runId, { replay = false, speed = 4 } = {}) {
  closeStream();
  const gen = stream.gen;
  stream.lastSeq = 0;
  stream.applied = 0;
  stream.replay = replay;
  const url = runPath(runId, `/events${replay ? `?replay=1&speed=${encodeURIComponent(String(speed))}` : ''}`);
  let es;
  try {
    es = new EventSource(url);
  } catch (err) {
    console.error('[posters] EventSource failed', err);
    return;
  }
  stream.es = es;
  let received = 0;
  es.onopen = () => {
    if (gen === stream.gen) received = 0;
  };
  es.onmessage = (m) => {
    if (gen !== stream.gen) return;
    received++;
    let ev;
    try {
      ev = JSON.parse(m.data);
    } catch {
      return;
    }
    if (!ev || typeof ev !== 'object') return;
    if (Number.isFinite(ev.seq)) {
      if (ev.seq <= stream.lastSeq) return;
      stream.lastSeq = ev.seq;
    } else if (received <= stream.applied) return;
    stream.applied = Math.max(stream.applied, received);
    let changed = false;
    try {
      changed = applyEvent(ev);
    } catch (err) {
      console.error('[posters] event handler failed', ev?.type, err);
    }
    if (changed) markDirty();
  };
  es.onerror = () => {
    if (gen !== stream.gen) return;
    if (replay) {
      // Paced replays end by design: hand over to a live stream (history is skipped by seq).
      const lastSeq = stream.lastSeq;
      closeStream();
      stream.replay = false;
      connectLive(runId, lastSeq);
    }
    // Live streams: EventSource reconnects by itself; the server re-sends history, deduped by seq.
  };
}

function connectLive(runId, lastSeq) {
  connect(runId);
  stream.lastSeq = lastSeq;
}

/** Switch to a run (or to none). Resets all state and DOM; loads the snapshot (unless replaying); opens SSE. */
async function openRun(runId, { replay = false, speed = 4 } = {}) {
  closeStream();
  S = freshState(runId);
  for (const c of ui.cards.values()) c.el.remove();
  ui.cards.clear();
  for (const row of ui.locRows.values()) row.el.remove();
  ui.locRows.clear();
  if (lightbox.open) closeLightbox();
  markDirty();
  if (!runId) return;
  if (!replay) {
    try {
      const snap = await api(runPath(runId));
      if (S.runId !== runId) return;
      loadSnapshot(snap);
      markDirty();
    } catch (err) {
      if (S.runId !== runId) return;
      if (err.status === 404) return; // unknown run: stay quiet, app.js reports it
      console.warn('[posters] snapshot failed; relying on the event stream', err);
    }
  }
  if (S.runId === runId) connect(runId, { replay, speed });
}

function syncHash() {
  const id = runIdFromHash();
  if (id !== S.runId) openRun(id);
}

// ─────────────────────────────────────────────────────────── boot + public API

function onKeydown(e) {
  if (!lightbox.open) return;
  if (e.key === 'Escape') closeLightbox();
  else if (e.key === 'ArrowRight') stepLightbox(1);
  else if (e.key === 'ArrowLeft') stepLightbox(-1);
  else if (e.key === 'Tab') {
    const f = [...lightbox.el.querySelectorAll('a[href], button:not([hidden])')].filter((x) => !x.hidden);
    if (f.length) {
      const i = f.indexOf(document.activeElement);
      const next = f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length];
      next.focus();
    }
  } else if (e.key.length === 1 && !e.metaKey && !e.ctrlKey) {
    // Swallow studio shortcuts (T / P) while the viewer is open.
  } else return;
  e.preventDefault();
  e.stopPropagation();
}

function boot() {
  mount();
  window.addEventListener('keydown', onKeydown, true);
  window.addEventListener('hashchange', syncHash);
  setInterval(syncHash, HASH_POLL_MS);
  setInterval(() => {
    if (S.runId && S.status === 'rendering' && ui.r) {
      const n = posterCount();
      const el = elapsedMs();
      setText(ui.r.counter, `${n} poster${n === 1 ? '' : 's'}${el != null ? ` · ${fmtMs(el)}` : ''}`);
    }
  }, CLOCK_TICK_MS);
  syncHash();
  markDirty();
}

window.AdMatePosters = Object.freeze({
  /** Plain snapshot of the campaign kit for the current run. */
  getState: snapshot,
  /** Subscribe to state snapshots (called after each render). Returns an unsubscribe function. */
  onChange(cb) {
    if (typeof cb !== 'function') return () => {};
    listeners.add(cb);
    return () => listeners.delete(cb);
  },
  /** Draw the "Campaign kit" poster wall into a presentation slide element; it stays live while attached. */
  renderSlide(el) {
    if (!(el instanceof Element)) return el;
    slides.add(el);
    try {
      el._pkSig = null;
      drawSlide(el);
    } catch (err) {
      console.error('[posters] renderSlide failed', err);
    }
    return el;
  },
  /**
   * Follow a run explicitly, e.g. from app.js `openRun(id, {replay: true, speed})` so the kit replays in step with
   * the sample run. The hash watcher keeps running: it only switches runs when `#run=` names a different run.
   */
  attach(runId, opts = {}) {
    if (!runId) return Promise.resolve();
    return openRun(String(runId), opts);
  },
  /** Re-load the snapshot for the current run (keeps the stream). */
  async refresh() {
    const runId = S.runId;
    if (!runId) return;
    try {
      const snap = await api(runPath(runId));
      if (S.runId === runId) {
        loadSnapshot(snap);
        markDirty();
      }
    } catch (err) {
      console.warn('[posters] refresh failed', err);
    }
  },
  FORMATS: FORMATS.map((f) => ({ ...f })),
});

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
else boot();
