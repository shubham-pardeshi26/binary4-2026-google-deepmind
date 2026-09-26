// Diagnose the models: node --env-file=.env probe.js [path/to/photo.jpg]
import { readFileSync } from 'node:fs';
import { extname } from 'node:path';

const KEY = process.env.GEMINI_API_KEY;
const API = 'https://generativelanguage.googleapis.com/v1beta';
const MODELS = [process.env.IMAGE_MODEL || 'gemini-3.1-flash-lite-image', process.env.VIDEO_MODEL || 'gemini-omni-1.1-flash', process.env.MUSIC_MODEL || 'lyria-3.5'];
if (!KEY) throw new Error('GEMINI_API_KEY missing from .env');
const headers = { 'content-type': 'application/json', 'x-goog-api-key': KEY };

console.log('=== How each model is called');
for (const m of MODELS) {
  const j = await (await fetch(`${API}/models/${m}`, { headers })).json();
  console.log(m, '→', j.error ? `ERROR ${j.error.message}` : (j.supportedGenerationMethods || []).join(', '));
}
const list = await (await fetch(`${API}/models?pageSize=1000`, { headers })).json();
console.log('\n=== Media-related models on this key');
for (const m of list.models || []) if (/image|omni|lyria|veo|video|audio|music|tts/i.test(m.name)) console.log(m.name.replace('models/', ''), '→', (m.supportedGenerationMethods || []).join(', '));

const photo = process.argv[2];
if (!photo) process.exit();
const mimeType = { '.png': 'image/png', '.webp': 'image/webp' }[extname(photo).toLowerCase()] || 'image/jpeg';
const img = { inlineData: { mimeType, data: readFileSync(photo).toString('base64') } };
const tests = [
  ['text only (no photo)', [{ text: 'A professional studio headshot of a fictional smiling person, grey backdrop.' }]],
  ['your photo, neutral edit', [img, { text: 'Change the background to a plain light-grey studio backdrop.' }]],
  ['your photo, new pose', [img, { text: 'Show this person in a professional corporate headshot, three-quarter turn, navy blazer, soft studio light.' }]],
];
for (const [name, parts] of tests) {
  const r = await fetch(`${API}/models/${MODELS[0]}:generateContent`, { method: 'POST', headers, body: JSON.stringify({ contents: [{ parts }], generationConfig: { responseModalities: ['TEXT', 'IMAGE'] } }) });
  const j = await r.json();
  const c = j.candidates?.[0];
  const got = c?.content?.parts?.map(p => (p.inlineData ? `IMAGE(${p.inlineData.mimeType})` : `text: ${p.text?.slice(0, 120)}`));
  console.log(`\n=== image: ${name} → HTTP ${r.status}`, JSON.stringify({ error: j.error?.message, blockReason: j.promptFeedback?.blockReason, finishReason: c?.finishReason, got }));
}
