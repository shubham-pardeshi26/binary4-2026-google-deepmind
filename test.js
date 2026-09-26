import assert from 'node:assert/strict';
import { tasteFrom, STYLES, limit, findMedia, redact } from './server.js';

assert.equal(STYLES.length, 20);
assert.equal(new Set(STYLES.map(s => s.pose)).size, 20, 'every preset has its own pose');
assert.equal(tasteFrom().variants.length, 20, 'no swipes still yields a batch');

const t = tasteFrom(['villain', 'noir'], ['commercial', 'comedic']);
assert.equal(t.variants.length, 20);
assert.ok(['villain', 'noir'].includes(t.variants[0].base), 'a liked type ranks first');
assert.ok(!t.variants.some(v => ['commercial', 'comedic'].includes(v.base)), 'passed types never come back');
assert.ok(t.tags.includes('dramatic') && t.avoid.includes('friendly'));
assert.equal(new Set(t.variants.slice(0, 4).map(v => v.pose)).size, 4, 'lighting variants of one type get different poses');

const slot = limit(3);
let live = 0, peak = 0;
await Promise.all(Array.from({ length: 10 }, (_, i) => slot(async () => {
  peak = Math.max(peak, ++live);
  await new Promise(r => setTimeout(r, 5));
  live--;
  if (i === 4) throw new Error('boom');
}).catch(() => {})));
assert.equal(peak, 3, 'limiter caps concurrency, and a failing task releases its slot');

// generateContent (images) and Interactions (video / music) response shapes.
assert.deepEqual(findMedia({ candidates: [{ content: { parts: [{ text: 'hi' }, { inlineData: { mimeType: 'image/png', data: 'AAA' } }] } }] }), { mimeType: 'image/png', data: 'AAA' });
assert.deepEqual(findMedia({ id: 'i1', steps: [{ type: 'thought' }, { type: 'model_output', content: [{ type: 'text', text: 'lyrics' }, { type: 'audio', mime_type: 'audio/mpeg', data: 'BBB' }] }] }), { mimeType: 'audio/mpeg', data: 'BBB', uri: undefined });
assert.equal(findMedia({ outputs: [{ type: 'video', uri: 'https://generativelanguage.googleapis.com/v1beta/files/abc' }] }).uri, 'https://generativelanguage.googleapis.com/v1beta/files/abc');
assert.equal(findMedia({ steps: [{ type: 'model_output', content: [{ type: 'text', text: 'sorry' }] }] }), null);
assert.ok(!redact({ data: 'x'.repeat(5000) }).includes('xxxx'), 'redact hides base64 blobs');
console.log('ok');
