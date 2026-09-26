import assert from 'node:assert/strict';
import { tasteFrom, STYLES, limit, findMedia } from './server.js';

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

// Response shapes the video/music models might use.
assert.deepEqual(findMedia({ candidates: [{ content: { parts: [{ text: 'hi' }, { inlineData: { mimeType: 'video/mp4', data: 'AAA' } }] } }] }), { mimeType: 'video/mp4', data: 'AAA' });
assert.deepEqual(findMedia({ predictions: [{ bytesBase64Encoded: 'BBB', mimeType: 'audio/wav' }] }), { mimeType: 'audio/wav', data: 'BBB' });
assert.equal(findMedia({ response: { generateVideoResponse: { generatedSamples: [{ video: { uri: 'https://x.googleapis.com/v1/files/a:download' } }] } } }).uri, 'https://x.googleapis.com/v1/files/a:download');
assert.equal(findMedia({ candidates: [{ content: { parts: [{ fileData: { mimeType: 'video/mp4', fileUri: 'https://g/f' } }] } }] }).uri, 'https://g/f');
assert.equal(findMedia({ candidates: [{ content: { parts: [{ text: 'no media' }] } }] }), null);
console.log('ok');
