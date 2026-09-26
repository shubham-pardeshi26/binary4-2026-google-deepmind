/**
 * AdLoop studio front-end (CONTRACT §8).
 *
 * Architecture (no framework, no build step):
 *   - One state store (`store.run`) shaped exactly like `GET /api/runs/{id}` (CONTRACT §6).
 *     It is rebuilt from that snapshot on load, then patched by SSE events through
 *     idempotent reducers (`REDUCERS`), so a reconnect or history replay never
 *     duplicates tiles, clips or music versions.
 *   - UI-only state (stage timers, local pending edits, selected versions...) lives in `ui`
 *     and is reset whenever a run is (re)opened.
 *   - Rendering is batched to one pass per animation frame (`markDirty`). Every section
 *     renderer is idempotent and uses keyed reconciliation (`reconcile`) so existing DOM
 *     nodes are updated in place: tiles pop in exactly once, videos never restart.
 *   - A 200 ms ticker (`tick`) updates only live clocks / progress rings.
 *   - All model-provided text is inserted with textContent (via `h`) -- never innerHTML --
 *     and every URL / colour from the backend is validated before use.
 *
 * Sections:
 *   0. Constants          5. Render scheduler + helpers   10. Localize
 *   1. DOM utilities      6. Top bar / rail / plan         11. Director bar
 *   2. Formatting         7. Storyboard fan-out            12. Telemetry + event log
 *   3. API + toasts       8. Motion lab + voiceover        13. Brief panel + launch
 *   4. Store + stream     9. Soundtrack + final cut        14. Voice capture
 *                                                          15. Presentation mode
 *                                                          16. Boot
 */

// ═══════════════════════════════════════════════════════════════════════════
// 0. Constants
// ═══════════════════════════════════════════════════════════════════════════

/** Sample briefs offered as one-click chips in the brief panel. */
const SAMPLE_BRIEFS = [
  {
    label: 'Irani chai café · Hyderabad',
    brand: 'Dum & Co.',
    brief:
      'Launch film for a new Irani chai café in Hyderabad’s Old City. Steaming glasses of dum chai, Osmania biscuits ' +
      'dunked just right, the Charminar glowing at golden hour, old friends arguing about cricket. Warm, nostalgic, ' +
      'a little cheeky. Close on the line “Your daily dum.”',
  },
  {
    label: 'EV scooter for Gen-Z commuters',
    brand: 'Zyp',
    brief:
      'A punchy spot for an electric scooter aimed at Gen-Z college commuters in Bengaluru. Neon dusk, flyovers, ' +
      'weaving past traffic in silence, charging at a café while friends laugh. Confident, fast, playful. ' +
      'End card: “Zero noise. All go.”',
  },
  {
    label: 'Monsoon sneaker drop',
    brand: 'Puddle',
    brief:
      'Limited monsoon sneaker drop: waterproof knit sneakers splashing through Mumbai rain, slow-motion droplets, ' +
      'reflections on wet streets, a dancer on a rooftop in the downpour. Moody teal and amber, cinematic, kinetic. ' +
      'Tagline: “Made for the wet season.”',
  },
];

/** Localization markets (CONTRACT §8.11). */
const MARKETS = ['Hyderabad · Telugu', 'Mumbai · Hindi', 'Chennai · Tamil', 'Tokyo · Japanese', 'USA · English'];

/** Quick edit chips offered on every clip card (CONTRACT §8.6). */
const CLIP_QUICK_EDITS = ['slower push-in', 'golden hour light', 'add gentle rain', 'orbit the product'];

/** Backend stage names that belong to scene narration (TTS); the rail and timeline accept any of them. */
const VOICE_STAGES = ['voiceover', 'voice', 'tts'];

/**
 * Pipeline rail definition. Each node aggregates one or more backend `stage` names
 * (CONTRACT §6 events table) and shows live state + elapsed time.
 */
const RAIL = [
  { key: 'director', label: 'Director', model: 'Gemini Flash', stages: ['director', 'direct'] },
  { key: 'storyboard', label: 'Storyboard', model: 'Nano Banana 2 Lite', stages: ['anchor', 'storyboard'] },
  { key: 'judge', label: 'Judge', model: 'Flash vision', stages: ['judge'] },
  { key: 'motion', label: 'Motion', model: 'Omni Flash', stages: ['motion'] },
  { key: 'music', label: 'Score', model: 'Lyria 3.5', stages: ['music'] },
  { key: 'voice', label: 'Voice', model: 'Flash TTS', stages: VOICE_STAGES },
  { key: 'final', label: 'Final cut', model: 'ffmpeg', stages: ['final'] },
  { key: 'localize', label: 'Localize', model: 'NB2 + Lyria', stages: ['localize'], optional: true },
];

/** Variants per self-repair round (mirrors app/pipeline.py) — sizes the anticipatory shimmer tiles. */
const REPAIR_VARIANTS = 2;

/** Nominal concurrency caps used to scale the in-flight bars (mirror app/config.py defaults). */
const INFLIGHT_CAPS = { image: 8, video: 4, music: 3, text: 6, tts: 6 };

/** In-flight modalities, in display order, with their short telemetry labels. */
const INFLIGHT_LABELS = { image: 'NB2', video: 'Omni', music: 'Lyria', text: 'Flash', tts: 'TTS' };

/** Telemetry tiles: [metrics key, label, format]. */
const TELEMETRY_TILES = [
  ['images_generated', 'Images', 'n'],
  ['images_per_min', 'Images / min', 'f1'],
  ['image_p50_ms', 'NB2 p50', 'ms'],
  ['image_p95_ms', 'NB2 p95', 'ms'],
  ['time_to_first_image_ms', 'First image', 'ms'],
  ['time_to_first_clip_ms', 'First clip', 'ms'],
  ['time_to_final_ms', 'First cut', 'ms'],
  ['wall_ms', 'Wall time', 'ms'],
  ['judge_calls', 'Judge calls', 'n'],
  ['repair_rounds', 'Repair rounds', 'n'],
  ['videos_generated', 'Omni clips', 'n'],
  ['video_p50_ms', 'Omni p50', 'ms'],
  ['video_edits', 'Omni edits', 'n'],
  ['music_versions', 'Lyria versions', 'n'],
  ['voiceovers_generated', 'Voiceovers', 'n'],
  ['tts_p50_ms', 'TTS p50', 'ms'],
];

/** Human names for backend stages, used in error toasts ("S2 clip failed: …"). */
const STAGE_NOUN = {
  director: 'plan', anchor: 'anchor', storyboard: 'keyframes', judge: 'judge', motion: 'clip', music: 'soundtrack',
  final: 'final cut', localize: 'localize', direct: 'direction', voiceover: 'voiceover', voice: 'voiceover', tts: 'voiceover',
};

/** Presentation timing: pause after narration ends, and the length of slides without narration. */
const SLIDE_GAP_MS = 800;
const SLIDE_TIMED_MS = 4000;

/** Web fonts (attached at boot so they never block first paint; system fallbacks render meanwhile). */
const FONTS_URL = 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap';

/** localStorage key for the telemetry drawer's open/closed choice (default: closed). */
const TELE_KEY = 'adloop.telemetry';

/** A manual scroll suppresses automatic scrolling for this long. */
const MANUAL_SCROLL_GRACE_MS = 10000;

const REPLAY_SPEED = 3;
const LOG_MAX = 300;
const SVG_NS = 'http://www.w3.org/2000/svg';

// ═══════════════════════════════════════════════════════════════════════════
// 1. DOM utilities
// ═══════════════════════════════════════════════════════════════════════════

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/**
 * Hyperscript element builder. Strings become text nodes (XSS-safe); `on*` props
 * become listeners; `dataset`/`style` objects are merged; `--custom` style props are
 * set via setProperty; everything else is assigned as a property or attribute.
 */
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'style' && typeof v === 'object') {
        for (const [sk, sv] of Object.entries(v)) {
          if (sv == null) continue;
          if (sk.startsWith('--')) el.style.setProperty(sk, sv);
          else el.style[sk] = sv;
        }
      } else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k in el && typeof v !== 'boolean' && k !== 'list' && k !== 'form') el[k] = v;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  appendKids(el, children);
  return el;
}

function appendKids(el, kids) {
  for (const c of kids) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) appendKids(el, c);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** Set text only when it changed (avoids layout churn on every frame). */
function setText(el, text) {
  const t = text == null ? '' : String(text);
  if (el && el.textContent !== t) el.textContent = t;
}

/** Toggle a class. */
function cls(el, name, on) {
  if (el) el.classList.toggle(name, !!on);
}

/** Assign a media src only when it actually changed, so playback never restarts needlessly. */
function setSrc(el, url) {
  const u = safeUrl(url);
  if (!el || el.dataset.src === u) return false;
  el.dataset.src = u;
  if (u) el.src = u;
  else el.removeAttribute('src');
  return true;
}

/**
 * Keyed reconciliation: make `container`'s children mirror `items` in order,
 * reusing nodes by key. `create(item)` builds a node, `update(node, item)` patches it.
 * Containers passed here must only hold keyed children.
 */
function reconcile(container, items, keyOf, create, update) {
  const existing = new Map();
  for (const el of Array.from(container.children)) existing.set(el.dataset.key, el);
  const keep = new Set();
  items.forEach((item, i) => {
    const key = String(keyOf(item));
    let el = existing.get(key);
    if (!el) {
      el = create(item);
      el.dataset.key = key;
    }
    keep.add(el);
    update(el, item);
    const at = container.children[i];
    if (at !== el) container.insertBefore(el, at || null);
  });
  for (const el of existing.values()) if (!keep.has(el)) el.remove();
}

/** SVG progress ring (used for rendering clips). Returns {el, set(progress 0..1)}. */
function progressRing(size = 56) {
  const r = 22;
  const c = 2 * Math.PI * r;
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 52 52');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.classList.add('ring');
  const mk = (klass) => {
    const ci = document.createElementNS(SVG_NS, 'circle');
    ci.setAttribute('cx', '26');
    ci.setAttribute('cy', '26');
    ci.setAttribute('r', String(r));
    ci.setAttribute('class', klass);
    svg.appendChild(ci);
    return ci;
  };
  mk('ring-track');
  const bar = mk('ring-bar');
  bar.style.strokeDasharray = String(c);
  bar.style.strokeDashoffset = String(c);
  return {
    el: svg,
    set(p) {
      bar.style.strokeDashoffset = String(c * (1 - Math.max(0, Math.min(1, p))));
    },
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// 2. Formatting + validation
// ═══════════════════════════════════════════════════════════════════════════

/** Human latency: 850 ms · 1.4 s · 2m 05s · — for missing. */
function fmtMs(ms) {
  if (ms == null || !Number.isFinite(Number(ms))) return '—';
  const v = Number(ms);
  if (v < 1000) return `${Math.round(v)} ms`;
  if (v < 60000) return `${(v / 1000).toFixed(1)} s`;
  const m = Math.floor(v / 60000);
  const s = Math.floor((v % 60000) / 1000);
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

function fmtClock(ms) {
  const v = Math.max(0, Number(ms) || 0);
  if (v < 60000) return `${(v / 1000).toFixed(1)} s`;
  const m = Math.floor(v / 60000);
  const s = ((v % 60000) / 1000).toFixed(1).padStart(4, '0');
  return `${m}:${s}`;
}

function fmtMetric(v, kind) {
  if (v == null || v === '' || !Number.isFinite(Number(v))) return '—';
  if (kind === 'ms') return fmtMs(v);
  if (kind === 'f1') return Number(v).toFixed(1);
  return String(Math.round(Number(v)));
}

function fmtScore(v) {
  return Number.isFinite(Number(v)) ? Number(v).toFixed(1) : '';
}

function truncate(s, n) {
  const t = String(s ?? '');
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

/** Allow only same-origin paths, http(s), blob: and data:image URLs. */
function safeUrl(u) {
  if (typeof u !== 'string' || !u) return '';
  if (u.startsWith('/') && !u.startsWith('//')) return u;
  if (/^https?:\/\//i.test(u) || u.startsWith('blob:') || /^data:(image|audio|video)\//i.test(u)) return u;
  return '';
}

/** Validate a model-provided colour (#rgb / #rrggbb / #rrggbbaa). */
function safeColor(c) {
  return typeof c === 'string' && /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(c.trim()) ? c.trim() : null;
}

const clamp01 = (x) => Math.max(0, Math.min(1, Number(x) || 0));

/** Map energy 0..1 onto the signature gradient violet → magenta → amber. */
function energyColor(e) {
  const stops = [
    [124, 92, 255],
    [255, 79, 216],
    [255, 181, 71],
  ];
  const x = clamp01(e) * 2;
  const i = Math.min(1, Math.floor(x));
  const f = x - i;
  const [a, b] = [stops[i], stops[i + 1]];
  const mix = a.map((v, k) => Math.round(v + (b[k] - v) * f));
  return `rgb(${mix.join(',')})`;
}

const isPortrait = (run) => (run?.input?.aspect || '16:9') === '9:16';

// ═══════════════════════════════════════════════════════════════════════════
// 3. API client + toasts
// ═══════════════════════════════════════════════════════════════════════════

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/**
 * fetch wrapper: JSON or multipart body, parses JSON replies, turns non-2xx into
 * ApiError with the backend's `{"error"}` message (or FastAPI's `detail`).
 */
async function api(path, { method = 'GET', json, form } = {}) {
  const opts = { method, headers: {} };
  if (json !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(json);
  } else if (form) {
    opts.body = form;
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError('Network error — is the AdLoop server running?', 0);
  }
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    let msg = body?.error || body?.detail || `${res.status} ${res.statusText || 'error'}`;
    if (Array.isArray(msg)) msg = msg.map((d) => d?.msg || JSON.stringify(d)).join('; ');
    throw new ApiError(String(msg), res.status);
  }
  return body;
}

/**
 * Run a user action (POST) with consistent UX: optional busy button, toast on
 * failure. Returns the response body or null on failure. Never throws.
 */
async function act(label, fn, { button, success } = {}) {
  if (button) button.disabled = true;
  try {
    const out = await fn();
    if (success) toast(success, 'ok');
    return out ?? {};
  } catch (err) {
    toast(`${label}: ${err?.message || err}`, 'error');
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

/** Recently shown toast texts / error scopes → timestamp, for de-duplication. */
const recentToasts = new Map();

/** True (and records `key`) unless the same key was seen within `windowMs`. */
function firstWithin(key, windowMs) {
  const now = performance.now();
  const last = recentToasts.get(key);
  recentToasts.set(key, now);
  return last == null || now - last > windowMs;
}

/** Slide-in toast. kind ∈ info | ok | warn | error. Identical messages within 3 s are shown once. */
function toast(msg, kind = 'info', ms = 4200) {
  const host = $('#toasts');
  if (!host || !firstWithin(`${kind}|${msg}`, 3000)) return;
  const el = h(
    'div',
    { class: `toast ${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    h('span', { class: 'toast-dot' }),
    h('span', { class: 'toast-msg' }, truncate(msg, 280)),
  );
  el.addEventListener('click', () => dismiss());
  host.appendChild(el);
  while (host.children.length > 5) host.firstChild.remove();
  requestAnimationFrame(() => el.classList.add('in'));
  const timer = setTimeout(dismiss, kind === 'error' ? ms * 1.6 : ms);
  function dismiss() {
    clearTimeout(timer);
    el.classList.remove('in');
    el.classList.add('out');
    setTimeout(() => el.remove(), 320);
  }
}

/**
 * The single error-toast path for pipeline failures. The backend reports one failure as
 * `stage error` + `error` + `log`; only `error` events call this, and a scope (stage + scene)
 * toasts at most once per 4 s so a burst of failures never floods the stack.
 */
function notifyError(stage, rawMsg) {
  const m = /^(s\d+):\s*(.*)$/is.exec(String(rawMsg || ''));
  const sceneId = m ? m[1] : null;
  const msg = (m ? m[2] : rawMsg) || 'failed';
  if (!firstWithin(`err|${stage}|${sceneId || ''}`, 4000)) return;
  const noun = STAGE_NOUN[stage] || stage || 'pipeline';
  const who = sceneId && store.run ? `${sceneLabel(store.run, sceneId)} ` : '';
  toast(`${who}${noun} failed: ${msg}`, 'error');
}

// ═══════════════════════════════════════════════════════════════════════════
// 4. Store, reducers and the event stream
// ═══════════════════════════════════════════════════════════════════════════

/** The single source of truth: the run state (CONTRACT §6) plus the open run id. */
const store = { runId: null, run: null };

/** UI-only state; reset by `resetUi()` whenever a run is opened. */
let ui = freshUi();

function freshUi() {
  return {
    es: null, // EventSource
    streamGen: 0, // bumps on every (re)connect to drop stale handlers
    applied: 0, // events applied from the (deterministic) history sequence (fallback when no seq)
    lastSeq: 0, // highest event `seq` applied — primary dedupe key across reconnects/replays
    replay: false,
    replaySpeed: 1,
    sse: 'idle', // idle | live | reconnecting | replay | closed
    clock: { t: 0, at: performance.now(), speed: 1 },
    stages: {}, // stage name → {active:Set, first, burstStart, burstEnd, lastMs, errors, starts, dones}
    clipView: {}, // scene_id → chosen version v (else latest)
    clipTimers: {}, // scene_id → {ms, at, status}
    pendingEdits: {}, // scene_id → [{instruction, baseV}]
    pendingRegen: {}, // scene_id → true until the next new round lands
    regenRounds: {}, // scene_id → Set(round) produced by a user regenerate
    pendingDirect: null, // instruction awaiting a `direction` event
    directError: null, // last direction failure, shown in the fan-out row
    pendingMusic: false,
    pendingVoice: {}, // scene_id → text sent for re-voicing, until the new take lands
    musicView: null, // chosen music version v (else latest)
    locMarkets: new Set(),
    spans: [], // timeline spans {row, stage, start, end, status} built from `stage` events
    openSpans: {}, // `${stage}|${key}` → open span
    celebrated: false, // first-final banner shown for this run
    celebratePending: false, // the final panel should autoplay + glow on its next render
    flights: [], // scene ids whose fresh winner should fly to its clip card after the next render
    autoScrolled: new Set(), // one-shot auto-scroll targets already used for this run
    voiceSend: null, // {text, timer} while a spoken direction counts down to auto-submit
  };
}

function resetUi() {
  closeStream();
  clearTimeout(ui.voiceSend?.timer);
  const keepMarkets = ui.locMarkets;
  ui = freshUi();
  ui.locMarkets = keepMarkets;
  const log = $('#event-log');
  if (log) log.replaceChildren();
  const banner = $('#cut-banner');
  if (banner) banner.hidden = true;
}

/** An empty run shell (used for fresh launches and for replay). */
function emptyRun(id, input = {}, mode = 'mock', createdAt = Date.now() / 1000) {
  return normalizeRun({ id, status: 'running', mode, created_at: createdAt, input });
}

/**
 * Fill every field the UI touches with a safe default so renderers never have to
 * guard against partially-populated state (live model output may be partial).
 */
function normalizeRun(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  r.status = r.status || 'running';
  r.input = { brief: '', brand: '', aspect: '16:9', n_scenes: 4, variants: 4, markets: [], ...(r.input || {}) };
  r.plan = r.plan && typeof r.plan === 'object' ? r.plan : null;
  r.anchor = r.anchor && r.anchor.url ? r.anchor : null;
  r.scenes = Array.isArray(r.scenes) ? r.scenes.map(normalizeScene) : [];
  r.music = { status: 'idle', current: 0, error: null, versions: [], ...(r.music || {}) };
  if (!Array.isArray(r.music.versions)) r.music.versions = [];
  r.final = { status: 'idle', url: null, duration_s: null, version: 0, captions_url: null, ...(r.final || {}) };
  r.directions = Array.isArray(r.directions) ? r.directions : [];
  r.localizations = r.localizations && typeof r.localizations === 'object' ? r.localizations : {};
  for (const [m, loc] of Object.entries(r.localizations)) r.localizations[m] = normalizeLoc(loc);
  r.metrics = r.metrics && typeof r.metrics === 'object' ? r.metrics : {};
  return r;
}

function normalizeScene(s) {
  const sc = { title: '', beat: '', mood: '', energy: 0.5, duration_s: 6, winner: null, ...(s || {}) };
  sc.variants = Array.isArray(sc.variants) ? sc.variants.slice().sort((a, b) => a.idx - b.idx) : [];
  sc.judge = Array.isArray(sc.judge) ? sc.judge : [];
  sc.clip = { status: 'idle', elapsed_ms: 0, error: null, current: 0, versions: [], ...(sc.clip || {}) };
  if (!Array.isArray(sc.clip.versions)) sc.clip.versions = [];
  sc.voiceover = normalizeVoice(sc.voiceover);
  // Apply judge scores onto variants when the snapshot did not already do it.
  for (const j of sc.judge) applyScores(sc, j.scores);
  return sc;
}

/** Scene narration state (CONTRACT §9c). Accepts the plan's bare string line as well as the state object. */
function normalizeVoice(vo) {
  const base = { status: 'idle', v: 0, text: '', url: null, latency_ms: 0, duration_s: null, error: null };
  if (typeof vo === 'string') return { ...base, text: vo };
  return { ...base, ...(vo && typeof vo === 'object' ? vo : {}) };
}

function normalizeLoc(l) {
  const loc = { status: '', plan: null, scenes: [], music_url: null, error: null, voiceover: [], video_url: null, captions_url: null, ...(l || {}) };
  if (!Array.isArray(loc.scenes)) loc.scenes = [];
  if (!Array.isArray(loc.voiceover)) loc.voiceover = [];
  return loc;
}

/** Append a cache-busting token so a re-rendered asset reusing its file name is re-fetched. */
function withBust(url, token) {
  if (!url || token == null || token === '') return url;
  return `${url}${url.includes('?') ? '&' : '?'}v=${encodeURIComponent(String(token))}`;
}

function ensureScene(run, sceneId) {
  let s = run.scenes.find((x) => x.id === sceneId);
  if (!s) {
    s = normalizeScene({ id: sceneId, title: sceneId });
    run.scenes.push(s);
  }
  return s;
}

function ensureLoc(run, market) {
  if (!run.localizations[market]) run.localizations[market] = normalizeLoc({});
  return run.localizations[market];
}

/** Copy judge `overall` scores onto the matching variants (score idx may be `idx` or `index`). */
function applyScores(scene, scores) {
  if (!Array.isArray(scores)) return;
  for (const sc of scores) {
    const idx = sc?.idx ?? sc?.index;
    const v = scene.variants.find((x) => x.idx === idx);
    if (v && sc.overall != null) {
      v.score = Number(sc.overall);
      v.notes = sc.notes || v.notes;
    }
  }
}

/** Insert or merge a variant keyed by (idx, round). */
function upsertVariant(scene, v) {
  const existing = scene.variants.find((x) => x.idx === v.idx && (x.round ?? 0) === (v.round ?? 0));
  if (existing) {
    for (const [k, val] of Object.entries(v)) if (val != null) existing[k] = val;
    if (v.url) existing.error = null;
    return false;
  }
  scene.variants.push({ score: null, error: null, ...v });
  scene.variants.sort((a, b) => a.idx - b.idx);
  return true;
}

/** Replace-or-append `item` in `list`, matching on `item[key]`. */
function upsertBy(list, key, item) {
  const i = list.findIndex((x) => x[key] === item[key]);
  if (i >= 0) list[i] = item;
  else list.push(item);
}

/** Insert a versioned item (clip / music) keyed by `v`; returns true if new. */
function upsertVersion(list, item) {
  const i = list.findIndex((x) => x.v === item.v);
  if (i >= 0) {
    list[i] = { ...list[i], ...item };
    return false;
  }
  list.push(item);
  list.sort((a, b) => a.v - b.v);
  return true;
}

/** Merge the plan's scenes into run.scenes without clobbering media already received. */
function applyPlan(run, plan) {
  if (!plan || typeof plan !== 'object') return;
  run.plan = plan;
  const planned = Array.isArray(plan.scenes) ? plan.scenes : [];
  const next = planned.map((ps, i) => {
    const id = ps.id || `s${i + 1}`;
    const s = run.scenes.find((x) => x.id === id) || normalizeScene({ id });
    for (const k of ['title', 'beat', 'duration_s', 'on_screen_text', 'camera']) if (ps[k] != null && ps[k] !== '') s[k] = ps[k];
    if (!s.mood && ps.mood) s.mood = ps.mood;
    if (ps.energy != null && s._energyLocked !== true) s.energy = ps.energy;
    // The plan's narration line shows immediately; the TTS take replaces it when it lands.
    if (typeof ps.voiceover === 'string' && !s.voiceover.text) s.voiceover.text = ps.voiceover;
    return s;
  });
  // Keep any scenes that events created but the plan did not list (defensive).
  for (const s of run.scenes) if (!next.includes(s)) next.push(s);
  run.scenes = next;
}

/** Stage bookkeeping for the pipeline rail (per stage name; per-scene keys for fan-out stages). */
function applyStage(e) {
  const name = e.stage;
  if (!name) return;
  const st = (ui.stages[name] ||= { active: new Set(), first: null, burstStart: null, burstEnd: null, lastMs: null, errors: 0, starts: 0, dones: 0 });
  // Fan-out stages are keyed per scene; localize is keyed per market (sent as `detail`).
  const key = e.scene_id || e.market || (name === 'localize' ? e.detail : null) || '_';
  const t = Number(e.t) || 0;
  // A storyboard "repair round" start means the judge found the best frame under threshold.
  if (name === 'storyboard' && e.scene_id && store.run) {
    const s = store.run.scenes.find((x) => x.id === e.scene_id);
    if (s && /^repair/i.test(e.detail || '')) s._pendingRepair = e.status === 'start';
  }
  if (e.status === 'start') {
    if (st.active.size === 0) st.burstStart = t;
    if (st.first == null) st.first = t;
    st.active.add(key);
    st.starts++;
  } else if (e.status === 'done' || e.status === 'error') {
    st.active.delete(key);
    st.burstEnd = t;
    if (e.ms != null) st.lastMs = e.ms;
    if (e.status === 'error') st.errors++;
    else st.dones++;
    if (st.burstStart == null) st.burstStart = e.ms != null ? t - e.ms : t;
  }
  recordSpan(name, key, e, t);
  if (e.status !== 'error') return;
  // Failures must release every "in progress" affordance tied to this stage.
  if (name === 'storyboard' && e.scene_id) delete ui.pendingRegen[e.scene_id];
  if (name === 'direct') {
    ui.pendingDirect = null;
    ui.directError = e.detail || 'direction failed';
  }
}

/** Timeline row for a stage event: the scene for per-scene stages, else a shared lane. */
function spanRow(name, e) {
  if (e.scene_id) return e.scene_id;
  if (name === 'music') return '_music';
  if (VOICE_STAGES.includes(name)) return '_voice';
  if (name === 'localize') return '_loc';
  return '_plan';
}

/** Open / close a timeline span for the live waterfall (`renderGantt`). */
function recordSpan(name, key, e, t) {
  const id = `${name}|${key}`;
  if (e.status === 'start') {
    const span = { row: spanRow(name, e), stage: name, start: t, end: null, status: 'active' };
    ui.openSpans[id] = span;
    ui.spans.push(span);
    return;
  }
  if (e.status !== 'done' && e.status !== 'error') return;
  let span = ui.openSpans[id];
  delete ui.openSpans[id];
  if (!span) {
    // A done without a start (e.g. history trimmed): reconstruct it from `ms` when possible.
    if (e.ms == null) return;
    span = { row: spanRow(name, e), stage: name, start: Math.max(0, t - Number(e.ms)), end: null, status: 'active' };
    ui.spans.push(span);
  }
  span.end = t;
  span.status = e.status;
}

/** Track per-clip live elapsed timers from clip_status events. */
function touchClipTimer(sceneId, status, elapsedMs) {
  const active = status === 'queued' || status === 'rendering';
  const prev = ui.clipTimers[sceneId];
  if (!active) {
    delete ui.clipTimers[sceneId];
    return;
  }
  const ms = elapsedMs != null ? Number(elapsedMs) : prev && prev.status !== 'done' ? currentClipElapsed(sceneId) : 0;
  ui.clipTimers[sceneId] = { ms, at: performance.now(), status };
}

function currentClipElapsed(sceneId) {
  const t = ui.clipTimers[sceneId];
  if (!t) return 0;
  return t.ms + (performance.now() - t.at) * (ui.clock.speed || 1);
}

/** Is an event recent enough (not history) to deserve a toast? */
function isFresh(e) {
  if (ui.replay || !store.run?.created_at) return false;
  const wall = store.run.created_at * 1000 + (Number(e.t) || 0);
  return Math.abs(Date.now() - wall) < 20000;
}

/**
 * Idempotent reducers, one per event type (CONTRACT §6). Each mutates `run` in place.
 * Applying the same event twice must leave the state unchanged.
 */
const REDUCERS = {
  run_started(run, e) {
    if (e.mode) run.mode = e.mode;
    if (e.input) run.input = { ...run.input, ...e.input };
  },
  stage(run, e) {
    applyStage(e);
  },
  transcript() {},
  plan(run, e) {
    applyPlan(run, e.plan);
  },
  anchor(run, e) {
    if (e.url) run.anchor = { url: e.url, latency_ms: e.latency_ms };
  },
  variant(run, e) {
    const s = ensureScene(run, e.scene_id);
    const round = Number(e.round) || 0;
    const isNewRound = !s.variants.some((v) => (v.round ?? 0) === round);
    if (isNewRound && round > 0 && ui.pendingRegen[s.id]) {
      (ui.regenRounds[s.id] ||= new Set()).add(round);
      delete ui.pendingRegen[s.id];
    }
    upsertVariant(s, { idx: e.idx, round, url: e.url, latency_ms: e.latency_ms, api_path: e.api_path, kind: e.kind });
    const repairsIn = s.variants.filter((v) => (v.round ?? 0) === round && round > 0).length;
    if (round > 0 && repairsIn >= REPAIR_VARIANTS) s._pendingRepair = false;
  },
  variant_error(run, e) {
    const s = ensureScene(run, e.scene_id);
    const round = Number(e.round) || 0;
    const existing = s.variants.find((v) => v.idx === e.idx && (v.round ?? 0) === round);
    if (existing?.url) return;
    upsertVariant(s, { idx: e.idx, round, url: null, error: e.error || 'generation failed' });
  },
  judge(run, e) {
    const s = ensureScene(run, e.scene_id);
    const round = Number(e.round) || 0;
    const entry = {
      round,
      winner_index: e.winner_idx ?? e.winner_index,
      kind: e.kind || null,
      rationale: e.rationale || '',
      fix_instructions: e.fix_instructions || '',
      scores: Array.isArray(e.scores) ? e.scores : [],
      latency_ms: e.latency_ms,
    };
    const i = s.judge.findIndex((j) => (j.round ?? 0) === round);
    if (i >= 0) s.judge[i] = entry;
    else s.judge.push(entry);
    s.judge.sort((a, b) => (a.round ?? 0) - (b.round ?? 0));
    applyScores(s, entry.scores);
  },
  winner(run, e) {
    const s = ensureScene(run, e.scene_id);
    s.winner = e.idx;
    s._winnerBy = e.by || 'judge';
    s._pendingRepair = false;
    if (isFresh(e) && e.by !== 'user') ui.flights.push(s.id);
  },
  clip_status(run, e) {
    const s = ensureScene(run, e.scene_id);
    s.clip.status = e.status || s.clip.status;
    if (e.elapsed_ms != null) s.clip.elapsed_ms = e.elapsed_ms;
    s.clip.error = e.status === 'error' ? e.error || 'render failed' : null;
    // A failed edit on a clip that already has versions arrives as status "done" + error:
    // keep the error visible and fail the oldest pending chat bubble instead of spinning forever.
    if (e.error && e.status !== 'error') {
      s.clip.lastError = e.error;
      const p = (ui.pendingEdits[s.id] || []).find((x) => !x.failed);
      if (p) {
        Object.assign(p, { failed: true, error: e.error, failedAt: performance.now() });
        setTimeout(markDirty, 8100);
      }
    }
    touchClipTimer(s.id, s.clip.status, e.elapsed_ms);
  },
  clip(run, e) {
    const s = ensureScene(run, e.scene_id);
    const isNew = upsertVersion(s.clip.versions, {
      v: e.v,
      url: e.url,
      instruction: e.instruction ?? null,
      latency_ms: e.latency_ms,
      api_path: e.api_path,
      interaction_id: e.interaction_id,
      fallback: e.fallback,
      kind: e.kind,
    });
    s.clip.current = e.v;
    s.clip.status = 'done';
    s.clip.error = null;
    s.clip.lastError = null;
    touchClipTimer(s.id, 'done');
    if (isNew) delete ui.clipView[s.id];
  },
  voiceover_status(run, e) {
    const vo = ensureScene(run, e.scene_id).voiceover;
    vo.status = e.status || vo.status;
    vo.error = e.status === 'error' ? e.error || 'narration failed' : null;
    if (e.status === 'error') delete ui.pendingVoice[e.scene_id];
  },
  voiceover(run, e) {
    const s = ensureScene(run, e.scene_id);
    Object.assign(s.voiceover, {
      status: 'done',
      v: e.v ?? s.voiceover.v,
      text: e.text ?? s.voiceover.text,
      url: e.url || s.voiceover.url,
      latency_ms: e.latency_ms,
      duration_s: e.duration_s ?? null,
      error: null,
    });
    delete ui.pendingVoice[s.id];
  },
  scene_update(run, e) {
    const s = ensureScene(run, e.scene_id);
    if (e.mood != null) s.mood = e.mood;
    if (e.energy != null) {
      s.energy = e.energy;
      s._energyLocked = true;
    }
    s._pulseAt = e.seq ?? e.t; // the mood-timeline segment pulses once per update
  },
  music_status(run, e) {
    run.music.status = e.status || run.music.status;
    run.music.error = e.status === 'error' ? e.error || 'score failed' : null;
    if (e.reason) run.music._reason = e.reason;
    if (e.status !== 'rendering') ui.pendingMusic = false;
  },
  music(run, e) {
    const isNew = upsertVersion(run.music.versions, { v: e.v, url: e.url, prompt: e.prompt, latency_ms: e.latency_ms, reason: e.reason });
    run.music.current = e.v;
    run.music.status = 'done';
    run.music.error = null;
    ui.pendingMusic = false;
    if (isNew) ui.musicView = null;
  },
  final_status(run, e) {
    run.final.status = e.status || run.final.status;
    run.final.error = e.status === 'error' ? e.error || 'stitch failed' : null;
  },
  final(run, e) {
    const isNew = (e.version ?? 0) !== run.final.version || e.url !== run.final.url;
    Object.assign(run.final, {
      url: e.url,
      duration_s: e.duration_s,
      version: e.version ?? run.final.version,
      captions_url: e.captions_url ?? null,
      status: 'done',
      error: null,
    });
    if (isNew && isFresh(e)) onFreshFinal(run);
  },
  direction(run, e) {
    // Dedupe on the bus `seq`; a snapshot entry (no seq) is adopted by the first matching replayed event.
    const same = (d) => d.instruction === (e.instruction || '') && d.summary === (e.summary || '');
    const existing = run.directions.find((d) => (e.seq != null && d._seq === e.seq) || (d._seq == null && same(d)));
    if (existing) {
      if (e.plan) existing.plan = e.plan;
      if (e.seq != null) existing._seq = e.seq;
    } else {
      run.directions.push({ instruction: e.instruction || '', summary: e.summary || '', plan: e.plan || null, ts: e.t, _seq: e.seq });
    }
    if (ui.pendingDirect && (!e.instruction || e.instruction === ui.pendingDirect)) ui.pendingDirect = null;
    ui.directError = null;
  },
  localize_status(run, e) {
    const loc = ensureLoc(run, e.market);
    loc.status = e.status || loc.status;
    loc.error = e.status === 'error' ? e.error || 'localization failed' : null;
    if (e.status === 'rendering') Object.assign(loc, { scenes: [], voiceover: [], video_url: null, captions_url: null, music_url: null });
    if (e.status === 'done' || e.status === 'error') ui.stages.localize?.active.delete(e.market);
    // "No keyframes produced" is reported only here (not via an `error` event).
    if (e.status === 'error' && isFresh(e)) notifyError('localize', `${e.market}: ${loc.error}`);
  },
  localize_plan(run, e) {
    ensureLoc(run, e.market).plan = e.plan || null;
  },
  // Localized assets reuse file names across re-localizations; the event seq busts the media cache.
  localize_image(run, e) {
    const loc = ensureLoc(run, e.market);
    upsertBy(loc.scenes, 'scene_id', { scene_id: e.scene_id, url: withBust(e.url, e.seq), latency_ms: e.latency_ms });
  },
  localize_music(run, e) {
    const loc = ensureLoc(run, e.market);
    loc.music_url = withBust(e.url, e.seq);
    loc.music_latency_ms = e.latency_ms;
  },
  localize_voiceover(run, e) {
    const loc = ensureLoc(run, e.market);
    upsertBy(loc.voiceover, 'scene_id', { scene_id: e.scene_id, url: withBust(e.url, e.seq), text: e.text || '', latency_ms: e.latency_ms });
  },
  localize_video(run, e) {
    const loc = ensureLoc(run, e.market);
    Object.assign(loc, { video_url: withBust(e.url, e.seq), captions_url: withBust(e.captions_url, e.seq), video_duration_s: e.duration_s ?? null });
  },
  metrics(run, e) {
    if (e.metrics && typeof e.metrics === 'object') run.metrics = e.metrics;
  },
  log() {
    // Logged in the event log only: the matching `error` event carries the toast.
  },
  error(run, e) {
    if (e.stage === 'director' && !run.plan) run.status = 'error';
    if (e.stage === 'direct') {
      ui.pendingDirect = null;
      ui.directError = e.msg || 'direction failed';
    }
    if (isFresh(e)) notifyError(e.stage, e.msg);
  },
  run_done(run, e) {
    if (run.status !== 'error') run.status = 'done';
    if (e.wall_ms != null) run._wall_ms = e.wall_ms;
  },
};

/** Apply one SSE event to the store, log it, and schedule a render. Never throws. */
function handleEvent(e) {
  const run = store.run;
  if (!run || !e || typeof e !== 'object') return;
  if (e.run_id && e.run_id !== store.runId) return;
  const t = Number(e.t);
  if (Number.isFinite(t) && t >= ui.clock.t) ui.clock = { t, at: performance.now(), speed: ui.clock.speed };
  try {
    REDUCERS[e.type]?.(run, e);
  } catch (err) {
    console.warn('reducer failed', e.type, err);
  }
  if (e.type !== 'metrics') appendLog(e);
  markDirty();
}

function closeStream() {
  ui.streamGen++;
  if (ui.es) {
    try {
      ui.es.close();
    } catch {
      /* already closed */
    }
    ui.es = null;
  }
}

/**
 * Open the SSE stream. The backend sends full history first, then live events, on
 * every (re)connection. Events carry a monotonically increasing `seq`; anything at or
 * below `ui.lastSeq` is skipped, so native EventSource auto-reconnect never
 * double-applies history (per-connection message counting is the fallback).
 * In replay mode the server ends the stream after history; we then switch to a
 * live connection (which skips the whole already-replayed history the same way).
 */
function connect(runId, { replay = false, speed = REPLAY_SPEED } = {}) {
  closeStream();
  const gen = ui.streamGen;
  const url = `/api/runs/${encodeURIComponent(runId)}/events${replay ? `?replay=1&speed=${speed}` : ''}`;
  let es;
  try {
    es = new EventSource(url);
  } catch (err) {
    toast(`Event stream failed: ${err?.message || err}`, 'error');
    return;
  }
  ui.es = es;
  ui.replay = replay;
  ui.sse = replay ? 'replay' : 'live';
  let received = 0;
  es.onopen = () => {
    if (gen !== ui.streamGen) return;
    received = 0;
    ui.sse = replay ? 'replay' : 'live';
    markDirty();
  };
  es.onmessage = (m) => {
    if (gen !== ui.streamGen) return;
    received++;
    let ev;
    try {
      ev = JSON.parse(m.data);
    } catch {
      return;
    }
    // Dedupe: prefer the bus's monotonically increasing `seq`; fall back to per-connection counting.
    if (Number.isFinite(ev?.seq)) {
      if (ev.seq <= ui.lastSeq) return;
      ui.lastSeq = ev.seq;
    } else if (received <= ui.applied) return;
    ui.applied = Math.max(ui.applied, received);
    handleEvent(ev);
  };
  es.onerror = () => {
    if (gen !== ui.streamGen) return;
    if (replay) {
      // Replay streams end by design; hand over to a live stream so actions work.
      closeStream();
      ui.replay = false;
      ui.clock.speed = 1;
      toast('Replay finished — the run is live. Try directing it.', 'ok');
      refreshSnapshot(runId).finally(() => connect(runId));
      return;
    }
    ui.sse = es.readyState === EventSource.CLOSED ? 'closed' : 'reconnecting';
    markDirty();
  };
}

/** Merge a fresh snapshot's authoritative fields after a replay (keeps UI-only flags). */
async function refreshSnapshot(runId) {
  try {
    const snap = await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (store.runId !== runId || !snap) return;
    const fresh = normalizeRun(snap);
    // Keep locally-derived winner flags so crowns stay put.
    for (const s of fresh.scenes) {
      const old = store.run.scenes.find((x) => x.id === s.id);
      if (old) {
        s._winnerBy = old._winnerBy;
        s._energyLocked = old._energyLocked;
      }
    }
    fresh._wall_ms = store.run._wall_ms;
    store.run = fresh;
    markDirty();
  } catch {
    /* keep the replayed state */
  }
}

/**
 * Open a run: fetch its snapshot, render it, then attach the SSE stream.
 * With `replay`, local state is reset to an empty shell and history is re-streamed
 * at `speed`× so the fan-out plays back like the live run.
 */
async function openRun(id, { replay = false, shell = null, speed = REPLAY_SPEED } = {}) {
  if (!id) return;
  resetUi();
  store.runId = id;
  setHash(id);
  if (shell) {
    store.run = shell;
    initClock(shell);
    markDirty();
  }
  let snap = null;
  try {
    snap = await api(`/api/runs/${encodeURIComponent(id)}`);
  } catch (err) {
    if (store.runId !== id) return;
    if (!shell) {
      toast(`Could not open run ${id}: ${err.message}`, 'error');
      store.runId = null;
      store.run = null;
      setHash(null);
      markDirty();
      return;
    }
  }
  if (store.runId !== id) return; // superseded by another openRun
  if (replay) {
    store.run = emptyRun(id, snap?.input, snap?.mode, snap?.created_at);
    ui.clock = { t: 0, at: performance.now(), speed };
    ui.replaySpeed = speed;
  } else if (snap) {
    const run = normalizeRun(snap);
    if (shell) for (const s of run.scenes) s._winnerBy = shell.scenes.find((x) => x.id === s.id)?._winnerBy;
    store.run = run;
    initClock(run);
  }
  markDirty();
  connect(id, { replay, speed });
  if (replay) window.AdLoopPosters?.attach?.(id, { replay: true, speed });
}

function initClock(run) {
  const elapsed = run.created_at ? Math.max(0, Date.now() - run.created_at * 1000) : 0;
  ui.clock = { t: run.status === 'running' ? elapsed : 0, at: performance.now(), speed: 1 };
}

/** Extrapolated run-relative time (ms) — last event `t` plus local time since. */
function nowT() {
  return ui.clock.t + (performance.now() - ui.clock.at) * (ui.clock.speed || 1);
}

function setHash(id) {
  const target = id ? `#run=${id}` : ' ';
  if (id && location.hash === target) return;
  history.replaceState(null, '', id ? target : location.pathname + location.search);
}

function hashRunId() {
  const m = /(?:^#|&)run=([a-z0-9]+)/i.exec(location.hash);
  return m ? m[1] : null;
}

// ═══════════════════════════════════════════════════════════════════════════
// 5. Render scheduler + derived-state helpers
// ═══════════════════════════════════════════════════════════════════════════

let dirty = false;

function markDirty() {
  if (dirty) return;
  dirty = true;
  requestAnimationFrame(() => {
    dirty = false;
    renderAll();
  });
}

/** Run a section renderer in isolation so one malformed field never blanks the page. */
function safely(name, fn) {
  try {
    fn();
  } catch (err) {
    console.error(`render ${name} failed`, err);
  }
}

function renderAll() {
  const run = store.run;
  $('#hero').hidden = !!run;
  $('#run-view').hidden = !run;
  document.body.classList.toggle('has-run', !!run);
  safely('chips', renderModelChips);
  safely('telemetry', renderTelemetry);
  if (!run) {
    $('#director-bar').hidden = true;
    return;
  }
  $('#run-view').classList.toggle('portrait', isPortrait(run));
  safely('rail', renderRail);
  safely('gantt', renderGantt);
  safely('plan', renderPlan);
  safely('storyboard', renderStoryboard);
  safely('motion', renderMotion);
  safely('music', renderMusic);
  safely('final', renderFinal);
  safely('banner', renderCutBanner);
  safely('localize', renderLocalize);
  safely('director', renderDirectorBar);
  safely('present', refreshPresentation);
  safely('flights', flyWinners);
  tick();
}

/** Timestamp of the user's last manual scroll; auto-scrolling backs off for a while after it. */
let lastManualScroll = -Infinity;

function initScrollTracking() {
  const mark = () => (lastManualScroll = performance.now());
  window.addEventListener('wheel', mark, { passive: true });
  window.addEventListener('touchmove', mark, { passive: true });
  window.addEventListener('keydown', (ev) => {
    if (['PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown', ' '].includes(ev.key) && !ev.target.closest?.('input, textarea')) mark();
  });
}

/**
 * Scroll `el` into view once per run for `key` (e.g. the first frames, the first cut), but only
 * when the user has not scrolled by hand recently: never fight someone who is reading.
 */
function autoScrollOnce(key, el) {
  if (!el || ui.autoScrolled.has(key)) return;
  ui.autoScrolled.add(key);
  if (performance.now() - lastManualScroll < MANUAL_SCROLL_GRACE_MS || present.open) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** A scene's winner is "decided" once the judge (or the user) has named it. */
function winnerDecided(s) {
  return !!s._winnerBy || (s.judge.length > 0 && !s._pendingRepair && s.winner != null);
}

function winnerVariant(s) {
  if (s.winner == null) return null;
  return s.variants.find((v) => v.idx === s.winner) || null;
}

function winnerScore(s) {
  const v = winnerVariant(s);
  if (v?.score != null) return v.score;
  for (const j of [...s.judge].reverse()) {
    const sc = (j.scores || []).find((x) => (x.idx ?? x.index) === s.winner);
    if (sc?.overall != null) return Number(sc.overall);
  }
  return null;
}

/** Latest clip version the user should see (their pinned choice, else newest). */
function clipVersion(s) {
  const vs = s.clip.versions;
  if (!vs.length) return null;
  const pinned = ui.clipView[s.id];
  return vs.find((x) => x.v === pinned) || vs[vs.length - 1];
}

function sceneLabel(run, sceneId) {
  const i = run.scenes.findIndex((s) => s.id === sceneId);
  return i >= 0 ? `S${i + 1}` : sceneId;
}

/** Derived per-node status for the rail, merged with live stage events. */
function railNodeState(run, node) {
  const sts = node.stages.map((n) => ui.stages[n]).filter(Boolean);
  const eventActive = sts.some((s) => s.active.size > 0);
  const eventErr = sts.some((s) => s.errors > 0);
  const eventDone = sts.length > 0 && sts.every((s) => s.active.size === 0) && sts.some((s) => s.dones > 0);
  const scenes = run.scenes;
  const n = scenes.length;
  let derived = null;
  let detail = '';
  switch (node.key) {
    case 'director':
      derived = run.plan ? 'done' : run.status === 'error' ? 'error' : run.status === 'running' ? 'active' : null;
      if (ui.pendingDirect) derived = 'active';
      detail = run.directions.length ? `${run.directions.length} direction${run.directions.length > 1 ? 's' : ''}` : run.plan ? `${n} scenes` : 'planning';
      break;
    case 'storyboard': {
      const imgs = scenes.reduce((a, s) => a + s.variants.filter((v) => v.url).length, 0);
      derived = n && scenes.every(winnerDecided) ? 'done' : imgs || run.anchor ? 'active' : null;
      detail = imgs ? `${imgs} frames` : '';
      break;
    }
    case 'judge': {
      const judged = scenes.filter((s) => s.judge.length).length;
      const repairs = scenes.reduce((a, s) => a + s.judge.filter((j) => (j.round ?? 0) > 0).length, 0);
      derived = n && scenes.every(winnerDecided) ? 'done' : judged ? 'active' : null;
      detail = judged ? `${judged}/${n}${repairs ? ` · ${repairs} repair` : ''}` : '';
      break;
    }
    case 'motion': {
      const done = scenes.filter((s) => s.clip.versions.length).length;
      const busy = scenes.some((s) => ['queued', 'rendering'].includes(s.clip.status));
      derived = busy ? 'active' : n && done === n ? 'done' : done ? 'active' : null;
      if (scenes.some((s) => s.clip.status === 'error') && !busy) derived = 'error';
      detail = done || busy ? `${done}/${n} clips` : '';
      break;
    }
    case 'music':
      derived = { rendering: 'active', done: 'done', error: 'error' }[run.music.status] || null;
      detail = run.music.versions.length ? `v${run.music.versions.length}` : '';
      break;
    case 'voice': {
      const vos = scenes.map((s) => s.voiceover);
      const voiced = vos.filter((v) => v.url).length;
      const busy = vos.some((v) => v.status === 'rendering' || v.status === 'queued');
      derived = busy ? 'active' : n && voiced === n ? 'done' : vos.some((v) => v.status === 'error') ? 'error' : voiced ? 'active' : null;
      detail = voiced || busy ? `${voiced}/${n} lines` : '';
      break;
    }
    case 'final':
      derived = { rendering: 'active', done: 'done', error: 'error' }[run.final.status] || null;
      detail = run.final.version ? `v${run.final.version}` : '';
      break;
    case 'localize': {
      const locs = Object.values(run.localizations);
      derived = locs.some((l) => ['start', 'running', 'rendering'].includes(l.status)) ? 'active' : locs.length ? 'done' : null;
      detail = locs.length ? `${locs.length} market${locs.length > 1 ? 's' : ''}` : '';
      break;
    }
    default:
  }
  let status = eventActive ? 'active' : derived || (eventDone ? 'done' : eventErr ? 'error' : 'idle');
  if (status === 'active' && !eventActive && run.status !== 'running' && derived === 'active' && !['motion', 'director', 'voice'].includes(node.key)) status = 'done';
  // Elapsed: current burst while active, else the last completed burst.
  let elapsed = null;
  const bursts = sts.filter((s) => s.burstStart != null);
  if (bursts.length) {
    const start = Math.min(...bursts.map((s) => s.burstStart));
    const end = eventActive ? nowT() : Math.max(...bursts.map((s) => s.burstEnd ?? s.burstStart));
    elapsed = Math.max(0, end - start);
  }
  return { status, elapsed, detail };
}

/** Which modalities have in-flight work (backend metrics OR local derivation). */
function inflight(run) {
  const m = run?.metrics?.inflight || {};
  const out = Object.fromEntries(Object.keys(INFLIGHT_LABELS).map((k) => [k, Number(m[k]) || 0]));
  if (!run) return out;
  const active = (name) => (ui.stages[name]?.active.size || 0);
  out.image = Math.max(out.image, active('storyboard') + active('anchor') + active('localize'));
  out.video = Math.max(out.video, run.scenes.filter((s) => ['queued', 'rendering'].includes(s.clip.status)).length);
  out.music = Math.max(out.music, run.music.status === 'rendering' ? 1 : 0);
  out.text = Math.max(out.text, active('director') + active('judge') + active('direct') + (ui.pendingDirect ? 1 : 0));
  out.tts = Math.max(out.tts, run.scenes.filter((s) => s.voiceover.status === 'rendering').length);
  return out;
}

// ═══════════════════════════════════════════════════════════════════════════
// 6. Top bar chips, pipeline rail, creative plan
// ═══════════════════════════════════════════════════════════════════════════

/** Model chips pulse while their modality has work in flight; the telemetry toggle mirrors any activity. */
function renderModelChips() {
  const inf = inflight(store.run);
  let total = 0;
  for (const mod of Object.keys(INFLIGHT_LABELS)) {
    const chip = $(`#chip-${mod}`);
    const n = inf[mod] || 0;
    total += n;
    cls(chip, 'busy', n > 0);
    chip.dataset.count = n > 0 ? String(n) : '';
  }
  const dot = $('#tele-activity');
  dot.dataset.state = total > 0 ? 'busy' : store.run ? ui.sse : 'idle';
  $('#telemetry-toggle').title = `Toggle telemetry (T) · ${total} request${total === 1 ? '' : 's'} in flight`;
}

function renderRail() {
  const run = store.run;
  const list = $('#rail-nodes');
  const nodes = RAIL.filter((n) => !n.optional || Object.keys(run.localizations).length || ui.stages.localize);
  reconcile(
    list,
    nodes,
    (n) => n.key,
    (n) =>
      h(
        'li',
        { class: 'rail-node' },
        h('span', { class: 'rail-dot' }),
        h('div', { class: 'rail-text' }, h('span', { class: 'rail-label' }, n.label), h('span', { class: 'rail-model' }, n.model)),
        h('div', { class: 'rail-meta mono' }, h('span', { class: 'rail-elapsed' }, '—'), h('span', { class: 'rail-detail' })),
      ),
    (el, n) => {
      const st = railNodeState(run, n);
      el.dataset.status = st.status;
      setText($('.rail-detail', el), st.detail);
      setText($('.rail-elapsed', el), st.elapsed != null ? fmtMs(st.elapsed) : st.status === 'idle' ? '—' : '');
    },
  );
  const status = $('#run-status');
  const label = ui.replay ? `REPLAY ${ui.replaySpeed}×` : run.status === 'running' ? 'LIVE' : run.status === 'error' ? 'ERROR' : 'DONE';
  setText(status, label);
  status.dataset.status = ui.replay ? 'replay' : run.status;
}

/** Timeline colour group per backend stage (see `.g-*` in styles.css). */
const GANTT_GROUP = {
  director: 'text', direct: 'text', anchor: 'image', storyboard: 'image', localize: 'image', judge: 'judge',
  motion: 'video', music: 'music', final: 'final', voiceover: 'tts', voice: 'tts', tts: 'tts',
};
const GANTT_LEGEND = [['text', 'Flash'], ['image', 'NB2'], ['judge', 'Judge'], ['video', 'Omni'], ['music', 'Lyria'], ['tts', 'TTS'], ['final', 'Cut']];

/**
 * Live waterfall: one row per scene (+ plan, Lyria, localize lanes) with a coloured span per stage,
 * scaled to the run clock. It is the visual proof that scene 1 renders video while scene 4 still judges.
 */
function renderGantt() {
  const run = store.run;
  const section = $('#gantt');
  section.hidden = !ui.spans.length;
  if (!ui.spans.length) return;
  const legend = $('#gantt-legend');
  if (!legend.childElementCount) legend.append(...GANTT_LEGEND.map(([g, label]) => h('span', { class: `g-key g-${g}` }, label)));
  const now = nowT();
  const running = run.status === 'running';
  const last = Math.max(...ui.spans.map((sp) => sp.end ?? (running ? now : sp.start)));
  const total = Math.max(1000, running ? Math.max(now, last) : last);
  const used = new Set(ui.spans.map((sp) => sp.row));
  const rows = [
    { id: '_plan', label: 'Plan' },
    ...run.scenes.map((sc, i) => ({ id: sc.id, label: `S${i + 1}` })),
    { id: '_music', label: 'Lyria' },
    { id: '_voice', label: 'Voice' },
    { id: '_loc', label: 'Local' },
  ].filter((r) => used.has(r.id) || /^s/.test(r.id));
  reconcile(
    $('#gantt-body'),
    rows,
    (r) => r.id,
    (r) => h('div', { class: 'g-row' }, h('span', { class: 'g-label mono' }, r.label), h('div', { class: 'g-track' })),
    (el, r) => {
      const spans = ui.spans.map((sp, i) => ({ sp, i })).filter((x) => x.sp.row === r.id);
      reconcile(
        $('.g-track', el),
        spans,
        (x) => x.i,
        (x) => h('i', { class: `g-span g-${GANTT_GROUP[x.sp.stage] || 'text'}${VOICE_STAGES.includes(x.sp.stage) && r.id.startsWith('s') ? ' g-sub' : ''}` }),
        (bar, { sp }) => {
          const end = sp.end ?? (running ? now : sp.start + 400);
          bar.style.left = `${((sp.start / total) * 100).toFixed(2)}%`;
          bar.style.width = `${Math.max(0.5, ((end - sp.start) / total) * 100).toFixed(2)}%`;
          cls(bar, 'live', sp.end == null && running);
          cls(bar, 'err', sp.status === 'error');
          bar.title = `${sp.stage} · ${fmtMs(end - sp.start)}${sp.status === 'error' ? ' · failed' : ''}`;
        },
      );
    },
  );
  // Unitless fraction: styles.css scales it across the track area (label column excluded).
  section.style.setProperty('--now', running ? (now / total).toFixed(4) : '1');
  cls(section, 'running', running);
  setText($('.tl-label', section), `LIVE TIMELINE · 0 → ${fmtClock(total)} · every scene runs its own chain`);
}

/**
 * Render big headline counters ({k, v, label}) into a section-meta element: speed must read
 * from across the room, so frames / p50 / throughput get display-size numbers.
 */
function setCounters(el, items) {
  reconcile(
    el,
    items.filter((x) => x.v != null && x.v !== ''),
    (x) => x.k,
    () => h('span', { class: 'counter' }, h('b', {}), h('small', {})),
    (c, x) => {
      setText($('b', c), x.v);
      setText($('small', c), x.label);
    },
  );
}

/** Build the plan card; rebuilt only when the plan object changes. */
function renderPlan() {
  const run = store.run;
  const card = $('#plan-card');
  const plan = run.plan;
  const sig = plan ? `plan:${plan.campaign_name}:${plan.tagline}` : `pending:${run.status}`;
  if (card.dataset.sig === sig) return;
  card.dataset.sig = sig;
  if (!plan) {
    card.replaceChildren(
      h(
        'div',
        { class: 'plan-pending' },
        h('div', { class: 'eyebrow mono' }, '01 · CREATIVE DIRECTOR · GEMINI FLASH'),
        run.status === 'error'
          ? h('p', { class: 'err-text' }, 'The director could not produce a plan. Check the event log.')
          : [h('div', { class: 'sk-line w60' }), h('div', { class: 'sk-line w40' }), h('div', { class: 'sk-line w80' }), h('p', { class: 'muted small' }, 'Writing the brand bible, scene beats and music brief…')],
      ),
    );
    return;
  }
  const brand = plan.brand || {};
  const palette = (Array.isArray(brand.palette) ? brand.palette : []).map(safeColor).filter(Boolean);
  const music = plan.music || {};
  const ms = ui.stages.director?.lastMs;
  const fact = (k, v) => (v ? h('div', { class: 'fact' }, h('span', { class: 'fact-k' }, k), h('span', { class: 'fact-v' }, v)) : null);
  card.replaceChildren(
    h(
      'div',
      { class: 'plan-main' },
      h('div', { class: 'eyebrow mono' }, `01 · CREATIVE PLAN · GEMINI FLASH${ms ? ` · ${fmtMs(ms)}` : ''}`),
      h('h2', { class: 'plan-title grad-text' }, plan.campaign_name || 'Untitled campaign'),
      plan.tagline ? h('p', { class: 'plan-tagline' }, `“${plan.tagline}”`) : null,
      h(
        'div',
        { class: 'plan-row' },
        plan.cta ? h('span', { class: 'cta-chip' }, plan.cta) : null,
        brand.name ? h('span', { class: 'brand-chip' }, brand.name) : null,
      ),
      palette.length
        ? h(
            'div',
            { class: 'palette' },
            palette.map((c) => h('div', { class: 'swatch', style: { '--c': c }, title: c }, h('span', { class: 'mono' }, c.toUpperCase()))),
          )
        : null,
    ),
    h(
      'div',
      { class: 'plan-facts' },
      fact('Visual style', brand.visual_style),
      fact('Mood', brand.mood),
      fact('Typography', brand.typography),
      fact('Product', brand.product),
      fact('Hero', brand.hero),
      fact(
        'Music brief',
        [music.genre, music.bpm ? `${music.bpm} bpm` : '', music.key, Array.isArray(music.instruments) ? music.instruments.join(', ') : '']
          .filter(Boolean)
          .join(' · ') + (music.arc ? ` — ${music.arc}` : ''),
      ),
    ),
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// 7. Storyboard fan-out (the headline visual)
// ═══════════════════════════════════════════════════════════════════════════

function renderStoryboard() {
  const run = store.run;
  const root = $('#storyboard');
  const K = Number(run.input.variants) || 4;
  const scenes = run.scenes.length
    ? run.scenes
    : Array.from({ length: Number(run.input.n_scenes) || 4 }, (_, i) => ({ id: `_sk${i}`, _skeleton: true, title: '', variants: [], judge: [], clip: { status: 'idle', versions: [] } }));
  root.style.setProperty('--cols', String(K + 1));
  const firstRound = run.scenes.reduce((a, s) => a + s.variants.filter((v) => v.url && (v.round ?? 0) === 0).length, 0);
  const extra = run.scenes.reduce((a, s) => a + s.variants.filter((v) => v.url && (v.round ?? 0) > 0).length, 0);
  const expected = scenes.length * K;
  setCounters($('#storyboard-meta'), [
    { k: 'frames', v: `${firstRound}/${expected}`, label: `${scenes.length}×${K} keyframes` },
    { k: 'extra', v: extra ? `+${extra}` : null, label: 'repair / regen' },
    { k: 'p50', v: run.metrics.image_p50_ms ? fmtMs(run.metrics.image_p50_ms) : null, label: 'NB2 p50' },
    { k: 'ipm', v: run.metrics.images_per_min ? Number(run.metrics.images_per_min).toFixed(0) : null, label: 'images / min' },
  ]);
  reconcile(root, scenes, (s) => s.id, createSceneRow, (el, s) => updateSceneRow(el, s, run, K));
  if (firstRound > 0 && run.status === 'running' && !ui.replay) autoScrollOnce('storyboard', $('#storyboard-panel'));
}

function createSceneRow(s) {
  const regenInput = h('input', { type: 'text', maxLength: 300, placeholder: 'Optional: what should change? e.g. tighter on the product' });
  const regenForm = h(
    'form',
    { class: 'regen-form', hidden: true },
    regenInput,
    h('button', { class: 'btn grad xs', type: 'submit' }, 'Regenerate'),
    h('button', { class: 'btn ghost xs regen-cancel', type: 'button' }, 'Cancel'),
  );
  const row = h(
    'article',
    { class: 'scene-row' },
    h(
      'header',
      { class: 'scene-head' },
      h('span', { class: 'scene-no mono' }),
      h(
        'div',
        { class: 'scene-titles' },
        h('h3', { class: 'scene-title' }),
        h('div', { class: 'scene-sub' }, h('span', { class: 'beat-chip' }), h('span', { class: 'scene-mood' }), h('span', { class: 'energy' }, h('i'))),
      ),
      h('span', { class: 'scene-status mono' }),
      h('button', { class: 'btn ghost xs regen-btn', type: 'button', title: 'New NB2 round for this scene' }, '↻ regenerate'),
    ),
    regenForm,
    h('div', { class: 'lanes' }),
    h('p', { class: 'judge-note' }),
  );
  $('.regen-btn', row).addEventListener('click', () => {
    regenForm.hidden = !regenForm.hidden;
    if (!regenForm.hidden) regenInput.focus();
  });
  $('.regen-cancel', row).addEventListener('click', () => (regenForm.hidden = true));
  regenForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const sid = row.dataset.key;
    if (guardReplay()) return;
    const instruction = regenInput.value.trim();
    const ok = await act(
      'Regenerate',
      () => api(`/api/runs/${store.runId}/scenes/${encodeURIComponent(sid)}/regenerate`, { method: 'POST', json: instruction ? { instruction } : {} }),
      { button: $('button[type=submit]', regenForm) },
    );
    if (ok) {
      ui.pendingRegen[sid] = true;
      regenInput.value = '';
      regenForm.hidden = true;
      toast(`Regenerating ${sceneLabel(store.run, sid)}${instruction ? ` — “${truncate(instruction, 60)}”` : ''}`, 'info');
      markDirty();
    }
  });
  return row;
}

function updateSceneRow(row, s, run, K) {
  const i = run.scenes.indexOf(s);
  cls(row, 'skeleton', s._skeleton);
  setText($('.scene-no', row), s._skeleton ? `S${Number(s.id.slice(3)) + 1}` : `S${i + 1}`);
  setText($('.scene-title', row), s._skeleton ? 'Director is writing this beat…' : s.title || s.id);
  const beat = $('.beat-chip', row);
  setText(beat, s.beat || '');
  beat.hidden = !s.beat;
  beat.dataset.beat = s.beat || '';
  setText($('.scene-mood', row), s.mood || '');
  const en = $('.energy', row);
  en.hidden = s._skeleton;
  const bar = $('.energy i', row);
  bar.style.width = `${Math.round(clamp01(s.energy) * 100)}%`;
  bar.style.background = energyColor(s.energy);
  en.title = `energy ${Number(s.energy ?? 0).toFixed(2)}`;
  $('.regen-btn', row).hidden = s._skeleton || !winnerDecided(s);

  // Status text for the row.
  const status = $('.scene-status', row);
  const got = s.variants.filter((v) => (v.round ?? 0) === 0).length;
  let st = '';
  let busy = true;
  if (s._skeleton) st = 'planning…';
  else if (!run.anchor && !s.variants.length) st = 'waiting for anchor';
  else if (!s.judge.length && got < K) st = `painting ${got}/${K}`;
  else if (!s.judge.length) st = 'judging…';
  else if (s._pendingRepair) st = 'repairing…';
  else if (ui.pendingRegen[s.id]) st = 'regenerating…';
  else {
    busy = false;
    const sc = winnerScore(s);
    st = `♛ #${(s.winner ?? 0) + 1}${sc != null ? ` · ${fmtScore(sc)}` : ''}${s._winnerBy === 'user' ? ' · your pick' : ''}`;
  }
  setText(status, st);
  cls(status, 'busy', busy && run.status !== 'error');

  // Lanes: round 0 (anchor + K tiles) then one lane per repair/regen round.
  const rounds = [...new Set(s.variants.map((v) => v.round ?? 0))].sort((a, b) => a - b);
  if (!rounds.includes(0)) rounds.unshift(0);
  const maxRound = rounds[rounds.length - 1];
  const lastJudged = Math.max(-1, ...s.judge.map((j) => j.round ?? 0));
  const boardActive = !!ui.stages.storyboard?.active.has(s.id);
  // A new lane is anticipated (shimmer tiles) once a repair/regenerate round is announced but no frame has landed.
  const pendingKind = s._pendingRepair ? 'repair' : ui.pendingRegen[s.id] ? 'regen' : null;
  const pendingRound = pendingKind && maxRound <= lastJudged ? maxRound + 1 : null;
  if (pendingRound != null) rounds.push(pendingRound);
  const decided = winnerDecided(s);
  const latestJudge = s.judge[s.judge.length - 1];
  const leader = !decided && latestJudge ? latestJudge.winner_index : null;
  const maxIdx = Math.max(K - 1, ...s.variants.map((v) => v.idx));
  const expectedFor = (kind) => (kind === 'repair' ? REPAIR_VARIANTS : kind === 'restyle' ? 1 : K);

  const lanes = rounds.map((r) => {
    const items = [];
    const vs = s.variants.filter((v) => (v.round ?? 0) === r);
    if (r === 0) {
      items.push({ kind: 'anchor', key: 'anchor' });
      for (let idx = 0; idx < K; idx++) {
        const v = vs.find((x) => x.idx === idx);
        items.push(v ? { kind: 'variant', key: `v${v.idx}`, v } : { kind: 'ph', key: `v${idx}` });
      }
      for (const v of vs) if (v.idx >= K) items.push({ kind: 'variant', key: `v${v.idx}`, v });
      return { round: r, items };
    }
    const vk = vs.find((v) => v.kind)?.kind || (r === pendingRound ? pendingKind : ui.regenRounds[s.id]?.has(r) ? 'regen' : 'repair');
    const label = vk === 'regenerate' ? 'regen' : vk;
    items.push({ kind: 'marker', key: `m${r}`, round: r, label });
    for (const v of vs) items.push({ kind: 'variant', key: `v${v.idx}`, v });
    // Remaining shimmer slots for a round still in flight (or just announced).
    const inFlight = r === pendingRound || (r === maxRound && boardActive && r > lastJudged);
    if (inFlight) {
      const missing = Math.max(0, expectedFor(vk) - vs.length);
      for (let k = 0; k < missing; k++) items.push({ kind: 'ph', key: `v${maxIdx + 1 + k}`, repair: true });
    }
    return { round: r, items };
  });

  reconcile(
    $('.lanes', row),
    lanes,
    (l) => `r${l.round}`,
    () => h('div', { class: 'lane' }),
    (laneEl, lane) => {
      cls(laneEl, 'repair-lane', lane.round > 0);
      reconcile(
        laneEl,
        lane.items,
        (it) => it.key,
        () => h('div', { class: 'tile' }),
        (tile, it) => updateTile(tile, it, s, run, { decided, leader }),
      );
    },
  );

  // Judge rationale line.
  const note = $('.judge-note', row);
  if (latestJudge?.rationale || latestJudge?.fix_instructions) {
    // replaceChildren would stringify a null child, so build through appendKids (which skips nulls).
    note.replaceChildren();
    appendKids(note, [
      h('span', { class: 'judge-tag mono' }, `JUDGE R${latestJudge.round ?? 0}${latestJudge.latency_ms ? ` · ${fmtMs(latestJudge.latency_ms)}` : ''}`),
      h('span', {}, latestJudge.rationale || ''),
      s._pendingRepair && latestJudge.fix_instructions ? h('span', { class: 'fix' }, ` Repair: ${latestJudge.fix_instructions}`) : null,
    ]);
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

/** Patch one storyboard tile; its inner DOM is rebuilt only when its kind changes. */
function updateTile(tile, it, s, run, { decided, leader }) {
  const kind = it.kind === 'variant' && !it.v.url ? 'error' : it.kind;
  if (tile.dataset.kind !== kind) {
    const wasPh = tile.dataset.kind === 'ph';
    tile.dataset.kind = kind;
    tile.className = `tile tile-${kind}`;
    tile.replaceChildren();
    tile.onclick = null;
    if (kind === 'ph') {
      tile.append(h('div', { class: 'shimmer' }), h('span', { class: 'ph-label mono' }, it.repair ? 'repair…' : s._skeleton ? '' : 'NB2…'));
    } else if (kind === 'anchor') {
      tile.append(h('div', { class: 'shimmer' }), h('img', { alt: 'Continuity anchor frame', decoding: 'async' }), h('span', { class: 'anchor-label mono' }, 'continuity anchor'), h('span', { class: 'badge lat mono' }));
    } else if (kind === 'marker') {
      tile.append(h('span', { class: 'marker-arrow' }, '↳'), h('span', { class: 'marker-label mono' }));
    } else if (kind === 'error') {
      tile.append(h('span', { class: 'err-ico' }, '⚠'), h('span', { class: 'err-msg' }));
    } else {
      const img = h('img', { alt: '', decoding: 'async' });
      img.addEventListener('load', () => tile.classList.add('loaded'));
      tile.append(
        img,
        h('span', { class: 'badge idx mono' }),
        h('span', { class: 'badge lat mono' }),
        h('span', { class: 'badge score mono' }),
        h('span', { class: 'crown', 'aria-hidden': 'true' }, '♛'),
      );
      tile.addEventListener('click', () => onTileClick(tile));
      tile.classList.add(wasPh ? 'pop' : 'pop-soft');
    }
  }
  if (kind === 'anchor') {
    const has = !!run.anchor?.url;
    // Anchor generation failed: the pipeline continues without one, so stop shimmering.
    const failed = !has && (ui.stages.anchor?.errors || 0) > 0;
    cls(tile, 'ready', has);
    cls(tile, 'failed', failed);
    const img = $('img', tile);
    setSrc(img, run.anchor?.url);
    img.hidden = !has;
    setText($('.anchor-label', tile), failed ? 'no anchor' : 'continuity anchor');
    tile.title = failed ? 'Continuity anchor failed; scenes render without it (see event log)' : '';
    setText($('.lat', tile), has && run.anchor.latency_ms ? fmtMs(run.anchor.latency_ms) : '');
  } else if (kind === 'marker') {
    setText($('.marker-label', tile), `${it.label} R${it.round}`);
  } else if (kind === 'error') {
    setText($('.err-msg', tile), truncate(it.v.error, 80));
    tile.title = String(it.v.error || '');
  } else if (kind === 'variant') {
    const v = it.v;
    tile.dataset.idx = String(v.idx);
    tile.dataset.scene = s.id;
    setSrc($('img', tile), v.url);
    $('img', tile).alt = `${s.title || s.id} — variant ${v.idx + 1}`;
    setText($('.idx', tile), `#${v.idx + 1}${(v.round ?? 0) > 0 ? ` · ${v.kind === 'regenerate' ? 'regen' : v.kind || 'repair'}` : ''}`);
    const lat = $('.lat', tile);
    setText(lat, fmtMs(v.latency_ms));
    const ms = Number(v.latency_ms) || 0;
    lat.dataset.speed = ms < 1500 ? 'fast' : ms < 3000 ? 'mid' : 'slow';
    const scoreEl = $('.score', tile);
    setText(scoreEl, v.score != null ? fmtScore(v.score) : '');
    scoreEl.hidden = v.score == null;
    const isWinner = decided && s.winner === v.idx;
    cls(tile, 'winner', isWinner);
    cls(tile, 'leader', !decided && leader === v.idx);
    cls(tile, 'loser', decided && !isWinner && v.score != null);
    cls(tile, 'judged', v.score != null);
    tile.title = v.notes ? `Judge: ${v.notes}` : decided && !isWinner ? 'Click to pick this frame instead' : '';
  }
}

/** Clicking a non-winning tile overrides the judge (POST select) and re-renders the clip. */
async function onTileClick(tile) {
  const run = store.run;
  const sid = tile.dataset.scene;
  const idx = Number(tile.dataset.idx);
  const s = run?.scenes.find((x) => x.id === sid);
  if (!s || !winnerDecided(s) || s.winner === idx) return;
  if (guardReplay()) return;
  const prev = { winner: s.winner, by: s._winnerBy };
  s.winner = idx;
  s._winnerBy = 'user';
  markDirty();
  const ok = await act('Select winner', () => api(`/api/runs/${store.runId}/scenes/${encodeURIComponent(sid)}/select`, { method: 'POST', json: { idx } }));
  if (ok) toast(`${sceneLabel(run, sid)}: you picked #${idx + 1} — re-rendering the shot`, 'info');
  else {
    s.winner = prev.winner;
    s._winnerBy = prev.by;
    markDirty();
  }
}

function guardReplay() {
  if (ui.replay) {
    toast('Replay in progress — actions unlock when it finishes.', 'warn');
    return true;
  }
  if (!store.runId) return true;
  return false;
}

// ═══════════════════════════════════════════════════════════════════════════
// 8. Motion lab (per-scene Omni clips + conversational editing + voiceover)
// ═══════════════════════════════════════════════════════════════════════════

/**
 * One shared preview player for every VO line in the studio, so two previews never overlap.
 * `sceneKey` identifies what is playing (a scene id, or `market|scene` for localized lines).
 */
const voPreview = { audio: new Audio(), key: null };
voPreview.audio.addEventListener('ended', () => {
  voPreview.key = null;
  markDirty();
});
voPreview.audio.addEventListener('pause', markDirty);

/** Toggle playback of a narration take; returns nothing, re-renders the play buttons. */
function toggleVoPreview(key, url) {
  const a = voPreview.audio;
  if (voPreview.key === key && !a.paused) {
    a.pause();
    voPreview.key = null;
    return;
  }
  const u = safeUrl(url);
  if (!u) return;
  voPreview.key = key;
  if (a.dataset.src !== u) {
    a.dataset.src = u;
    a.src = u;
  }
  a.currentTime = 0;
  a.play?.().catch(() => toast('Could not play the narration preview.', 'warn'));
  markDirty();
}

const voPlaying = (key) => voPreview.key === key && !voPreview.audio.paused;

/** POST a re-voice request for one scene (text optional: omitted = same line, fresh take). */
async function revoice(sceneId, text, button) {
  if (guardReplay()) return;
  const body = text ? { text } : {};
  const ok = await act('Re-voice', () => api(`/api/runs/${store.runId}/scenes/${encodeURIComponent(sceneId)}/voiceover`, { method: 'POST', json: body }), { button });
  if (ok) {
    ui.pendingVoice[sceneId] = text || true;
    toast(`Re-voicing ${sceneLabel(store.run, sceneId)} with Flash TTS…`, 'info');
    markDirty();
  }
}

function renderMotion() {
  const run = store.run;
  const grid = $('#motion-grid');
  const done = run.scenes.filter((s) => s.clip.versions.length).length;
  const edits = run.scenes.reduce((a, s) => a + Math.max(0, s.clip.versions.length - 1), 0);
  setCounters($('#motion-meta'), [
    { k: 'clips', v: `${done}/${run.scenes.length}`, label: 'Omni clips' },
    { k: 'edits', v: edits ? String(edits) : null, label: 'edits' },
    { k: 'p50', v: run.metrics.video_p50_ms ? fmtMs(run.metrics.video_p50_ms) : null, label: 'Omni p50' },
  ]);
  $('#motion-panel').hidden = !run.scenes.length;
  reconcile(grid, run.scenes, (s) => s.id, createClipCard, (el, s) => updateClipCard(el, s, run));
}

let pendingEditSeq = 0;

function createClipCard(s) {
  const input = h('input', { type: 'text', maxLength: 300, placeholder: 'Direct this shot…' });
  const form = h('form', { class: 'clip-form' }, input, h('button', { class: 'btn grad xs', type: 'submit', title: 'Send edit to Omni' }, '↗'));
  const ring = progressRing(64);
  const card = h(
    'article',
    { class: 'clip-card glass' },
    h(
      'div',
      { class: 'clip-media' },
      h('img', { class: 'clip-poster', alt: '' }),
      h('video', { muted: true, loop: true, autoplay: true, playsInline: true, preload: 'auto' }),
      h('div', { class: 'clip-overlay' }, ring.el, h('span', { class: 'ring-label mono' }), h('span', { class: 'ring-sub mono' })),
      h('span', { class: 'clip-label' }),
      h('span', { class: 'badge lat mono clip-lat' }),
    ),
    h('div', { class: 'clip-bar' }, h('span', { class: 'status-chip' }), h('div', { class: 'pills' })),
    h(
      'div',
      { class: 'vo' },
      h(
        'div',
        { class: 'vo-head' },
        h('span', { class: 'vo-tag mono' }, 'VO'),
        h('span', { class: 'vo-status mono' }),
        h('button', { class: 'vo-play', type: 'button', title: 'Preview this narration take', 'aria-label': 'Preview narration' }, '▶'),
        h('button', { class: 'btn ghost xs vo-send', type: 'button', title: 'Re-voice this line with Flash TTS (Enter)' }, '↻ re-voice'),
      ),
      h('textarea', { class: 'vo-text', rows: 2, maxLength: 300, placeholder: 'Narration for this scene…', 'aria-label': 'Voiceover line' }),
    ),
    h('ol', { class: 'clip-chat' }),
    form,
    h(
      'div',
      { class: 'quick-chips' },
      CLIP_QUICK_EDITS.map((q) => h('button', { type: 'button', class: 'qchip', dataset: { q } }, q)),
    ),
  );
  card._ring = ring;
  const video = $('video', card);
  video.muted = true; // property (not just attribute) is what autoplay policies check
  const send = async (instruction) => {
    const sid = card.dataset.key;
    if (!instruction || guardReplay()) return;
    const s = store.run?.scenes.find((x) => x.id === sid);
    const baseV = s ? Math.max(0, ...s.clip.versions.map((v) => v.v)) : 0;
    const ok = await act(
      'Edit clip',
      () => api(`/api/runs/${store.runId}/scenes/${encodeURIComponent(sid)}/edit`, { method: 'POST', json: { instruction } }),
      { button: $('button[type=submit]', form) },
    );
    if (ok) {
      (ui.pendingEdits[sid] ||= []).push({ id: ++pendingEditSeq, instruction, baseV });
      input.value = '';
      markDirty();
    }
  };
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    send(input.value.trim());
  });
  $('.quick-chips', card).addEventListener('click', (ev) => {
    const b = ev.target.closest('.qchip');
    if (b) send(b.dataset.q);
  });
  $('.pills', card).addEventListener('click', (ev) => {
    const b = ev.target.closest('.pill');
    if (!b) return;
    ui.clipView[card.dataset.key] = Number(b.dataset.v);
    markDirty();
  });
  // Voiceover: the textarea is user-owned while dirty; state updates only overwrite a clean one.
  const voText = $('.vo-text', card);
  const voSend = () => revoice(card.dataset.key, voText.value.trim(), $('.vo-send', card));
  voText.addEventListener('input', () => {
    voText._dirty = voText.value !== voText._synced;
    cls(card, 'vo-dirty', voText._dirty);
  });
  voText.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      voSend();
    }
  });
  $('.vo-send', card).addEventListener('click', voSend);
  $('.vo-play', card).addEventListener('click', () => {
    const s = store.run?.scenes.find((x) => x.id === card.dataset.key);
    if (s?.voiceover.url) toggleVoPreview(s.id, withBust(s.voiceover.url, s.voiceover.v));
  });
  return card;
}

/** Patch a clip card's voiceover block from `scene.voiceover` (CONTRACT §9c). */
function updateVoiceBlock(card, s) {
  const vo = s.voiceover;
  const ta = $('.vo-text', card);
  if (ta._synced !== vo.text) {
    if (!ta._dirty || ta.value === vo.text) {
      ta.value = vo.text || '';
      ta._dirty = false;
    }
    ta._synced = vo.text;
  }
  cls(card, 'vo-dirty', !!ta._dirty);
  const pending = ui.pendingVoice[s.id];
  const busy = vo.status === 'rendering' || vo.status === 'queued' || !!pending;
  const status = $('.vo-status', card);
  status.dataset.status = vo.status === 'error' ? 'error' : busy ? 'busy' : vo.url ? 'done' : 'idle';
  setText(
    status,
    vo.status === 'error'
      ? `failed: ${truncate(vo.error, 40)}`
      : busy
        ? pending ? 're-voicing…' : 'voicing…'
        : vo.url
          ? `take ${vo.v || 1} · ${vo.duration_s ? `${Number(vo.duration_s).toFixed(1)} s · ` : ''}${fmtMs(vo.latency_ms)}`
          : vo.text ? 'waiting for Flash TTS' : 'narration arrives with the plan',
  );
  status.title = vo.error || '';
  const play = $('.vo-play', card);
  play.disabled = !vo.url;
  const playing = voPlaying(s.id);
  setText(play, playing ? '❚❚' : '▶');
  cls(play, 'on', playing);
  $('.vo-send', card).disabled = busy || ui.replay || !(ta.value.trim() || vo.text);
}

function updateClipCard(card, s, run) {
  const c = s.clip;
  const i = run.scenes.indexOf(s);
  const win = winnerDecided(s) ? winnerVariant(s) : null;
  const ver = clipVersion(s);
  const active = c.status === 'queued' || c.status === 'rendering';
  const maxV = Math.max(0, ...c.versions.map((v) => v.v));
  card.dataset.status = c.status;
  // Glow while the latest one-sentence direction is re-rendering this shot.
  const lastDir = run.directions[run.directions.length - 1];
  const directed = (lastDir?.plan?.scene_edits || []).some((se) => se.scene_id === s.id);
  cls(card, 'directing', directed && active);
  setText($('.clip-label', card), `S${i + 1} · ${s.title || s.id}`);

  const poster = $('.clip-poster', card);
  setSrc(poster, win?.url);
  poster.hidden = !win?.url;
  const video = $('video', card);
  if (setSrc(video, ver?.url) && ver?.url) {
    video.muted = true;
    video.play?.().catch(() => {});
  }
  video.hidden = !ver?.url;
  cls(card, 'has-video', !!ver?.url);
  cls(card, 'empty', !ver?.url && !win?.url);
  cls(card, 'rendering', active);
  setText($('.clip-lat', card), ver ? `${fmtMs(ver.latency_ms)}${ver.api_path ? ` · ${ver.api_path}` : ''}` : '');
  $('.clip-lat', card).hidden = !ver;

  // Status chip.
  const chip = $('.status-chip', card);
  chip.dataset.status = c.status;
  const editing = (ui.pendingEdits[s.id] || []).some((p) => !p.failed);
  const label =
    c.status === 'queued'
      ? 'queued'
      : c.status === 'rendering'
        ? ver
          ? `${editing ? 'editing' : 're-rendering'} → v${maxV + 1}`
          : 'rendering'
        : c.status === 'error'
          ? `error: ${truncate(c.error, 60)}`
          : c.status === 'done' || ver
            ? `v${ver?.v ?? '?'} ready${c.lastError ? ' · last edit failed' : ''}`
            : win
              ? 'waiting for Omni'
              : 'waiting for winner';
  setText(chip, label);
  chip.title = c.error || c.lastError || '';

  // Version pills.
  reconcile(
    $('.pills', card),
    c.versions,
    (v) => v.v,
    (v) => h('button', { type: 'button', class: 'pill mono', dataset: { v: String(v.v) } }, `v${v.v}`),
    (el, v) => {
      cls(el, 'on', ver?.v === v.v);
      el.title = [v.instruction ? `“${v.instruction}”` : 'initial render', fmtMs(v.latency_ms), v.api_path, v.fallback ? `fallback: ${v.fallback}` : '']
        .filter(Boolean)
        .join(' · ');
    },
  );

  // Chat history: instruction → version, plus local pending edits. A pending edit leaves only when
  // a newer version with its instruction lands, or ~8 s after it failed (shown in red meanwhile).
  const now = performance.now();
  const pend = (ui.pendingEdits[s.id] || []).filter(
    (p) => !c.versions.some((v) => v.v > p.baseV && v.instruction === p.instruction) && !(p.failedAt && now - p.failedAt > 8000),
  );
  ui.pendingEdits[s.id] = pend;
  const chat = [
    ...c.versions
      .filter((v) => v.instruction)
      .map((v) => ({ key: `v${v.v}`, text: v.instruction, out: `v${v.v}`, meta: [v.kind === 'direct' ? 'direct' : '', fmtMs(v.latency_ms), v.fallback || ''].filter(Boolean).join(' · ') })),
    ...pend.map((p) => ({
      key: `p${p.id}`,
      text: p.instruction,
      out: p.failed ? '✕' : '…',
      meta: p.failed ? `failed · ${truncate(p.error, 60)}` : 'Omni editing',
      pending: !p.failed,
      failed: p.failed,
    })),
  ];
  const chatEl = $('.clip-chat', card);
  chatEl.hidden = !chat.length;
  reconcile(
    chatEl,
    chat,
    (m) => m.key,
    (m) => h('li', { class: 'msg' }, h('span', { class: 'msg-text' }), h('span', { class: 'msg-out mono' }), h('span', { class: 'msg-meta mono' })),
    (el, m) => {
      cls(el, 'pending', m.pending);
      cls(el, 'failed', m.failed);
      setText($('.msg-text', el), `“${m.text}”`);
      setText($('.msg-out', el), `→ ${m.out}`);
      setText($('.msg-meta', el), m.meta);
    },
  );
  const canEdit = !!win || c.versions.length > 0;
  for (const el of $$('input, button', $('.clip-form', card)).concat($$('.qchip', card))) el.disabled = !canEdit;
  updateVoiceBlock(card, s);
}

// ═══════════════════════════════════════════════════════════════════════════
// 9. Soundtrack + final cut
// ═══════════════════════════════════════════════════════════════════════════

function renderMusic() {
  const run = store.run;
  const panel = $('#music-panel');
  if (!panel.dataset.built) buildMusicPanel(panel);
  const m = run.music;
  const vs = m.versions;
  const cur = vs.find((x) => x.v === ui.musicView) || vs[vs.length - 1] || null;
  const chip = $('.status-chip', panel);
  chip.dataset.status = m.status;
  setText(chip, m.status === 'rendering' ? `scoring${m._reason ? ` · ${m._reason}` : ''}` : m.status === 'error' ? `error: ${truncate(m.error, 50)}` : cur ? `v${cur.v} ready` : 'waiting for plan');
  const audio = $('audio', panel);
  // A new score version replaces the old one mid-listen: keep playing on the new take.
  const wasPlaying = !audio.paused && !audio.ended;
  if (setSrc(audio, cur?.url) && wasPlaying) audio.play?.().catch(() => {});
  $('.audio-wrap', panel).hidden = !cur;
  const why = $('.rescore-why', panel);
  const reason = cur && cur.v > 1 ? cur.reason : null;
  setText(why, reason ? `re-scored because: ${reason}` : '');
  why.hidden = !reason;
  $('.music-skel', panel).hidden = !!cur;
  cls($('.music-skel', panel), 'busy', m.status === 'rendering' || ui.pendingMusic);

  // Mood timeline: one segment per scene, width ∝ duration, colour by energy.
  const tl = $('.mood-timeline', panel);
  reconcile(
    tl,
    run.scenes,
    (s) => s.id,
    () => h('div', { class: 'seg' }, h('span', { class: 'seg-t mono' }), h('span', { class: 'seg-m' })),
    (el, s) => {
      el.style.flexGrow = String(Math.max(1, Number(s.duration_s) || 6));
      el.style.setProperty('--c', energyColor(s.energy));
      setText($('.seg-t', el), sceneLabel(run, s.id));
      setText($('.seg-m', el), s.mood || s.beat || '');
      el.title = `${s.title || s.id} — ${s.mood || ''} · energy ${Number(s.energy ?? 0).toFixed(2)}`;
      if (s._pulseAt != null && el._pulseAt !== s._pulseAt) {
        el._pulseAt = s._pulseAt;
        el.classList.remove('pulse');
        void el.offsetWidth; // restart the animation
        el.classList.add('pulse');
      }
    },
  );
  $('.timeline-wrap', panel).hidden = !run.scenes.length;

  // Version list.
  reconcile(
    $('.music-versions', panel),
    [...vs].reverse(),
    (v) => v.v,
    (v) => h('li', { class: 'mver', tabIndex: 0 }, h('span', { class: 'pill mono' }), h('span', { class: 'mver-reason' }), h('span', { class: 'mver-lat mono' })),
    (el, v) => {
      cls(el, 'on', cur?.v === v.v);
      setText($('.pill', el), `v${v.v}`);
      setText($('.mver-reason', el), v.reason || 'initial');
      setText($('.mver-lat', el), fmtMs(v.latency_ms));
    },
  );
  setText($('.music-prompt', panel), cur?.prompt || '—');
  $('.prompt-details', panel).hidden = !cur?.prompt;
}

function buildMusicPanel(panel) {
  panel.dataset.built = '1';
  const input = h('input', { type: 'text', maxLength: 300, placeholder: 'Re-score with a note… e.g. more tabla, warmer ending' });
  const form = h('form', { class: 'inline-form' }, input, h('button', { class: 'btn ghost sm', type: 'submit' }, '♫ Re-score'));
  const audio = h('audio', { controls: true, preload: 'auto' });
  const playhead = h('div', { class: 'playhead' });
  panel.replaceChildren(
    h('div', { class: 'section-head' }, h('h2', {}, h('span', { class: 'step-no' }, '05'), 'Soundtrack ', h('span', { class: 'muted small' }, 'Lyria 3.5 · adaptive')), h('span', { class: 'status-chip' })),
    h('div', { class: 'music-skel' }, h('div', { class: 'eq' }, Array.from({ length: 24 }, () => h('i'))), h('span', { class: 'muted small' }, 'Lyria starts scoring the moment the plan lands…')),
    h('div', { class: 'audio-wrap' }, audio),
    h('p', { class: 'rescore-why mono', hidden: true }),
    h('div', { class: 'timeline-wrap' }, h('div', { class: 'tl-label mono muted' }, 'MOOD TIMELINE'), h('div', { class: 'tl-track' }, h('div', { class: 'mood-timeline' }), playhead)),
    h('ol', { class: 'music-versions' }),
    h('details', { class: 'prompt-details' }, h('summary', {}, 'Lyria prompt'), h('pre', { class: 'music-prompt mono' })),
    form,
  );
  audio.addEventListener('timeupdate', () => {
    const p = audio.duration ? audio.currentTime / audio.duration : 0;
    playhead.style.left = `${(p * 100).toFixed(2)}%`;
    playhead.hidden = !audio.duration;
  });
  $('.music-versions', panel).addEventListener('click', (ev) => {
    const li = ev.target.closest('.mver');
    if (!li) return;
    ui.musicView = Number(li.dataset.key);
    markDirty();
    requestAnimationFrame(() => audio.play?.().catch(() => {}));
  });
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (guardReplay()) return;
    const instruction = input.value.trim();
    const ok = await act('Re-score', () => api(`/api/runs/${store.runId}/music`, { method: 'POST', json: instruction ? { instruction } : {} }), { button: $('button', form) });
    if (ok) {
      ui.pendingMusic = true;
      input.value = '';
      toast('Lyria is re-scoring…', 'info');
      markDirty();
    }
  });
}

function renderFinal() {
  const run = store.run;
  const panel = $('#final-panel');
  if (!panel.dataset.built) buildFinalPanel(panel);
  const f = run.final;
  const url = f.url ? `${f.url}${f.url.includes('?') ? '&' : '?'}v=${f.version || 0}` : null;
  const video = $('video', panel);
  setSrc(video, url);
  setCaptions(video, f.captions_url ? withBust(f.captions_url, f.version) : null);
  const has = !!url;
  if (has && ui.celebratePending) {
    ui.celebratePending = false;
    const player = $('.final-player', panel);
    player.classList.remove('celebrate');
    void player.offsetWidth;
    player.classList.add('celebrate');
    video.muted = true; // autoplay policies allow muted playback without a gesture
    video.play?.().catch(() => {});
    autoScrollOnce('final', panel);
  }
  $('.final-player', panel).hidden = !has;
  const waiting = $('.final-wait', panel);
  waiting.hidden = has;
  const chip = $('.status-chip', panel);
  chip.dataset.status = f.status;
  setText(chip, f.status === 'rendering' ? 'stitching…' : f.status === 'error' ? `error: ${truncate(f.error, 50)}` : has ? `v${f.version} · ${Number(f.duration_s || 0).toFixed(1)} s` : 'waiting');
  if (!has) {
    const clips = run.scenes.filter((s) => s.clip.versions.length).length;
    const voiced = run.scenes.filter((s) => s.voiceover.url || s.voiceover.status === 'error').length;
    setText(
      $('.wait-text', panel),
      f.status === 'rendering'
        ? 'ffmpeg is cutting clips, score and narration together…'
        : `Auto-stitches when every clip, the score and the narration are ready · clips ${clips}/${run.scenes.length || '—'} · score ${run.music.versions.length ? '✓' : '…'} · voice ${voiced}/${run.scenes.length || '—'}`,
    );
  }
  const dl = $('.dl-btn', panel);
  dl.hidden = !has;
  if (has) {
    dl.href = safeUrl(url);
    dl.download = `${(run.plan?.campaign_name || 'adloop').replace(/[^\w-]+/g, '_').slice(0, 40)}_v${f.version || 1}.mp4`;
  }
  $('.present-btn', panel).disabled = !run.plan;
  $('.restitch-btn', panel).disabled = !run.scenes.some((s) => s.clip.versions.length);
}

/**
 * Point a <video>'s single captions <track> at `url` (WebVTT from the stitcher). The track is
 * replaced (not mutated) whenever the URL changes, so a new final version gets its new cues.
 */
function setCaptions(video, url) {
  const u = safeUrl(url || '');
  if ((video.dataset.captions || '') === u) return;
  video.dataset.captions = u;
  for (const t of $$('track', video)) t.remove();
  if (!u) return;
  const track = h('track', { kind: 'captions', srclang: 'en', label: 'Narration', src: u, default: true });
  video.appendChild(track);
  track.addEventListener('load', () => {
    if (track.track) track.track.mode = 'showing';
  });
  if (track.track) track.track.mode = 'showing';
}

/** The first fresh final cut of a run: celebrate it (banner + autoplay + scroll), once. */
function onFreshFinal() {
  if (ui.celebrated) return;
  ui.celebrated = true;
  ui.celebratePending = true;
}

/** One-shot "brief → finished ad" banner with the headline numbers and a Present CTA. */
function renderCutBanner() {
  const run = store.run;
  const banner = $('#cut-banner');
  if (!ui.celebrated || banner.dataset.dismissed === run.id) {
    banner.hidden = true;
    return;
  }
  const m = metricsView(run);
  const ttf = m.time_to_final_ms ?? run._wall_ms;
  const lines = run.scenes.filter((s) => s.voiceover.url).length;
  const facts = [
    `${fmtMetric(m.images_generated, 'n')} keyframes`,
    `${run.scenes.filter((s) => s.clip.versions.length).length} Omni clips`,
    run.music.versions.length ? 'Lyria score' : null,
    lines ? `${lines} narrated lines` : null,
  ].filter(Boolean);
  const sig = `${ttf}|${facts.join('|')}`;
  banner.hidden = false;
  if (banner.dataset.sig === sig) return;
  banner.dataset.sig = sig;
  const close = h('button', { class: 'cut-banner-close', type: 'button', 'aria-label': 'Dismiss' }, '×');
  close.addEventListener('click', () => {
    banner.dataset.dismissed = run.id;
    markDirty();
  });
  const go = h('button', { class: 'btn grad', type: 'button' }, '▶ Present');
  go.addEventListener('click', () => openPresentation());
  banner.replaceChildren(
    h('div', { class: 'cut-banner-text' }, h('b', {}, ttf ? `Brief → finished ad in ${fmtMs(ttf)}` : 'Your ad is cut'), h('span', { class: 'mono' }, facts.join(' · '))),
    go,
    close,
  );
}

/**
 * Winner → Omni hand-off made visible: a clone of the freshly crowned keyframe flies from its
 * storyboard tile to the scene's clip card (FLIP-style transform). Skipped when either end is off-screen.
 */
function flyWinners() {
  const queue = ui.flights.splice(0);
  for (const sid of queue) {
    const img = $(`#storyboard .tile.winner[data-scene="${CSS.escape(sid)}"] img`);
    const card = $(`#motion-grid .clip-card[data-key="${CSS.escape(sid)}"] .clip-media`);
    if (!img?.src || !card) continue;
    const a = img.getBoundingClientRect();
    const b = card.getBoundingClientRect();
    const visible = (r) => r.width > 0 && r.bottom > 0 && r.top < window.innerHeight;
    if (!visible(a) || !visible(b)) continue;
    const flyer = h('img', { class: 'flyer', src: img.src, alt: '' });
    Object.assign(flyer.style, { left: `${a.left}px`, top: `${a.top}px`, width: `${a.width}px`, height: `${a.height}px` });
    document.body.appendChild(flyer);
    requestAnimationFrame(() => {
      flyer.style.transform = `translate(${b.left - a.left}px, ${b.top - a.top}px) scale(${b.width / a.width}, ${b.height / a.height})`;
      flyer.style.opacity = '0.25';
    });
    setTimeout(() => flyer.remove(), 700);
  }
}

function buildFinalPanel(panel) {
  panel.dataset.built = '1';
  const video = h('video', { controls: true, playsInline: true, preload: 'metadata' });
  panel.replaceChildren(
    h('div', { class: 'section-head' }, h('h2', {}, h('span', { class: 'step-no' }, '06'), 'Final cut'), h('span', { class: 'status-chip' })),
    h('div', { class: 'final-player' }, video),
    h('div', { class: 'final-wait' }, h('div', { class: 'shimmer' }), h('span', { class: 'wait-text muted small' })),
    h(
      'div',
      { class: 'final-actions' },
      h('button', { class: 'btn grad present-btn', type: 'button' }, '▶ Present'),
      h('a', { class: 'btn ghost dl-btn', href: '#', download: 'adloop.mp4' }, '⬇ Download'),
      h('button', { class: 'btn ghost restitch-btn', type: 'button', title: 'Force a re-stitch' }, '↻ Re-stitch'),
    ),
  );
  $('.present-btn', panel).addEventListener('click', () => openPresentation());
  $('.restitch-btn', panel).addEventListener('click', async (ev) => {
    if (guardReplay()) return;
    await act('Re-stitch', () => api(`/api/runs/${store.runId}/final`, { method: 'POST' }), { button: ev.currentTarget, success: 'Re-stitching the final cut…' });
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// 10. Localize panel
// ═══════════════════════════════════════════════════════════════════════════

function renderLocalize() {
  const run = store.run;
  const panel = $('#localize-panel');
  if (!panel.dataset.built) buildLocalizePanel(panel);
  panel.hidden = !run.plan;
  const chips = $('.loc-chips', panel);
  for (const b of $$('.chip', chips)) cls(b, 'on', ui.locMarkets.has(b.dataset.market));
  const go = $('.loc-go', panel);
  go.disabled = !ui.locMarkets.size || !run.scenes.some(winnerDecided);
  setText(go, ui.locMarkets.size ? `Localize ${ui.locMarkets.size} market${ui.locMarkets.size > 1 ? 's' : ''} ↗` : 'Pick markets');

  const markets = Object.keys(run.localizations);
  $('.loc-empty', panel).hidden = markets.length > 0;
  reconcile($('.loc-grid', panel), markets, (m) => m, createLocRow, (el, m) => updateLocRow(el, m, run));
}

function createLocRow(m) {
  const video = h('video', { class: 'loc-video', controls: true, playsInline: true, preload: 'metadata' });
  const row = h(
    'div',
    { class: 'loc-row' },
    h(
      'div',
      { class: 'loc-head' },
      h('div', { class: 'loc-market' }, h('b', {}, m), h('span', { class: 'loc-lang muted small' })),
      h('div', { class: 'loc-copy' }, h('span', { class: 'loc-tag' }), h('span', { class: 'loc-cta' })),
      h('span', { class: 'status-chip' }),
      h('audio', { controls: true, preload: 'none', class: 'loc-audio', title: 'Regional Lyria score' }),
    ),
    h(
      'div',
      { class: 'loc-body' },
      h('div', { class: 'loc-video-wrap' }, video, h('span', { class: 'loc-video-cap mono muted' })),
      h('div', { class: 'loc-side' }, h('div', { class: 'loc-tiles' }), h('ol', { class: 'loc-vo' })),
    ),
  );
  $('.loc-vo', row).addEventListener('click', (ev) => {
    const b = ev.target.closest('.vo-play');
    if (b?.dataset.url) toggleVoPreview(`${m}|${b.dataset.scene}`, b.dataset.url);
  });
  return row;
}

/** Patch one market row: copy, status, regional score, animatic (with captions), keyframes, VO lines. */
function updateLocRow(el, m, run) {
  const loc = run.localizations[m];
  const p = loc.plan || {};
  setText($('.loc-lang', el), [p.language, p.voice].filter(Boolean).join(' · '));
  setText($('.loc-tag', el), p.tagline ? `“${p.tagline}”` : '');
  setText($('.loc-cta', el), p.cta || '');
  const chip = $('.status-chip', el);
  const done = loc.scenes.length;
  const settled = loc.status === 'done' || loc.status === 'error';
  chip.dataset.status = loc.status === 'error' ? 'error' : loc.status === 'done' ? 'done' : 'rendering';
  setText(
    chip,
    loc.status === 'error'
      ? `error: ${truncate(loc.error, 40)}`
      : loc.status === 'done'
        ? `${done} frames${loc.voiceover.length ? ` · ${loc.voiceover.length} lines` : ''}${loc.video_url ? ' · animatic' : ''}`
        : `${loc.status || 'working'} · ${done}/${run.scenes.length}`,
  );
  const audio = $('.loc-audio', el);
  setSrc(audio, loc.music_url);
  audio.hidden = !loc.music_url;

  // Animatic: Ken Burns of the localized keyframes + regional score + localized narration.
  const video = $('.loc-video', el);
  setSrc(video, loc.video_url);
  setCaptions(video, loc.captions_url);
  const hasVideo = !!loc.video_url;
  $('.loc-video-wrap', el).hidden = !hasVideo;
  cls($('.loc-body', el), 'no-video', !hasVideo);
  setText($('.loc-video-cap', el), hasVideo ? `animatic${loc.video_duration_s ? ` · ${Number(loc.video_duration_s).toFixed(1)} s` : ''} · NB2 + Lyria + Flash TTS` : '');

  const tiles = run.scenes.map((s) => ({ s, img: loc.scenes.find((x) => x.scene_id === s.id) }));
  reconcile(
    $('.loc-tiles', el),
    tiles,
    (t) => t.s.id,
    () => h('div', { class: 'tile loc-tile' }, h('div', { class: 'shimmer' }), h('img', { alt: '' }), h('span', { class: 'badge idx mono' }), h('span', { class: 'badge lat mono' })),
    (tile, t) => {
      const has = !!t.img?.url;
      const img = $('img', tile);
      if (setSrc(img, t.img?.url) && has) tile.classList.add('pop');
      img.hidden = !has;
      $('.shimmer', tile).hidden = has || settled;
      setText($('.idx', tile), sceneLabel(run, t.s.id));
      setText($('.lat', tile), has ? fmtMs(t.img.latency_ms) : '');
    },
  );

  // Localized narration lines: the TTS takes when they land, else the plan's translated text.
  const planned = Array.isArray(p.voiceover) ? p.voiceover : [];
  const lines = run.scenes
    .map((s) => {
      const take = loc.voiceover.find((x) => x.scene_id === s.id);
      const text = take?.text || planned.find((x) => x.scene_id === s.id)?.text || '';
      return text ? { s, text, url: take?.url || null } : null;
    })
    .filter(Boolean);
  const voList = $('.loc-vo', el);
  voList.hidden = !lines.length;
  reconcile(
    voList,
    lines,
    (x) => x.s.id,
    () => h('li', {}, h('span', { class: 'scene-tag' }), h('span', { class: 'vo-line' }), h('button', { class: 'vo-play', type: 'button', 'aria-label': 'Play localized line' }, '▶')),
    (li, x) => {
      setText($('.scene-tag', li), sceneLabel(run, x.s.id));
      setText($('.vo-line', li), x.text);
      const b = $('.vo-play', li);
      b.dataset.scene = x.s.id;
      b.dataset.url = x.url || '';
      b.disabled = !x.url;
      const on = voPlaying(`${m}|${x.s.id}`);
      setText(b, on ? '❚❚' : '▶');
      cls(b, 'on', on);
    },
  );
}

function buildLocalizePanel(panel) {
  panel.dataset.built = '1';
  const chips = h(
    'div',
    { class: 'chip-set loc-chips' },
    MARKETS.map((m) => h('button', { type: 'button', class: 'chip', dataset: { market: m } }, m)),
  );
  const go = h('button', { class: 'btn grad sm loc-go', type: 'button' }, 'Localize');
  panel.replaceChildren(
    h('div', { class: 'section-head' }, h('h2', {}, h('span', { class: 'step-no' }, '07'), 'Localize ', h('span', { class: 'muted small' }, 'NB2 keyframes · Lyria regional score · Flash TTS narration · animatic')), go),
    chips,
    h('p', { class: 'loc-empty muted small' }, 'Pick markets — each one fans out in parallel: translated on-image text, culturally adapted cast & setting, same composition and product.'),
    h('div', { class: 'loc-grid' }),
  );
  chips.addEventListener('click', (ev) => {
    const b = ev.target.closest('.chip');
    if (!b) return;
    const m = b.dataset.market;
    if (ui.locMarkets.has(m)) ui.locMarkets.delete(m);
    else ui.locMarkets.add(m);
    markDirty();
  });
  go.addEventListener('click', async () => {
    if (guardReplay()) return;
    const markets = [...ui.locMarkets];
    await act('Localize', () => api(`/api/runs/${store.runId}/localize`, { method: 'POST', json: { markets } }), {
      button: go,
      success: `Localizing for ${markets.length} market${markets.length > 1 ? 's' : ''}…`,
    });
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// 11. Director bar ("Direct the whole ad in one sentence")
// ═══════════════════════════════════════════════════════════════════════════

function renderDirectorBar() {
  const run = store.run;
  const bar = $('#director-bar');
  bar.hidden = !run.plan;
  if (!run.plan) return;
  const fan = $('#dir-fanout');
  const last = run.directions[run.directions.length - 1];
  const items = [];
  if (ui.voiceSend) {
    items.push({ key: 'voice-send', kind: 'pending', text: `Sending “${truncate(ui.voiceSend.text, 60)}” in a moment · type or Esc to edit` });
  } else if (ui.pendingDirect) {
    items.push({ key: `pending:${ui.pendingDirect}`, kind: 'pending', text: `Gemini is breaking “${truncate(ui.pendingDirect, 70)}” into shot edits…` });
  } else if (last) {
    items.push({ key: `sum:${last.instruction}`, kind: 'summary', text: last.summary || last.instruction });
    const p = last.plan || {};
    for (const se of Array.isArray(p.scene_edits) ? p.scene_edits : []) {
      const s = run.scenes.find((x) => x.id === se.scene_id);
      const st = s?.clip.status;
      items.push({ key: `se:${se.scene_id}`, kind: 'edit', scene: se.scene_id, text: `${sceneLabel(run, se.scene_id)} · ${truncate(se.omni_instruction, 48)}`, busy: st === 'queued' || st === 'rendering', err: st === 'error' });
    }
    if (p.music?.rescore) items.push({ key: 'music', kind: 'edit', text: `♫ re-score · ${truncate(p.music.instruction, 40)}`, busy: run.music.status === 'rendering' });
    if (p.restyle_keyframes) items.push({ key: 'restyle', kind: 'edit', text: 'NB2 restyle keyframes', busy: (ui.stages.storyboard?.active.size || 0) > 0 });
    for (const vu of Array.isArray(p.voiceover_updates) ? p.voiceover_updates : []) {
      const vo = run.scenes.find((x) => x.id === vu.scene_id)?.voiceover;
      items.push({ key: `vo:${vu.scene_id}`, kind: 'edit', scene: vu.scene_id, text: `🎙 ${sceneLabel(run, vu.scene_id)} re-voice · ${truncate(vu.text, 36)}`, busy: vo?.status === 'rendering', err: vo?.status === 'error' });
    }
    if (p.voice_style) items.push({ key: 'vo-style', kind: 'edit', text: `🎙 tone · ${truncate(p.voice_style, 40)}`, busy: run.scenes.some((x) => x.voiceover.status === 'rendering') });
    if (run.final.status === 'rendering') items.push({ key: 'final', kind: 'edit', text: '✂ re-stitching', busy: true });
  }
  if (ui.directError && !ui.pendingDirect && !ui.voiceSend) items.push({ key: 'dir-err', kind: 'edit', text: `Direction failed: ${truncate(ui.directError, 90)}`, err: true });
  fan.hidden = !items.length;
  reconcile(
    fan,
    items,
    (x) => x.key,
    (x) => h('span', { class: `fan-item fan-${x.kind}` }),
    (el, x) => {
      setText(el, x.text);
      el.title = x.scene ? `${x.text} · click to jump to the shot` : x.text;
      if (x.scene) el.dataset.scene = x.scene;
      cls(el, 'jump', !!x.scene);
      cls(el, 'busy', x.busy || x.kind === 'pending');
      cls(el, 'err', x.err);
      cls(el, 'done', x.kind === 'edit' && !x.busy && !x.err);
    },
  );
  $('#dir-send').disabled = !!ui.pendingDirect;
}

/** Clicking a fan-out chip scrolls to the clip card it is re-rendering. */
function onFanoutClick(ev) {
  const sid = ev.target.closest('.fan-item[data-scene]')?.dataset.scene;
  const card = sid && $(`#motion-grid .clip-card[data-key="${CSS.escape(sid)}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.classList.remove('flash-card');
  void card.offsetWidth;
  card.classList.add('flash-card');
}

/**
 * A spoken direction is sent automatically after a short, cancellable countdown: voice →
 * whole-ad re-render with no extra click. Typing in the field (or Esc) cancels it.
 */
function queueSpokenDirection(text) {
  cancelSpokenDirection();
  $('#dir-input').value = text;
  ui.voiceSend = { text, timer: setTimeout(() => {
    ui.voiceSend = null;
    submitDirection(text);
  }, 1400) };
  markDirty();
}

function cancelSpokenDirection() {
  if (!ui.voiceSend) return false;
  clearTimeout(ui.voiceSend.timer);
  ui.voiceSend = null;
  markDirty();
  return true;
}

async function submitDirection(instruction) {
  if (!instruction || guardReplay()) return;
  ui.directError = null;
  const ok = await act('Direct', () => api(`/api/runs/${store.runId}/direct`, { method: 'POST', json: { instruction } }), { button: $('#dir-send') });
  if (ok) {
    ui.pendingDirect = instruction;
    $('#dir-input').value = '';
    markDirty();
    // Safety valve: never leave the bar stuck if the direction event is lost.
    setTimeout(() => {
      if (ui.pendingDirect === instruction) {
        ui.pendingDirect = null;
        markDirty();
      }
    }, 60000);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// 12. Telemetry drawer + event log
// ═══════════════════════════════════════════════════════════════════════════

/** Metrics with local fallbacks so tiles are meaningful even before the first metrics event. */
function metricsView(run) {
  const m = { ...(run?.metrics || {}) };
  if (!run) return m;
  const imgs = run.scenes.reduce((a, s) => a + s.variants.filter((v) => v.url).length, 0);
  m.images_generated ??= imgs || null;
  m.judge_calls ??= run.scenes.reduce((a, s) => a + s.judge.length, 0) || null;
  m.videos_generated ??= run.scenes.reduce((a, s) => a + s.clip.versions.length, 0) || null;
  m.music_versions ??= run.music.versions.length || null;
  m.voiceovers_generated ??= run.scenes.filter((s) => s.voiceover.url).length || null;
  m.wall_ms ??= run._wall_ms ?? null;
  // Wall time keeps counting between (throttled) metrics events while the run is live.
  if (run.status === 'running' && !ui.replay) m.wall_ms = Math.max(Number(m.wall_ms) || 0, nowT());
  return m;
}

function renderTelemetry() {
  const run = store.run;
  const m = metricsView(run);
  const tiles = $('#tele-tiles');
  reconcile(
    tiles,
    TELEMETRY_TILES,
    (t) => t[0],
    (t) => h('div', { class: 'ttile' }, h('span', { class: 'tval mono' }, '—'), h('span', { class: 'tlabel' }, t[1])),
    (el, [key, , kind]) => {
      const val = fmtMetric(m[key], kind);
      const vEl = $('.tval', el);
      if (vEl.textContent !== val) {
        vEl.textContent = val;
        if (key === 'wall_ms') return; // a live clock, not a new measurement: no bump
        el.classList.remove('bump');
        void el.offsetWidth; // restart the bump animation
        el.classList.add('bump');
      }
    },
  );
  const inf = inflight(run);
  reconcile(
    $('#inflight'),
    Object.keys(INFLIGHT_LABELS),
    (k) => k,
    (k) => h('div', { class: 'ibar', dataset: { mod: k } }, h('span', { class: 'ilabel mono' }, INFLIGHT_LABELS[k]), h('span', { class: 'itrack' }, h('i')), h('span', { class: 'icount mono' })),
    (el, k) => {
      const n = inf[k] || 0;
      $('i', el).style.width = `${Math.min(100, (n / INFLIGHT_CAPS[k]) * 100)}%`;
      setText($('.icount', el), String(n));
      cls(el, 'busy', n > 0);
    },
  );
  const paths = Object.entries(m.api_paths || {});
  reconcile(
    $('#api-paths'),
    paths.length ? paths : [['—', 'no calls yet']],
    (p) => p[0],
    () => h('div', { class: 'apath' }, h('span', { class: 'amodel' }), h('span', { class: 'aval' })),
    (el, [model, path]) => {
      setText($('.amodel', el), model);
      setText($('.aval', el), String(path));
    },
  );
  const dot = $('#sse-dot');
  dot.dataset.state = ui.sse;
  dot.title = `Event stream: ${ui.sse}`;
}

/** One-line human summary per event type for the log. */
function summarize(e) {
  const f = (x) => fmtMs(x);
  switch (e.type) {
    case 'run_started':
      return `${e.mode || ''} · ${e.input?.n_scenes ?? '?'}×${e.input?.variants ?? '?'} · ${e.input?.aspect || ''}`;
    case 'stage':
      return `${e.stage}${e.scene_id ? `/${e.scene_id}` : ''}${e.market ? `/${e.market}` : ''} ${e.status}${e.ms != null ? ` ${f(e.ms)}` : ''}${e.detail ? ` · ${e.detail}` : ''}`;
    case 'plan':
      return `“${e.plan?.campaign_name || ''}” · ${e.plan?.scenes?.length ?? 0} scenes`;
    case 'anchor':
      return f(e.latency_ms);
    case 'variant':
      return `${e.scene_id}#${e.idx} r${e.round} ${f(e.latency_ms)} ${e.api_path || ''}`;
    case 'variant_error':
      return `${e.scene_id}#${e.idx} ${e.error || ''}`;
    case 'judge': {
      const best = Math.max(0, ...(e.scores || []).map((s) => Number(s.overall) || 0));
      return `${e.scene_id} r${e.round} → #${(e.winner_idx ?? 0) + 1} (${best.toFixed(1)}) ${f(e.latency_ms)}`;
    }
    case 'winner':
      return `${e.scene_id} → #${(e.idx ?? 0) + 1} by ${e.by || 'judge'}`;
    case 'clip_status':
      return `${e.scene_id} ${e.status}${e.elapsed_ms != null ? ` ${f(e.elapsed_ms)}` : ''}${e.error ? ` · ${e.error}` : ''}`;
    case 'clip':
      return `${e.scene_id} v${e.v} ${f(e.latency_ms)} ${e.api_path || ''}${e.fallback ? ` (${e.fallback})` : ''}`;
    case 'scene_update':
      return `${e.scene_id} → ${e.mood || ''} ${e.energy != null ? Number(e.energy).toFixed(2) : ''}`;
    case 'music_status':
      return `${e.status}${e.reason ? ` · ${e.reason}` : ''}${e.error ? ` · ${e.error}` : ''}`;
    case 'music':
      return `v${e.v} ${f(e.latency_ms)} · ${e.reason || ''}`;
    case 'final_status':
      return `${e.status}${e.error ? ` · ${e.error}` : ''}`;
    case 'final':
      return `v${e.version} ${Number(e.duration_s || 0).toFixed(1)} s`;
    case 'direction':
      return `“${truncate(e.instruction, 50)}” → ${truncate(e.summary, 60)}`;
    case 'localize_status':
      return `${e.market} ${e.status}${e.error ? ` · ${e.error}` : ''}`;
    case 'localize_plan':
      return `${e.market} · ${e.plan?.language || ''}`;
    case 'localize_image':
      return `${e.market} ${e.scene_id} ${f(e.latency_ms)}`;
    case 'localize_music':
      return `${e.market} ${f(e.latency_ms)}`;
    case 'localize_voiceover':
      return `${e.market} ${e.scene_id} “${truncate(e.text, 40)}” ${f(e.latency_ms)}`;
    case 'localize_video':
      return `${e.market} animatic ${Number(e.duration_s || 0).toFixed(1)} s`;
    case 'voiceover_status':
      return `${e.scene_id} ${e.status}${e.error ? ` · ${e.error}` : ''}`;
    case 'voiceover':
      return `${e.scene_id} take ${e.v ?? 1} “${truncate(e.text, 44)}” ${f(e.latency_ms)}`;
    case 'log':
      return `[${e.level || 'info'}] ${e.msg || ''}`;
    case 'error':
      return `${e.stage || ''} · ${e.msg || ''}`;
    case 'run_done':
      return `first cut in ${f(e.wall_ms)}`;
    case 'transcript':
      return truncate(e.text, 80);
    default:
      return '';
  }
}

function logClass(e) {
  if (e.type === 'error' || e.type === 'variant_error' || e.status === 'error' || e.level === 'error') return 'lv-error';
  if (e.level === 'warn') return 'lv-warn';
  if (e.type.startsWith('variant') || e.type === 'anchor' || e.type.startsWith('localize')) return 'lv-image';
  if (e.type.startsWith('clip')) return 'lv-video';
  if (e.type.startsWith('music')) return 'lv-music';
  if (e.type.startsWith('voiceover')) return 'lv-voice';
  if (e.type === 'judge' || e.type === 'winner' || e.type === 'plan' || e.type === 'direction') return 'lv-text';
  if (e.type.startsWith('final') || e.type === 'run_done') return 'lv-final';
  return 'lv-dim';
}

function appendLog(e) {
  const log = $('#event-log');
  if (!log) return;
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.appendChild(
    h(
      'div',
      { class: `lrow ${logClass(e)}` },
      h('span', { class: 'lt' }, `+${((Number(e.t) || 0) / 1000).toFixed(1)}`),
      h('span', { class: 'ltype' }, e.type),
      h('span', { class: 'lmsg' }, summarize(e)),
    ),
  );
  while (log.children.length > LOG_MAX) log.firstChild.remove();
  if (nearBottom) log.scrollTop = log.scrollHeight;
}

// ═══════════════════════════════════════════════════════════════════════════
// Live ticker: clocks, rail timers, clip progress rings (cheap, text-only updates)
// ═══════════════════════════════════════════════════════════════════════════

function tick() {
  const run = store.run;
  if (!run) return;
  const clock = $('#run-clock');
  const t = run.status === 'running' ? nowT() : (run._wall_ms ?? run.metrics.wall_ms ?? run.metrics.time_to_final_ms ?? nowT());
  setText(clock, fmtClock(t));
  cls(clock, 'running', run.status === 'running');
  // Active rail nodes' elapsed.
  for (const el of $$('#rail-nodes .rail-node[data-status="active"]')) {
    const node = RAIL.find((n) => n.key === el.dataset.key);
    if (!node) continue;
    const st = railNodeState(run, node);
    if (st.elapsed != null) setText($('.rail-elapsed', el), fmtMs(st.elapsed));
  }
  // Clip progress rings.
  const tau = run.mode === 'live' ? 45000 : 7000;
  for (const card of $$('#motion-grid .clip-card')) {
    const sid = card.dataset.key;
    const s = run.scenes.find((x) => x.id === sid);
    if (!s) continue;
    const active = s.clip.status === 'queued' || s.clip.status === 'rendering';
    if (!active) continue;
    const ms = ui.clipTimers[sid] ? currentClipElapsed(sid) : Number(s.clip.elapsed_ms) || 0;
    const p = s.clip.status === 'queued' ? 0.04 : Math.min(0.96, 1 - Math.exp(-ms / tau));
    card._ring?.set(p);
    setText($('.ring-label', card), `${(ms / 1000).toFixed(1)}s`);
    setText($('.ring-sub', card), s.clip.status === 'queued' ? 'queued' : s.clip.versions.length ? 'Omni edit' : 'Omni i2v');
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// 13. Brief panel + launch
// ═══════════════════════════════════════════════════════════════════════════

const brief = { aspect: '16:9', n_scenes: 4, variants: 4, markets: new Set(), productFile: null, productUrl: null };

function initBriefPanel() {
  const text = $('#brief-text');
  const count = $('#brief-count');
  const updateCount = () => setText(count, `${text.value.length}`);
  text.addEventListener('input', updateCount);

  // Sample briefs.
  const samples = $('#sample-chips');
  samples.replaceChildren(...SAMPLE_BRIEFS.map((s, i) => h('button', { type: 'button', class: 'chip sample', dataset: { i: String(i) } }, `✦ ${s.label}`)));
  samples.addEventListener('click', (ev) => {
    const b = ev.target.closest('.sample');
    if (!b) return;
    const s = SAMPLE_BRIEFS[Number(b.dataset.i)];
    text.value = s.brief;
    $('#brand-name').value = s.brand;
    updateCount();
    text.classList.remove('flash');
    void text.offsetWidth;
    text.classList.add('flash');
  });

  // Aspect toggle.
  $('#aspect-toggle').addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-aspect]');
    if (!b) return;
    brief.aspect = b.dataset.aspect;
    for (const x of $$('#aspect-toggle button')) {
      cls(x, 'on', x === b);
      x.setAttribute('aria-checked', String(x === b));
    }
    updateFanoutHint();
  });

  // Steppers.
  for (const st of $$('.stepper')) {
    st.addEventListener('click', (ev) => {
      const b = ev.target.closest('button[data-step]');
      if (!b) return;
      const min = Number(st.dataset.min);
      const max = Number(st.dataset.max);
      const v = Math.max(min, Math.min(max, Number(st.dataset.value) + Number(b.dataset.step)));
      st.dataset.value = String(v);
      setText($('output', st), String(v));
      brief[st.dataset.stepper] = v;
      updateFanoutHint();
    });
  }

  // Market chips.
  const mk = $('#brief-markets');
  mk.replaceChildren(...MARKETS.map((m) => h('button', { type: 'button', class: 'chip', dataset: { market: m } }, m)));
  mk.addEventListener('click', (ev) => {
    const b = ev.target.closest('.chip');
    if (!b) return;
    const m = b.dataset.market;
    if (brief.markets.has(m)) brief.markets.delete(m);
    else brief.markets.add(m);
    cls(b, 'on', brief.markets.has(m));
  });

  initDropzone();
  $('#launch-btn').addEventListener('click', launch);
  text.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) {
      ev.preventDefault();
      launch();
    }
  });
  updateFanoutHint();
}

function updateFanoutHint() {
  const n = brief.n_scenes;
  const k = brief.variants;
  setText($('#fanout-hint'), `${n} × ${k} = ${n * k} parallel keyframes · ${n} Omni clips · ${brief.aspect}`);
}

function initDropzone() {
  const dz = $('#dropzone');
  const input = $('#product-file');
  const pick = (file) => {
    if (!file) return;
    if (!/^image\/(png|jpe?g|webp)$/i.test(file.type)) return toast('Product photo must be PNG, JPEG or WebP.', 'warn');
    if (file.size > 10 * 1024 * 1024) return toast('Product photo must be under 10 MB.', 'warn');
    if (brief.productUrl) URL.revokeObjectURL(brief.productUrl);
    brief.productFile = file;
    brief.productUrl = URL.createObjectURL(file);
    $('.dz-preview img', dz).src = brief.productUrl;
    setText($('.dz-name', dz), truncate(file.name, 28));
    $('.dz-preview', dz).hidden = false;
    $('.dz-empty', dz).hidden = true;
    dz.classList.add('filled');
  };
  const clear = () => {
    if (brief.productUrl) URL.revokeObjectURL(brief.productUrl);
    brief.productFile = null;
    brief.productUrl = null;
    input.value = '';
    $('.dz-preview', dz).hidden = true;
    $('.dz-empty', dz).hidden = false;
    dz.classList.remove('filled');
  };
  dz.addEventListener('click', (ev) => {
    if (ev.target.closest('.dz-clear')) {
      ev.stopPropagation();
      clear();
      return;
    }
    input.click();
  });
  dz.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      input.click();
    }
  });
  input.addEventListener('change', () => pick(input.files?.[0]));
  for (const t of ['dragenter', 'dragover']) {
    dz.addEventListener(t, (ev) => {
      ev.preventDefault();
      dz.classList.add('drag');
    });
  }
  for (const t of ['dragleave', 'drop']) dz.addEventListener(t, () => dz.classList.remove('drag'));
  dz.addEventListener('drop', (ev) => {
    ev.preventDefault();
    pick(ev.dataTransfer?.files?.[0]);
  });
}

let launching = false;

/** POST /api/runs with the brief, then open the run with an instant skeleton shell. */
async function launch() {
  if (launching) return;
  const text = $('#brief-text').value.trim();
  if (text.length < 8) {
    toast('Write (or speak) a brief first — a sentence is enough.', 'warn');
    $('#brief-text').focus();
    return;
  }
  const btn = $('#launch-btn');
  setLaunchBusy(btn, true);
  const input = {
    brief: text,
    brand: $('#brand-name').value.trim(),
    aspect: brief.aspect,
    n_scenes: brief.n_scenes,
    variants: brief.variants,
    markets: [...brief.markets],
    has_product_image: !!brief.productFile,
  };
  const form = new FormData();
  form.append('brief', input.brief);
  form.append('brand', input.brand);
  form.append('aspect', input.aspect);
  form.append('n_scenes', String(input.n_scenes));
  form.append('variants', String(input.variants));
  form.append('markets', input.markets.join(','));
  if (brief.productFile) form.append('product_image', brief.productFile, brief.productFile.name);
  try {
    const res = await api('/api/runs', { method: 'POST', form });
    if (!res?.run_id) throw new Error('server did not return a run id');
    ui.locMarkets = new Set(input.markets);
    await openRun(res.run_id, { shell: emptyRun(res.run_id, input, store.run?.mode || health.mode || 'mock') });
    $('#workspace').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    toast(`Launch failed: ${err.message}`, 'error');
  } finally {
    setLaunchBusy(btn, false);
  }
}

/** Busy state for the launch button: spinner + "Launching…", disabled and announced to AT. */
function setLaunchBusy(btn, busy) {
  launching = busy;
  btn.disabled = busy;
  btn.setAttribute('aria-busy', String(busy));
  cls(btn, 'busy', busy);
  setText($('.launch-label', btn), busy ? 'Launching…' : 'Launch loop');
}

// ═══════════════════════════════════════════════════════════════════════════
// 14. Voice capture (MediaRecorder → /api/transcribe)
// ═══════════════════════════════════════════════════════════════════════════

/** Pick the best-supported recording container: webm/opus, then mp4, then ogg. */
function pickAudioMime() {
  if (!window.MediaRecorder?.isTypeSupported) return '';
  return ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus', 'audio/ogg'].find((t) => MediaRecorder.isTypeSupported(t)) || '';
}

/**
 * Toggle-to-record voice capture bound to a mic button + meter element. Shows a
 * recording timer and a live level meter, then uploads the clip for transcription
 * and hands the text to `onText`. All failures surface as toasts.
 */
class VoiceCapture {
  constructor({ button, meter, onText, maxSeconds = 90 }) {
    this.button = button;
    this.meter = meter;
    this.onText = onText;
    this.maxSeconds = maxSeconds;
    this.rec = null;
    button.addEventListener('click', () => (this.rec ? this.stop() : this.start()));
  }

  async start() {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      toast('Voice capture is not supported in this browser.', 'warn');
      return;
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch (err) {
      toast(`Microphone unavailable: ${err?.message || 'permission denied'}`, 'error');
      return;
    }
    this.mime = pickAudioMime();
    try {
      this.rec = new MediaRecorder(this.stream, this.mime ? { mimeType: this.mime } : undefined);
    } catch (err) {
      this.cleanup();
      toast(`Recorder failed: ${err?.message || err}`, 'error');
      return;
    }
    this.chunks = [];
    this.rec.ondataavailable = (ev) => ev.data?.size && this.chunks.push(ev.data);
    this.rec.onstop = () => this.finish();
    this.rec.start(250);
    this.t0 = performance.now();
    this.button.classList.add('recording');
    this.meter.hidden = false;
    this.startMeter();
    this.timer = setInterval(() => {
      const s = (performance.now() - this.t0) / 1000;
      setText($('.rec-time', this.meter), `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`);
      if (s >= this.maxSeconds) this.stop();
    }, 200);
  }

  /** Level meter from an AnalyserNode (RMS of the time-domain signal). */
  startMeter() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC();
      const src = this.ctx.createMediaStreamSource(this.stream);
      const an = this.ctx.createAnalyser();
      an.fftSize = 512;
      src.connect(an);
      const buf = new Uint8Array(an.fftSize);
      const bars = $$('.rec-bars i', this.meter);
      const hist = new Array(bars.length).fill(0);
      const loop = () => {
        if (!this.rec) return;
        an.getByteTimeDomainData(buf);
        let sum = 0;
        for (const b of buf) sum += ((b - 128) / 128) ** 2;
        const rms = Math.min(1, Math.sqrt(sum / buf.length) * 4);
        hist.push(rms);
        hist.shift();
        bars.forEach((el, i) => (el.style.transform = `scaleY(${0.15 + hist[i] * 0.85})`));
        this.raf = requestAnimationFrame(loop);
      };
      loop();
    } catch {
      /* meter is cosmetic */
    }
  }

  stop() {
    try {
      if (this.rec?.state !== 'inactive') this.rec?.stop();
    } catch {
      this.cleanup();
    }
  }

  cleanup() {
    clearInterval(this.timer);
    cancelAnimationFrame(this.raf);
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close?.().catch(() => {});
    this.ctx = null;
    this.button.classList.remove('recording');
    this.meter.hidden = true;
    setText($('.rec-time', this.meter), '0:00');
  }

  async finish() {
    const type = this.rec?.mimeType || this.mime || 'audio/webm';
    this.rec = null;
    this.cleanup();
    const blob = new Blob(this.chunks || [], { type });
    if (blob.size < 1200) {
      toast('That was too short — hold the mic a little longer.', 'warn');
      return;
    }
    const ext = type.includes('mp4') ? 'mp4' : type.includes('ogg') ? 'ogg' : 'webm';
    const form = new FormData();
    form.append('audio', blob, `voice.${ext}`);
    this.button.classList.add('busy');
    const label = $('.mic-label', this.button);
    const prev = label.textContent;
    label.textContent = '…';
    try {
      const res = await api('/api/transcribe', { method: 'POST', form });
      const text = (res?.text || '').trim();
      if (!text) toast('No speech detected.', 'warn');
      else {
        this.onText(text);
        toast(`Transcribed in ${fmtMs(res.latency_ms)} · ${res.model || 'transcribe'}`, 'ok');
      }
    } catch (err) {
      toast(`Transcription failed: ${err.message}`, 'error');
    } finally {
      this.button.classList.remove('busy');
      label.textContent = prev;
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// 15. Presentation mode (fullscreen overlay, ←/→/Esc)
// ═══════════════════════════════════════════════════════════════════════════

/**
 * Presenter state. `slides` are specs `{key, sig, kind, el, vo, hold}`; `vo` is a narration URL
 * played on enter, `hold` the dwell time for slides without media. Auto-play advances when the
 * slide's narration (or the film) ends, plus SLIDE_GAP_MS.
 */
const present = { open: false, idx: 0, slides: [], auto: true, audio: new Audio(), timer: null, enteredAt: 0, raf: 0, wasFullscreen: false };

const BEAT_NAMES = { hook: 'Hook', build: 'Build', reveal: 'Reveal', cta: 'Call to action' };

/**
 * Slide specs from the current state: title → one slide per scene (clip + narration) → film →
 * how it was made → one animatic slide per localized market. `sig` changes when a slide's
 * content does, so `refreshPresentation` can rebuild just that slide while presenting.
 */
function slideSpecs(run) {
  const plan = run.plan || {};
  const specs = [{ key: 'title', kind: 'title', sig: `${plan.campaign_name}|${plan.tagline}`, hold: SLIDE_TIMED_MS, build: () => titleSlide(run) }];
  run.scenes.forEach((s, i) => {
    const clip = s.clip.versions[s.clip.versions.length - 1];
    const vo = s.voiceover.url ? withBust(s.voiceover.url, s.voiceover.v) : null;
    specs.push({
      key: `scene:${s.id}`,
      kind: 'scene',
      sig: `${clip?.url}|${vo}|${s.voiceover.text}|${s.winner}`,
      vo,
      hold: SLIDE_TIMED_MS,
      build: () => sceneSlide(run, s, i, clip),
    });
  });
  const f = run.final;
  specs.push({ key: 'film', kind: 'film', sig: `${f.url}|${f.version}|${f.captions_url}`, hold: SLIDE_TIMED_MS, build: () => filmSlide(run) });
  // Campaign kit slide (static/posters.js plugin); skipped when the plugin isn't loaded or has no winners yet.
  const kitItems = ((window.AdLoopPosters?.getState?.() || run.posters || {}).items || []).filter((it) => it.winner != null);
  if (kitItems.length && window.AdLoopPosters?.renderSlide) {
    specs.push({
      key: 'kit', kind: 'kit', hold: SLIDE_TIMED_MS,
      sig: `kit|${kitItems.map((it) => `${it.format}:${it.winner}`).join(',')}`,
      build: () => { const el = h('section', { class: 'slide slide-kit' }); window.AdLoopPosters.renderSlide(el); return el; },
    });
  }
  specs.push({ key: 'stats', kind: 'stats', sig: 'stats', hold: SLIDE_TIMED_MS + 2000, build: () => statsSlide(run) });
  for (const [market, loc] of Object.entries(run.localizations)) {
    if (!loc.video_url && !loc.scenes.some((x) => x.url)) continue;
    specs.push({ key: `loc:${market}`, kind: 'loc', sig: `${loc.video_url}|${loc.scenes.length}|${loc.plan?.tagline}`, hold: SLIDE_TIMED_MS, build: () => locSlide(run, market, loc) });
  }
  return specs;
}

function titleSlide(run) {
  const plan = run.plan || {};
  const brand = plan.brand || {};
  const palette = (brand.palette || []).map(safeColor).filter(Boolean);
  return h(
    'section',
    { class: 'slide slide-title', style: { '--p0': palette[0] || '#7c5cff', '--p1': palette[1] || '#ff4fd8', '--p2': palette[2] || '#ffb547' } },
    h('div', { class: 'eyebrow mono' }, `${brand.name || run.input.brand || 'AdLoop'} · ${run.scenes.length} scenes · ${run.input.aspect}`),
    h('h1', { class: 'grad-text' }, plan.campaign_name || 'Untitled campaign'),
    plan.tagline ? h('p', { class: 'slide-tagline' }, `“${plan.tagline}”`) : null,
    plan.cta ? h('span', { class: 'cta-chip big' }, plan.cta) : null,
    h('div', { class: 'palette big' }, palette.map((c) => h('div', { class: 'swatch', style: { '--c': c } }, h('span', { class: 'mono' }, c.toUpperCase())))),
  );
}

/** Full-bleed scene: its clip looping muted (or the keyframe with a slow push), label, judge score and a big narrated caption. */
function sceneSlide(run, s, i, clip) {
  const win = winnerVariant(s);
  const still = safeUrl(win?.url || s.variants.find((v) => v.url)?.url || '');
  const media = clip?.url
    ? h('video', { class: 'scene-clip', src: safeUrl(clip.url), muted: true, loop: true, playsInline: true, preload: 'auto', poster: still || null })
    : still
      ? h('img', { class: 'scene-clip still', src: still, alt: '' })
      : h('div', { class: 'scene-clip shimmer' });
  const score = winnerScore(s);
  const words = String(s.voiceover.text || '').split(/\s+/).filter(Boolean);
  const beat = BEAT_NAMES[s.beat] || s.beat || '';
  return h(
    'section',
    { class: `slide slide-scene${isPortrait(run) ? ' portrait' : ''}` },
    still ? h('img', { class: 'scene-backdrop', src: still, alt: '' }) : null,
    media,
    h('div', { class: 'scene-shade' }),
    h(
      'div',
      { class: 'scene-top' },
      h('span', { class: 'scene-label mono' }, `Scene ${i + 1}${beat ? ` · ${beat}` : ''}`),
      score != null ? h('span', { class: 'scene-score mono', title: 'Flash vision-judge score of the winning keyframe' }, `♛ judge ${fmtScore(score)}`) : null,
    ),
    h(
      'div',
      { class: 'scene-bottom' },
      h('div', { class: 'scene-kicker' }, s.title || ''),
      words.length
        ? h('p', { class: 'scene-caption' }, words.map((w) => h('span', { class: 'w' }, `${w} `)))
        : h('p', { class: 'scene-caption muted-cap' }, s.mood || ''),
    ),
  );
}

function filmSlide(run) {
  const f = run.final;
  const filmUrl = f.url ? withBust(f.url, f.version || 0) : null;
  let body;
  if (filmUrl) {
    body = h('video', { class: `film ${isPortrait(run) ? 'portrait' : ''}`, src: safeUrl(filmUrl), controls: true, playsInline: true, preload: 'auto' });
    setCaptions(body, f.captions_url ? withBust(f.captions_url, f.version) : null);
  } else body = h('div', { class: 'film-wait' }, h('div', { class: 'shimmer' }), h('p', {}, 'The final cut is still stitching…'));
  return h(
    'section',
    { class: 'slide slide-film' },
    body,
    h('p', { class: 'film-cap mono muted' }, filmUrl ? `Final cut v${f.version} · ${Number(f.duration_s || 0).toFixed(1)} s · Omni Flash clips · Lyria 3.5 score · Flash TTS narration` : ''),
  );
}

function statsSlide(run) {
  const m = metricsView(run);
  const stat = (v, label) => h('div', { class: 'stat' }, h('b', { class: 'grad-text' }, v), h('span', {}, label));
  return h(
    'section',
    { class: 'slide slide-stats' },
    h('h2', {}, 'How it was made'),
    h(
      'div',
      { class: 'stat-grid' },
      stat(fmtMetric(m.images_generated, 'n'), 'NB2 keyframes generated'),
      stat(fmtMetric(m.image_p50_ms, 'ms'), 'p50 Nano Banana 2 Lite latency'),
      stat(fmtMetric(m.time_to_first_image_ms, 'ms'), 'time to first image'),
      stat(`${fmtMetric(m.judge_calls, 'n')} / ${fmtMetric(m.repair_rounds ?? 0, 'n')}`, 'judge calls / self-repairs'),
      stat(`${fmtMetric(m.videos_generated, 'n')} / ${fmtMetric(m.video_edits ?? 0, 'n')}`, 'Omni clips / edits'),
      stat(fmtMetric(m.music_versions, 'n'), 'Lyria score versions'),
      stat(`${fmtMetric(m.voiceovers_generated, 'n')}${m.tts_p50_ms ? ` · ${fmtMs(m.tts_p50_ms)}` : ''}`, 'narrated lines · TTS p50'),
      stat(fmtMetric(m.time_to_final_ms ?? m.wall_ms, 'ms'), 'brief → first final cut'),
    ),
    h('p', { class: 'muted mono small' }, `pipelined, not barriered · ${run.mode === 'live' ? 'live models' : 'mock mode'}`),
  );
}

/** A localized market: its narrated animatic when rendered, else its keyframes. */
function locSlide(run, market, loc) {
  const p = loc.plan || {};
  let media;
  if (loc.video_url) {
    media = h('video', { class: `film loc-film ${isPortrait(run) ? 'portrait' : ''}`, src: safeUrl(loc.video_url), controls: true, playsInline: true, preload: 'auto' });
    setCaptions(media, loc.captions_url);
  } else {
    media = h(
      'div',
      { class: `board-grid ${isPortrait(run) ? 'portrait' : ''}` },
      loc.scenes.filter((x) => x.url).map((x) => h('div', { class: `board-img ${isPortrait(run) ? 'portrait' : ''}` }, h('img', { src: safeUrl(x.url), alt: '' }))),
    );
  }
  return h(
    'section',
    { class: 'slide slide-loc' },
    h('h2', {}, market, p.language ? h('span', { class: 'muted' }, ` · ${p.language}`) : null),
    media,
    p.tagline ? h('p', { class: 'slide-tagline loc-tagline' }, `“${p.tagline}”`) : null,
  );
}

function openPresentation() {
  const run = store.run;
  if (!run?.plan) return toast('Nothing to present yet — wait for the plan.', 'warn');
  // Only the presenter makes sound: silence every studio player first.
  for (const m of $$('#final-panel video, #music-panel audio, .loc-audio, .loc-video')) m.pause();
  voPreview.audio.pause();
  present.slides = slideSpecs(run).map((sp) => ({ ...sp, el: sp.build() }));
  present.open = true;
  const overlay = $('#present');
  overlay.hidden = false;
  mountSlides();
  overlay.requestFullscreen?.().catch(() => {});
  showSlide(0);
  present.raf = requestAnimationFrame(presentLoop);
}

/** (Re)mount slide elements and navigation dots. */
function mountSlides() {
  $('#present-stage').replaceChildren(...present.slides.map((sp) => sp.el));
  $('#present-dots').replaceChildren(
    ...present.slides.map((sp, i) => h('button', { type: 'button', class: `dot dot-${sp.kind}`, dataset: { i: String(i) }, 'aria-label': `Slide ${i + 1}` })),
  );
}

/** While presenting, rebuild only the slides whose content changed (a new final version, a VO take landing…). */
function refreshPresentation() {
  if (!present.open || !store.run) return;
  const specs = slideSpecs(store.run);
  const cur = present.slides[present.idx]?.key;
  const sameShape = specs.length === present.slides.length && specs.every((sp, i) => sp.key === present.slides[i].key);
  if (!sameShape) {
    present.slides = specs.map((sp) => present.slides.find((o) => o.key === sp.key && o.sig === sp.sig) || { ...sp, el: sp.build() });
    mountSlides();
    const i = present.slides.findIndex((sp) => sp.key === cur);
    showSlide(i >= 0 ? i : present.idx, { restart: false });
    return;
  }
  specs.forEach((sp, i) => {
    const old = present.slides[i];
    if (old.sig === sp.sig) return;
    const next = { ...sp, el: sp.build() };
    old.el.replaceWith(next.el);
    present.slides[i] = next;
    // Re-enter the visible slide only if it carries media (its film / narration just changed).
    if (i === present.idx) showSlide(i, { restart: sp.kind !== 'stats' && sp.kind !== 'title' });
    else cls(next.el, 'before', i < present.idx);
  });
}

function closePresentation() {
  if (!present.open) return;
  present.open = false;
  stopSlideMedia();
  cancelAnimationFrame(present.raf);
  $('#present').hidden = true;
  $('#present-stage').replaceChildren();
  present.slides = [];
  if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
}

function stopSlideMedia() {
  clearTimeout(present.timer);
  present.timer = null;
  present.audio.pause();
  present.audio.onended = present.audio.onerror = null;
  for (const v of $$('#present-stage video')) {
    v.pause();
    v.onended = null;
  }
}

/** Schedule the auto-advance for the current slide (no-op on the last slide or with auto-play off). */
function scheduleAdvance(ms) {
  clearTimeout(present.timer);
  present.timer = null;
  if (!present.auto || present.idx >= present.slides.length - 1) return;
  present.timer = setTimeout(() => showSlide(present.idx + 1), ms);
}

/**
 * Enter slide `i`: stop the previous slide's media, start this one's (clip loop, narration,
 * film with sound), and arm auto-advance on narration / film end (timed fallback without media).
 */
function showSlide(i, { restart = true } = {}) {
  const n = present.slides.length;
  if (!n) return;
  present.idx = Math.max(0, Math.min(n - 1, i));
  const spec = present.slides[present.idx];
  present.slides.forEach((sp, k) => {
    cls(sp.el, 'active', k === present.idx);
    cls(sp.el, 'before', k < present.idx);
  });
  $$('#present-dots .dot').forEach((d, k) => cls(d, 'on', k === present.idx));
  setText($('#present-count'), `${present.idx + 1} / ${n}`);
  if (!restart) return;
  stopSlideMedia();
  present.enteredAt = performance.now();
  present.media = null;
  for (const w of $$('.scene-caption .w', spec.el)) w.classList.remove('lit');

  const clip = $('video.scene-clip', spec.el);
  if (clip) {
    clip.muted = true;
    clip.currentTime = 0;
    clip.play?.().catch(() => {});
  }
  if (spec.kind === 'scene') {
    if (!spec.vo) return scheduleAdvance(spec.hold);
    const a = present.audio;
    a.src = spec.vo;
    a.currentTime = 0;
    present.media = a;
    a.onended = () => scheduleAdvance(SLIDE_GAP_MS);
    a.onerror = () => scheduleAdvance(spec.hold);
    // A short lead-in so the slide transition lands before the first word.
    present.timer = setTimeout(() => a.play().catch(() => scheduleAdvance(spec.hold)), 350);
    return;
  }
  const film = $('video.film', spec.el);
  if (film) {
    present.media = film;
    film.muted = false;
    film.currentTime = 0;
    film.onended = () => scheduleAdvance(SLIDE_GAP_MS);
    film.play?.().catch(() => {
      // Autoplay with sound refused: fall back to muted so the film still moves.
      film.muted = true;
      film.play?.().catch(() => {});
    });
    return;
  }
  scheduleAdvance(spec.hold);
}

/** Per-frame presenter UI: the progress bar and the word-by-word caption highlight. */
function presentLoop() {
  if (!present.open) return;
  const spec = present.slides[present.idx];
  const m = present.media;
  let p;
  if (m) p = Number.isFinite(m.duration) && m.duration > 0 ? m.currentTime / m.duration : 0;
  else p = present.auto ? (performance.now() - present.enteredAt) / (spec?.hold || SLIDE_TIMED_MS) : 0;
  $('#present-progress').style.width = `${(Math.max(0, Math.min(1, p)) * 100).toFixed(1)}%`;
  if (spec?.kind === 'scene' && m === present.audio) {
    const words = $$('.scene-caption .w', spec.el);
    const lit = Math.ceil(p * words.length);
    words.forEach((w, k) => cls(w, 'lit', k < lit));
  }
  present.raf = requestAnimationFrame(presentLoop);
}

function setPresentAuto(on) {
  present.auto = on;
  const b = $('#present-auto');
  b.setAttribute('aria-pressed', String(on));
  setText(b, on ? '⏵ Auto-play' : '⏸ Manual');
  if (!present.open) return;
  if (!on) {
    clearTimeout(present.timer);
    present.timer = null;
  } else if (!present.media || present.media.ended) scheduleAdvance(present.slides[present.idx]?.hold || SLIDE_TIMED_MS);
}

function initPresentation() {
  $('#present-prev').addEventListener('click', () => showSlide(present.idx - 1));
  $('#present-next').addEventListener('click', () => showSlide(present.idx + 1));
  $('#present-close').addEventListener('click', closePresentation);
  $('#present-auto').addEventListener('click', () => setPresentAuto(!present.auto));
  setPresentAuto(true);
  $('#present-dots').addEventListener('click', (ev) => {
    const d = ev.target.closest('.dot');
    if (d) showSlide(Number(d.dataset.i));
  });
  document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement && present.open && present.wasFullscreen) closePresentation();
    present.wasFullscreen = !!document.fullscreenElement;
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// 16. Boot: health, showcase, recent runs, global keys
// ═══════════════════════════════════════════════════════════════════════════

const health = { mode: null };

async function loadHealth() {
  const badge = $('#mode-badge');
  try {
    const res = await api('/api/health');
    health.mode = res?.mode || 'mock';
    setText(badge, health.mode === 'live' ? '● LIVE' : '● MOCK');
    badge.dataset.mode = health.mode;
    const models = res?.models || {};
    badge.title = Object.entries(models)
      .map(([k, v]) => `${k}: ${v}`)
      .join('\n') || 'Backend mode';
    if (res?.ffmpeg === false) toast('ffmpeg missing on the server — final cut will fail.', 'warn');
  } catch {
    setText(badge, '● OFFLINE');
    badge.dataset.mode = 'offline';
  }
}

/** Disable the "Watch sample run" buttons (with a reason) when the server has no finished run to replay. */
async function checkShowcase() {
  try {
    const res = await api('/api/showcase');
    const none = !res?.run_id;
    for (const b of [$('#sample-btn'), $('#hero-sample')]) {
      cls(b, 'sample-none', none);
      b.title = none ? 'No finished sample run on this server yet: launch one first' : `Replay run ${res.run_id} at ${REPLAY_SPEED}× speed`;
    }
  } catch {
    /* health badge already reports an offline server */
  }
}

async function watchSample() {
  const res = await act('Sample run', () => api('/api/showcase'));
  if (!res) return;
  if (!res.run_id) return toast('No finished sample run yet — launch one first.', 'warn');
  toast(`Replaying run ${res.run_id} at ${REPLAY_SPEED}× speed`, 'info');
  await openRun(res.run_id, { replay: true });
  $('#workspace').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function toggleRecent() {
  const menu = $('#recent-menu');
  const btn = $('#recent-btn');
  if (!menu.hidden) {
    menu.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    return;
  }
  menu.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  menu.replaceChildren(h('div', { class: 'muted small pad' }, 'Loading…'));
  try {
    const runs = (await api('/api/runs')) || [];
    if (!runs.length) {
      menu.replaceChildren(h('div', { class: 'muted small pad' }, 'No runs yet.'));
      return;
    }
    menu.replaceChildren(
      ...runs.map((r) =>
        h(
          'button',
          { type: 'button', class: 'recent-item', dataset: { id: r.id } },
          r.thumb && safeUrl(r.thumb) ? h('img', { src: safeUrl(r.thumb), alt: '' }) : h('span', { class: 'recent-ph' }),
          h('span', { class: 'recent-text' }, h('b', {}, r.campaign_name || r.id), h('span', { class: 'mono muted small' }, `${r.id} · ${r.status}${r.created_at ? ` · ${new Date(r.created_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : ''}`)),
        ),
      ),
    );
  } catch (err) {
    menu.replaceChildren(h('div', { class: 'err-text small pad' }, err.message));
  }
}

function initGlobal() {
  $('#sample-btn').addEventListener('click', watchSample);
  $('#hero-sample').addEventListener('click', watchSample);
  $('#recent-btn').addEventListener('click', toggleRecent);
  $('#recent-menu').addEventListener('click', (ev) => {
    const item = ev.target.closest('.recent-item');
    if (!item) return;
    $('#recent-menu').hidden = true;
    openRun(item.dataset.id);
  });
  document.addEventListener('click', (ev) => {
    if (!ev.target.closest('.recent')) $('#recent-menu').hidden = true;
  });
  $('#brand-home').addEventListener('click', (ev) => {
    ev.preventDefault();
    resetUi();
    store.runId = null;
    store.run = null;
    setHash(null);
    markDirty();
    $('#brief-text').focus();
  });
  // Telemetry drawer: closed by default; an explicit open/close is remembered across reloads.
  const setTele = (open, persist = true) => {
    document.body.classList.toggle('tele-closed', !open);
    $('#telemetry-toggle').setAttribute('aria-expanded', String(open));
    if (persist) {
      try {
        localStorage.setItem(TELE_KEY, open ? 'open' : 'closed');
      } catch {
        /* storage unavailable (private mode): the choice just is not remembered */
      }
    }
  };
  const toggleTele = () => setTele(document.body.classList.contains('tele-closed'));
  $('#telemetry-toggle').addEventListener('click', toggleTele);
  let saved = null;
  try {
    saved = localStorage.getItem(TELE_KEY);
  } catch {
    saved = null;
  }
  setTele(saved === 'open', false);
  $('#dir-fanout').addEventListener('click', onFanoutClick);
  $('#dir-input').addEventListener('keydown', (ev) => {
    if (cancelSpokenDirection() && ev.key === 'Escape') ev.preventDefault();
  });
  $('#log-clear').addEventListener('click', () => $('#event-log').replaceChildren());

  $('#dir-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    submitDirection($('#dir-input').value.trim());
  });

  new VoiceCapture({
    button: $('#brief-mic'),
    meter: $('#brief-meter'),
    onText: (t) => {
      const ta = $('#brief-text');
      ta.value = ta.value.trim() ? `${ta.value.trim()} ${t}` : t;
      ta.dispatchEvent(new Event('input'));
    },
  });
  new VoiceCapture({
    button: $('#dir-mic'),
    meter: $('#dir-meter'),
    onText: queueSpokenDirection,
  });

  document.addEventListener('keydown', (ev) => {
    if (present.open) {
      if (ev.key === 'ArrowRight' || ev.key === 'PageDown' || ev.key === ' ') showSlide(present.idx + 1);
      else if (ev.key === 'ArrowLeft' || ev.key === 'PageUp') showSlide(present.idx - 1);
      else if (ev.key === 'Escape') closePresentation();
      else if (ev.key === 'a' || ev.key === 'A') setPresentAuto(!present.auto);
      else return;
      ev.preventDefault();
      return;
    }
    if (ev.key === 'Escape' && cancelSpokenDirection()) return;
    const typing = ev.target.closest?.('input, textarea, [contenteditable]');
    if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === 't' || ev.key === 'T') toggleTele();
    else if (ev.key === 'b' || ev.key === 'B') document.body.classList.toggle('big-screen');
    else if ((ev.key === 'p' || ev.key === 'P') && store.run?.plan) openPresentation();
  });

  window.addEventListener('hashchange', () => {
    const id = hashRunId();
    if (id && id !== store.runId) openRun(id);
  });
  setInterval(() => {
    if (store.run) tick();
  }, 200);
}

/** Attach the web fonts without blocking first paint (system fallbacks render meanwhile / offline). */
function loadFonts() {
  document.head.appendChild(h('link', { rel: 'stylesheet', href: FONTS_URL }));
}

function boot() {
  loadFonts();
  initScrollTracking();
  initBriefPanel();
  initPresentation();
  initGlobal();
  loadHealth();
  checkShowcase();
  const id = hashRunId();
  if (id) openRun(id);
  markDirty();
}

boot();
