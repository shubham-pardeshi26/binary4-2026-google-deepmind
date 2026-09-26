// Diagnose the models against the real API.
//   node --env-file=.env probe.js media        → one tiny Omni video + one Lyria clip, prints response structure
//   node --env-file=.env probe.js photo.jpg    → image-edit checks (is this photo refused?)
import { readFileSync } from 'node:fs';
import { extname } from 'node:path';

const KEY = process.env.GEMINI_API_KEY;
const API = 'https://generativelanguage.googleapis.com/v1beta';
const IMAGE = process.env.IMAGE_MODEL || 'gemini-3.1-flash-lite-image';
const VIDEO = process.env.VIDEO_MODEL || 'gemini-omni-1.1-flash';
const MUSIC = process.env.MUSIC_MODEL || 'lyria-3.5';
if (!KEY) throw new Error('GEMINI_API_KEY missing from .env');
const arg = process.argv[2];
if (!arg) throw new Error('usage: node --env-file=.env probe.js media | path/to/photo.jpg');
const headers = { 'content-type': 'application/json', 'x-goog-api-key': KEY };
const redact = j => JSON.stringify(j, (k, v) => (typeof v === 'string' && v.length > 200 ? `<${v.length} chars>` : v), 1);

async function post(path, body) {
  const t0 = Date.now();
  const r = await fetch(`${API}/${path}`, { method: 'POST', headers, body: JSON.stringify(body) });
  const text = await r.text();
  console.log(`→ HTTP ${r.status} in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  try { return JSON.parse(text); } catch { return { raw: text.slice(0, 500) }; }
}

if (arg === 'media') {
  console.log(`\n=== ${MUSIC} (Interactions API)`);
  console.log(redact(await post('interactions', { model: MUSIC, input: 'A 15-second calm instrumental piano underscore. Instrumental only, no vocals.' })).slice(0, 3000));
  console.log(`\n=== ${VIDEO} (Interactions API, 360p text-to-video)`);
  console.log(redact(await post('interactions', { model: VIDEO, input: 'A person slowly turning their head in a photo studio, single continuous shot.', response_format: { type: 'video', aspect_ratio: '16:9', resolution: '360p' } })).slice(0, 3000));
} else {
  const mimeType = { '.png': 'image/png', '.webp': 'image/webp' }[extname(arg).toLowerCase()] || 'image/jpeg';
  const img = { inlineData: { mimeType, data: readFileSync(arg).toString('base64') } };
  for (const [name, parts] of [
    ['text only (no photo)', [{ text: 'A professional studio headshot of a fictional smiling person, grey backdrop.' }]],
    ['your photo, neutral edit', [img, { text: 'Change the background to a plain light-grey studio backdrop.' }]],
    ['your photo, new pose', [img, { text: 'Show this person in a professional corporate headshot, three-quarter turn, navy blazer, soft studio light.' }]],
  ]) {
    console.log(`\n=== image: ${name}`);
    const j = await post(`models/${IMAGE}:generateContent`, { contents: [{ parts }], generationConfig: { responseModalities: ['TEXT', 'IMAGE'] } });
    const c = j.candidates?.[0];
    console.log(JSON.stringify({ error: j.error?.message, blockReason: j.promptFeedback?.blockReason, finishReason: c?.finishReason, got: c?.content?.parts?.map(p => (p.inlineData ? `IMAGE(${p.inlineData.mimeType})` : `text: ${p.text?.slice(0, 120)}`)) }));
  }
}
