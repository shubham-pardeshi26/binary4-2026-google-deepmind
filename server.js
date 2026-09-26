import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = process.env.PORT || 3000;
const KEY = process.env.GEMINI_API_KEY;
const LIVE = !!KEY && process.env.MOCK !== '1'; // MOCK=1 npm start → fully offline demo fallback
const MODELS = {
  image: process.env.IMAGE_MODEL || 'gemini-3.1-flash-lite-image',
  video: process.env.VIDEO_MODEL || 'gemini-omni-1.1-flash',
  music: process.env.MUSIC_MODEL || 'lyria-3.5',
};
const API = 'https://generativelanguage.googleapis.com/v1beta';
const PUBLIC = join(import.meta.dirname, 'public');
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const jitter = (a, b) => a + Math.random() * (b - a);

// filter/tint only drive the mock look; desc/pose/tags/mood feed the real prompts.
export const STYLES = [
  ['corporate', 'Corporate', 'navy blazer, soft key light, clean grey backdrop', ['formal', 'friendly', 'studio', 'soft'], 'saturate(.85) brightness(1.05)', '#9aa7b8', 55, true],
  ['commercial', 'Commercial smile', 'bright even light, casual knit, big genuine smile', ['friendly', 'casual', 'studio', 'warm'], 'brightness(1.12) saturate(1.15)', '#f0c98a', 60, true],
  ['theatrical', 'Theatrical', 'neutral expression, dark backdrop, single key light', ['dramatic', 'studio', 'formal', 'cool'], 'contrast(1.25) brightness(.85) saturate(.8)', '#3b4252', 48, false],
  ['romlead', 'Rom-com lead', 'soft window light, warm tones, half smile', ['warm', 'friendly', 'soft', 'casual'], 'sepia(.2) brightness(1.08) saturate(1.1)', '#e8a98f', 57, true],
  ['villain', 'Villain', 'low hard light, cold palette, intense stare', ['dramatic', 'edgy', 'cool', 'bold'], 'contrast(1.4) brightness(.7) hue-rotate(190deg) saturate(.6)', '#20304a', 45, false],
  ['editorial', 'Fashion editorial', 'high-contrast studio light, bold styling', ['bold', 'studio', 'edgy', 'dramatic'], 'contrast(1.35) saturate(1.3)', '#c2185b', 52, false],
  ['detective', 'Detective', 'leather jacket, city street at dusk, serious', ['outdoor', 'edgy', 'cool', 'casual'], 'saturate(.7) contrast(1.15) brightness(.85)', '#44617a', 47, false],
  ['doctor', 'Doctor / lawyer', 'crisp white shirt, bright office background blur', ['formal', 'cool', 'friendly', 'studio'], 'brightness(1.15) saturate(.8) contrast(.95)', '#dbe7f0', 58, true],
  ['period', 'Period drama', 'high collar, painterly Rembrandt light', ['dramatic', 'warm', 'formal', 'soft'], 'sepia(.45) contrast(1.15) brightness(.88)', '#7a5230', 50, false],
  ['scifi', 'Sci-fi', 'cool rim light, sleek dark wardrobe, neon hint', ['cool', 'edgy', 'bold', 'studio'], 'hue-rotate(160deg) saturate(1.4) contrast(1.2)', '#1fb5c9', 53, false],
  ['bw', 'Classic B&W', 'black and white, timeless studio portrait', ['mono', 'dramatic', 'studio', 'formal'], 'grayscale(1) contrast(1.2)', '#ffffff', 51, true],
  ['golden', 'Golden-hour outdoor', 'natural backlight, park bokeh, relaxed', ['outdoor', 'warm', 'casual', 'friendly'], 'sepia(.3) saturate(1.35) brightness(1.08)', '#f2a33a', 59, true],
  ['musician', 'Indie musician', 'denim, brick wall, moody side light', ['casual', 'edgy', 'outdoor', 'warm'], 'sepia(.3) contrast(1.2) brightness(.9)', '#9b4a2c', 49, false],
  ['athlete', 'Athletic', 'sportswear, bright hard light, determined', ['bold', 'casual', 'outdoor', 'friendly'], 'contrast(1.2) saturate(1.3) brightness(1.05)', '#e65a2e', 56, true],
  ['comedic', 'Comedic', 'playful expression, colourful backdrop', ['friendly', 'bold', 'casual', 'warm'], 'saturate(1.8) brightness(1.1)', '#f2d230', 62, true],
  ['noir', 'Film noir', 'venetian-blind shadows, 1940s mood', ['mono', 'dramatic', 'edgy', 'cool'], 'grayscale(1) contrast(1.5) brightness(.8)', '#222222', 44, false],
  ['daytime', 'Daytime TV', 'glossy beauty light, polished styling', ['soft', 'friendly', 'warm', 'studio'], 'brightness(1.12) contrast(.92) saturate(1.1)', '#f5c6d0', 60, true],
  ['action', 'Action hero', 'dust, grit, hard sun, tactical wear', ['edgy', 'bold', 'outdoor', 'dramatic'], 'sepia(.35) contrast(1.35) saturate(1.1)', '#8a6a3a', 46, false],
  ['fantasy', 'Fantasy epic', 'hooded cloak, candlelit warm glow', ['dramatic', 'warm', 'bold', 'soft'], 'sepia(.4) saturate(1.3) brightness(.9)', '#b5651d', 50, false],
  ['founder', 'Tech founder', 'hoodie, modern office bokeh, approachable', ['casual', 'friendly', 'cool', 'outdoor'], 'saturate(.9) brightness(1.08) hue-rotate(-10deg)', '#8fb3d9', 57, true],
].map(([id, name, desc, tags, filter, tint, root, major]) => ({ id, base: id, name, desc, tags, filter, tint, mood: { root, major } }));

const POSES = [
  'facing camera straight on, shoulders square', 'three-quarter turn to the left, looking into the lens', 'arms crossed, slight lean forward',
  'over-the-shoulder glance back at camera', 'chin resting lightly on hand', 'three-quarter turn to the right, soft gaze past the lens',
  'leaning against a wall, relaxed', 'hands in pockets, weight on one leg', 'looking slightly up and off-camera',
  'seated, forearms on knees, looking up', 'mid-laugh, candid moment', 'hand adjusting jacket collar',
  'profile view, looking off to the side', 'head tilted slightly, direct eye contact', 'walking toward camera, mid-stride',
  'looking down, then eyes up to lens', 'arms relaxed at sides, tight head-and-shoulders crop', 'hand running through hair',
  'seated sideways on a chair, turned to camera', 'leaning on a table, both hands down, intense look',
];
STYLES.forEach((s, i) => (s.pose = POSES[i]));

const LIGHTS = [
  { id: 'softkey', name: 'Soft key', desc: 'large soft key light', filter: 'brightness(1.05)' },
  { id: 'rembrandt', name: 'Rembrandt', desc: 'Rembrandt lighting, triangle of light on the cheek', filter: 'contrast(1.15) brightness(.92)' },
  { id: 'butterfly', name: 'Butterfly', desc: 'butterfly (paramount) lighting from above', filter: 'brightness(1.1) saturate(1.05)' },
  { id: 'rim', name: 'Rim light', desc: 'strong rim light separating them from the background', filter: 'contrast(1.25) brightness(.85)' },
];

// Mock-only look changes for conversational edits; the real video model reads the edit text directly.
const LOOKS = [
  { re: /black|b&w|mono/i, filter: 'grayscale(1) contrast(1.2)', tint: '#ffffff', shift: -2, major: false },
  { re: /dramatic|moody|intense|dark|night/i, filter: 'contrast(1.35) brightness(.8)', tint: '#2a2a3a', shift: -4, major: false },
  { re: /smile|friendly|happy|energy/i, filter: 'brightness(1.1) saturate(1.2)', tint: '#f5d08a', shift: 4, major: true },
  { re: /golden|sunset|warm/i, filter: 'sepia(.3) saturate(1.3) brightness(1.05)', tint: '#e0913a', shift: 2, major: true },
];

const role = s => s.name.split(' · ')[0];
export const prompts = {
  image: s => `Create a new professional ${role(s)} casting headshot of the person in this photo: ${s.desc}. Pose: ${s.pose}. Preserve their exact face, identity, age, skin tone and features. Change the pose, wardrobe, lighting, background and expression to match. Photorealistic 85mm portrait, casting quality.`,
  video: (s, edits) => [`Casting reel of this exact person as a ${role(s)} type (${s.desc}): they face camera, turn to show left and right profiles, then give a natural smile and a serious take. Keep their identity exactly.`, ...edits.map(e => `Direction: ${e}.`)].join(' '),
  music: (s, edits) => [`Subtle instrumental underscore for an actor's ${role(s)} casting reel (${s.tags.join(', ')}), ${s.mood.major ? 'warm, major key' : 'tense, minor key'}, unobtrusive, no vocals, about 20 seconds.`, ...edits.map(e => `Mood direction: ${e}.`)].join(' '),
};

// Counts liked (+1) and passed (-1) tags, ranks the unpassed types by that score, and returns
// the top 5 types × 4 lighting setups, rotating poses so the 4 shots of one type differ.
export function tasteFrom(liked = [], disliked = []) {
  const score = {};
  const bump = (ids, d) => ids.forEach(id => STYLES.find(s => s.id === id)?.tags.forEach(t => (score[t] = (score[t] || 0) + d)));
  bump(liked, 1);
  bump(disliked, -1);
  const tags = Object.entries(score).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).map(([t]) => t).slice(0, 5);
  const avoid = Object.entries(score).filter(([, v]) => v < 0).sort((a, b) => a[1] - b[1]).map(([t]) => t).slice(0, 3);
  const fit = s => s.tags.reduce((a, t) => a + (score[t] || 0), 0) + (liked.includes(s.id) ? 2 : 0);
  const top = STYLES.filter(s => !disliked.includes(s.id)).sort((a, b) => fit(b) - fit(a)).slice(0, 5);
  const variants = top.flatMap(s => LIGHTS.map((l, j) => ({
    ...s, id: `${s.id}-${l.id}`, name: `${s.name} · ${l.name}`, desc: `${s.desc}, ${l.desc}`, filter: `${s.filter} ${l.filter}`,
    pose: POSES[(POSES.indexOf(s.pose) + j * 5) % POSES.length],
  })));
  const summary = tags.length ? `leaning ${tags.slice(0, 3).join(', ')}${avoid.length ? `; avoiding ${avoid.join(', ')}` : ''}` : 'no strong signal yet';
  return { tags, avoid, summary, variants };
}

function applyEdits(style, edits) {
  const last = edits.map(e => LOOKS.find(l => l.re.test(e))).filter(Boolean).at(-1);
  return {
    filter: last ? `${style.filter} ${last.filter}` : style.filter,
    tint: last?.tint ?? style.tint,
    mood: last ? { root: style.mood.root + last.shift, major: last.major } : style.mood,
  };
}

// ---------- Gemini API ----------

export const limit = (n, queue = []) => async fn => {
  if (n <= 0) await new Promise(r => queue.push(r));
  n--;
  try { return await fn(); } finally { n++; queue.shift()?.(); }
};
const imageSlot = limit(6); // ponytail: global cap of 6 in-flight image calls; tune to the key's rate limit.

async function callApi(path, body) {
  const r = await fetch(`${API}/${path}`, {
    method: body ? 'POST' : 'GET',
    headers: { 'content-type': 'application/json', 'x-goog-api-key': KEY },
    body: body && JSON.stringify(body),
  }).catch(e => { throw new Error(`can't reach Gemini API (${e.cause?.code || e.cause?.message || e.message})`); });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`${path.split(':')[0]}: ${j.error?.message || `HTTP ${r.status}`}`);
  return j;
}

// Ask the API how each model is called instead of hard-coding it (no docs for Omni / Lyria yet).
const methods = {};
const methodsOf = model => (methods[model] ??= callApi(`models/${model}`)
  .then(m => m.supportedGenerationMethods ?? [])
  .catch(e => { delete methods[model]; throw e; }));

const inline = dataUrl => {
  const [, mimeType, data] = dataUrl?.match(/^data:(.+?);base64,(.+)$/) ?? [];
  if (!data) throw Object.assign(new Error('image must be a base64 data URL'), { status: 400 });
  return { mimeType, data };
};

// Finds the first media payload anywhere in a response; covers generateContent (inlineData / fileData)
// and predict-style (bytesBase64Encoded / video.uri) shapes.
export function findMedia(o) {
  if (!o || typeof o !== 'object') return null;
  if (o.inlineData?.data) return { mimeType: o.inlineData.mimeType, data: o.inlineData.data };
  if (o.bytesBase64Encoded) return { mimeType: o.mimeType, data: o.bytesBase64Encoded };
  if (o.fileData?.fileUri) return { mimeType: o.fileData.mimeType, uri: o.fileData.fileUri };
  if (typeof o.uri === 'string' && /^https:/.test(o.uri)) return { mimeType: o.mimeType, uri: o.uri };
  for (const v of Object.values(o)) {
    const m = findMedia(v);
    if (m) return m;
  }
  return null;
}

function checkBlocked(j) {
  if (j.promptFeedback?.blockReason) throw new Error(`input refused (${j.promptFeedback.blockReason}): public-figure photos are blocked; use your own photo`);
  const c = j.candidates?.[0];
  if (c && !c.content?.parts) {
    console.warn('blocked:', JSON.stringify({ finishReason: c.finishReason, finishMessage: c.finishMessage, safetyRatings: c.safetyRatings }));
    throw new Error(`blocked: ${[c.finishReason, c.finishMessage].filter(Boolean).join(' · ')}`);
  }
}

async function poll(name, timeoutMs = 6 * 60e3) {
  for (const t0 = Date.now(); Date.now() - t0 < timeoutMs; await sleep(4000)) {
    const op = await callApi(name);
    if (op.error) throw new Error(op.error.message);
    if (op.done) return op;
  }
  throw new Error('timed out after 6 min');
}

async function download(uri, mimeType) {
  // Only Google hosts get the key; redirects are followed manually so the key never leaks to the target.
  const google = new URL(uri).hostname.endsWith('.googleapis.com');
  let r = await fetch(uri, { headers: google ? { 'x-goog-api-key': KEY } : {}, redirect: 'manual' });
  if (r.status >= 300 && r.status < 400) r = await fetch(r.headers.get('location'));
  if (!r.ok) throw new Error(`media download failed: HTTP ${r.status}`);
  const type = r.headers.get('content-type')?.split(';')[0] || mimeType || 'application/octet-stream';
  return `data:${type};base64,${Buffer.from(await r.arrayBuffer()).toString('base64')}`;
}

async function generate(model, { prompt, image, modalities, fallbackType }) {
  const supported = await methodsOf(model);
  const img = image && inline(image);
  let j;
  if (supported.includes('generateContent')) {
    const parts = [...(img ? [{ inlineData: img }] : []), { text: prompt }];
    j = await callApi(`models/${model}:generateContent`, { contents: [{ parts }], generationConfig: { responseModalities: modalities } });
    checkBlocked(j);
  } else {
    const method = ['predictLongRunning', 'predict'].find(m => supported.includes(m));
    if (!method) throw new Error(`${model} only supports: ${supported.join(', ') || 'nothing'}; tell Claude`);
    j = await callApi(`models/${model}:${method}`, { instances: [{ prompt, ...(img && { image: { bytesBase64Encoded: img.data, mimeType: img.mimeType } }) }] });
    if (method === 'predictLongRunning') j = await poll(j.name);
  }
  const m = findMedia(j);
  if (!m) {
    const text = j.candidates?.[0]?.content?.parts?.map(p => p.text).filter(Boolean).join(' ');
    throw new Error(`${model} returned no media: ${(text || JSON.stringify(j)).slice(0, 200)}`);
  }
  return m.uri ? download(m.uri, m.mimeType) : `data:${m.mimeType || fallbackType};base64,${m.data}`;
}

const live = {
  mock: false,
  async stage(image, style) {
    const prompt = prompts.image(style);
    return { src: await imageSlot(() => generate(MODELS.image, { prompt, image, modalities: ['TEXT', 'IMAGE'], fallbackType: 'image/png' })), prompt };
  },
  async reel(image, style, edits) {
    const prompt = prompts.video(style, edits);
    return { src: await generate(MODELS.video, { prompt, image, modalities: ['VIDEO'], fallbackType: 'video/mp4' }), prompt };
  },
  async music(style, edits) {
    const prompt = prompts.music(style, edits);
    return { src: await generate(MODELS.music, { prompt, modalities: ['AUDIO'], fallbackType: 'audio/wav' }), prompt };
  },
};

const mock = {
  mock: true,
  async stage(image, style) { await sleep(jitter(700, 2200)); return { src: null, prompt: prompts.image(style) }; },
  async reel(image, style, edits) { await sleep(jitter(2500, 4000)); return { src: null, ...applyEdits(style, edits), prompt: prompts.video(style, edits) }; },
  async music(style, edits) { await sleep(jitter(1200, 2000)); return { src: null, mood: applyEdits(style, edits).mood, prompt: prompts.music(style, edits) }; },
};

const ai = LIVE ? live : mock;

// ---------- HTTP ----------

async function body(req) {
  const chunks = [];
  let size = 0;
  for await (const c of req) {
    if ((size += c.length) > 20e6) throw Object.assign(new Error('body too large'), { status: 413 });
    chunks.push(c);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString() || '{}');
  } catch {
    throw Object.assign(new Error('invalid JSON body'), { status: 400 });
  }
}

// NDJSON: one line per headshot as soon as it lands, so the grid fills live.
async function stage(req, res) {
  const { image, styles = STYLES } = await body(req);
  res.writeHead(200, { 'content-type': 'application/x-ndjson' });
  await Promise.all(styles.map(async (style, i) => {
    const t = Date.now();
    try {
      res.write(JSON.stringify({ i, style, ...(await ai.stage(image, style)), ms: Date.now() - t }) + '\n');
    } catch (e) {
      res.write(JSON.stringify({ i, style, error: e.message }) + '\n');
    }
  }));
  res.end();
}

const routes = {
  'GET /api/config': async () => ({ mock: ai.mock }),
  'POST /api/taste': async ({ liked, disliked }) => tasteFrom(liked, disliked),
  'POST /api/reel': async ({ image, style, edits = [] }) => ai.reel(image, style, edits),
  'POST /api/music': async ({ style, edits = [] }) => ai.music(style, edits),
};

async function serveStatic(req, res) {
  const path = normalize(join(PUBLIC, req.url === '/' ? 'index.html' : decodeURIComponent(req.url.split('?')[0])));
  if (!path.startsWith(PUBLIC + sep)) return res.writeHead(403).end();
  try {
    const data = await readFile(path);
    res.writeHead(200, { 'content-type': TYPES[extname(path)] || 'application/octet-stream' }).end(data);
  } catch {
    res.writeHead(404).end('not found');
  }
}

export async function handle(req, res) {
  try {
    if (req.method === 'POST' && req.url === '/api/stage') return await stage(req, res);
    const route = routes[`${req.method} ${req.url}`];
    if (!route) return await serveStatic(req, res);
    const result = await route(req.method === 'POST' ? await body(req) : {});
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(result));
  } catch (e) {
    console.error(`${req.method} ${req.url}:`, e.message);
    if (!res.headersSent) res.writeHead(e.status || 500, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: e.message }));
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  http.createServer(handle).listen(PORT, () => {
    console.log(`CastReel on http://localhost:${PORT}`);
    console.log(LIVE ? `LIVE · image ${MODELS.image} · video ${MODELS.video} · music ${MODELS.music}` : 'MOCK (no key, or MOCK=1)');
  });
}
