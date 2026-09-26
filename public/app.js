const $ = s => document.querySelector(s);
const S = { source: null, batch: [], round: 1, liked: [], disliked: [], deck: 0, pick: null, edits: [], music: null, muted: false, busy: false, ctl: null, reelRun: 0, slate: null };
let images = 0, actx, stopAudio = () => {};

const show = id => document.querySelectorAll('main > section').forEach(s => (s.hidden = s.id !== id));
const secs = t0 => ((performance.now() - t0) / 1000).toFixed(1);
const make = (tag, props) => Object.assign(document.createElement(tag), props);

function node(n, state, text) {
  const li = $(`[data-node=${n}]`);
  li.className = state;
  li.querySelector('small').textContent = text;
}

// Runs one pipeline stage with a live elapsed-seconds readout (video can take a minute+).
async function step(n, text, promise, done) {
  const t0 = performance.now();
  node(n, 'run', text);
  const tick = setInterval(() => node(n, 'run', `${text} ${secs(t0)}s`), 500);
  try {
    const r = await promise;
    node(n, 'done', done(r, secs(t0)));
    return r;
  } catch (e) {
    node(n, 'err', 'failed');
    throw e;
  } finally {
    clearInterval(tick);
  }
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 8000);
}
addEventListener('unhandledrejection', e => e.reason?.name !== 'AbortError' && toast(e.reason?.message || String(e.reason)));

async function post(url, payload) {
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

async function stream(url, payload, onLine, signal) {
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload), signal });
  if (!r.ok) throw new Error((await r.json()).error || r.statusText);
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const lines = (buf += value).split('\n');
    buf = lines.pop();
    lines.filter(Boolean).forEach(l => onLine(JSON.parse(l)));
  }
}

// Live mode returns src; mock returns src=null + a CSS look applied to the source photo.
function media(src, look) {
  const wrap = make('div', { className: 'media' });
  wrap.append(make('img', { src: src || S.source, draggable: false }));
  if (!src) {
    wrap.classList.add('mocked');
    wrap.firstChild.style.filter = look.filter;
    wrap.style.setProperty('--tint', look.tint);
  }
  return wrap;
}

// ---------- 1. Consent + upload ----------
const input = $('#drop input');
$('#consent input').onchange = e => {
  input.disabled = !e.target.checked;
  $('#drop').classList.toggle('locked', !e.target.checked);
};

async function loadImage(file) {
  let bmp;
  try {
    bmp = await createImageBitmap(file);
  } catch {
    throw new Error(`Can't read “${file.name}”. Use a JPG, PNG or WebP (iPhone HEIC photos: export as JPG first).`);
  }
  const k = Math.min(1, 1024 / Math.max(bmp.width, bmp.height));
  const c = make('canvas', { width: Math.round(bmp.width * k), height: Math.round(bmp.height * k) });
  c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', 0.9);
}

async function start(file) {
  if (!file) return;
  if (!$('#consent input').checked) return toast('Tick the consent box first.');
  Object.assign(S, { source: await loadImage(file), liked: [], disliked: [], round: 1, slate: null });
  $('#playSlate').hidden = true;
  $('#slate').textContent = '🎙 Record slate (8s)';
  stage(undefined, 'Batch 1 · 20 casting headshots');
}

input.onchange = () => { start(input.files[0]); input.value = ''; }; // reset so re-picking the same file fires again
$('#drop').ondragover = e => { e.preventDefault(); $('#drop').classList.add('over'); };
$('#drop').ondragleave = () => $('#drop').classList.remove('over');
$('#drop').ondrop = e => { e.preventDefault(); $('#drop').classList.remove('over'); start(e.dataTransfer.files[0]); };
$('#newPhoto').onclick = () => { S.ctl?.abort(); show('upload'); };

// ---------- 2. Headshot batch (NB2 fan-out) ----------
async function stage(styles, title) {
  S.ctl?.abort(); // a new batch cancels any still-streaming one so tiles never mix
  const ctl = (S.ctl = new AbortController());
  show('grid');
  $('#grid h2').textContent = title;
  $('#grid .meta').textContent = '';
  $('#toDeck').textContent = S.round === 1 ? 'Teach it your taste →' : '↻ Swipe again';
  $('#toDeck').disabled = $('#toReel').disabled = true;
  S.batch = [];
  S.pick = null;
  const n = styles?.length ?? 20;
  const tiles = $('#grid .tiles');
  tiles.replaceChildren(...Array.from({ length: n }, () => make('div', { className: 'tile pending' })));
  const t0 = performance.now();
  node('nb2', 'run', `0/${n}`);
  try {
    await stream('/api/stage', { image: S.source, styles }, item => {
      const el = tiles.children[item.i];
      if (item.error) return (el.className = 'tile err', el.textContent = item.error);
      S.batch.push(item);
      $('#count').textContent = `${++images} images`;
      el.className = 'tile ai';
      el.replaceChildren(media(item.src, item.style), make('span', { textContent: item.style.name }));
      el.onclick = () => pick(el, item);
      $('#grid .meta').textContent = `${S.batch.length}/${n} generated in ${secs(t0)}s`;
      node('nb2', 'run', `${S.batch.length}/${n} · ${secs(t0)}s`);
    }, ctl.signal);
  } catch (e) {
    if (e.name === 'AbortError') return;
    node('nb2', 'err', 'failed');
    throw e;
  }
  node('nb2', S.batch.length ? 'done' : 'err', `${S.batch.length}/${n} in ${secs(t0)}s`);
  $('#toDeck').disabled = !S.batch.length;
  const refused = tiles.querySelector('.tile.err')?.textContent;
  if (!S.batch.length && refused) toast(refused);
}

function pick(el, item) {
  document.querySelectorAll('.tile.picked').forEach(t => t.classList.remove('picked'));
  el.classList.add('picked');
  S.pick = item;
  $('#toReel').disabled = false;
}

// ---------- 3. Swipe deck → taste ----------
$('#toDeck').onclick = () => { S.deck = 0; show('deck'); learn(); card(); };

function card() {
  const item = S.batch[S.deck];
  if (!item) return refine();
  const c = $('.card');
  c.style.transform = '';
  c.replaceChildren(media(item.src, item.style), make('h3', { textContent: item.style.name }), make('p', { textContent: `${item.style.tags.join(' · ')} — ${item.style.pose}` }));
}

function decide(love) {
  const item = S.batch[S.deck];
  if (!item || S.busy) return;
  S.busy = true;
  (love ? S.liked : S.disliked).push(item.style);
  const c = $('.card');
  c.style.transition = 'transform .25s';
  c.style.transform = `translateX(${love ? 700 : -700}px) rotate(${love ? 20 : -20}deg)`;
  setTimeout(() => { c.style.transition = ''; S.deck++; S.busy = false; learn(); card(); }, 250);
}

// Live readout only; the ranking itself runs server-side on refine.
function learn() {
  const score = {};
  S.liked.forEach(s => s.tags.forEach(t => (score[t] = (score[t] || 0) + 1)));
  S.disliked.forEach(s => s.tags.forEach(t => (score[t] = (score[t] || 0) - 1)));
  const top = Object.entries(score).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).slice(0, 4);
  $('#learn').textContent = `${S.liked.length} loved · ${S.disliked.length} passed` + (top.length ? ` · learning: ${top.map(([t, v]) => `${t}×${v}`).join(', ')}` : '');
  $('#refine').disabled = S.deck < 4;
}

async function refine() {
  const ids = list => [...new Set(list.map(s => s.base))];
  const taste = await step('taste', 'reading your swipes…', post('/api/taste', { liked: ids(S.liked), disliked: ids(S.disliked) }), t => t.tags.slice(0, 3).join(' · ') || 'no signal');
  S.round++;
  stage(taste.variants, `Batch ${S.round} · ${taste.summary}`);
}

$('#like').onclick = () => decide(true);
$('#nope').onclick = () => decide(false);
$('#refine').onclick = refine;
addEventListener('keydown', e => {
  if ($('#deck').hidden) return;
  if (e.key === 'ArrowRight') decide(true);
  if (e.key === 'ArrowLeft') decide(false);
});

const c = $('.card');
let x0 = null;
c.onpointerdown = e => { x0 = e.clientX; c.setPointerCapture(e.pointerId); };
c.onpointermove = e => { if (x0 !== null) c.style.transform = `translateX(${e.clientX - x0}px) rotate(${(e.clientX - x0) / 20}deg)`; };
c.onpointerup = e => {
  if (x0 === null) return;
  const dx = e.clientX - x0;
  x0 = null;
  Math.abs(dx) > 100 ? decide(dx > 0) : (c.style.transform = '');
};

// ---------- 4. Casting reel (Omni) + score (Lyria), in parallel ----------
$('#toReel').onclick = () => {
  actx ??= new AudioContext(); // must be created inside the click for autoplay rules
  actx.resume();
  S.edits = [];
  reel();
};

async function reel() {
  const run = ++S.reelRun; // a newer edit or leaving the screen makes older results stale
  show('reel');
  const { style, src } = S.pick;
  $('#reel h2').textContent = style.name;
  $('#edits').replaceChildren(...S.edits.map(e => make('li', { textContent: e })));
  $('.screen').classList.add('loading');
  const label = S.edits.length ? `edit ${S.edits.length}…` : 'rendering…';
  await Promise.all([
    step('omni', label, post('/api/reel', { image: src || S.source, style, edits: S.edits }), (_, s) => `${s}s`).then(v => run === S.reelRun && showVideo(v)),
    step('lyria', 'scoring…', post('/api/music', { style, edits: S.edits }), (_, s) => `${s}s`).then(m => run === S.reelRun && playMusic(m)),
  ]).catch(e => { if (run === S.reelRun) $('.screen').classList.remove('loading'); throw e; });
}

function showVideo(v) {
  const screen = $('.screen');
  screen.classList.remove('loading');
  if (v.src) screen.replaceChildren(make('video', { src: v.src, autoplay: true, loop: true, muted: true, playsInline: true, controls: true }));
  else screen.replaceChildren(Object.assign(media(null, v), { className: 'media mocked turn' }));
  $('#pOmni').textContent = `Omni: ${v.prompt}`;
}

function playMusic(m) {
  S.music = m;
  $('#pLyria').textContent = `Lyria: ${m.prompt}`;
  stopAudio();
  if (S.muted) return;
  if (m.src) {
    const a = make('audio', { src: m.src, loop: true });
    a.play().catch(() => toast('Click “Sound on” to play the score.'));
    stopAudio = () => a.pause();
  } else stopAudio = synth(m.mood);
}

// Mock stand-in for Lyria: a detuned pad chord with a slowly breathing filter.
function synth({ root, major }) {
  const t = actx.currentTime;
  const out = actx.createGain();
  out.gain.setValueAtTime(0, t);
  out.gain.linearRampToValueAtTime(0.1, t + 3);
  out.connect(actx.destination);
  const lp = actx.createBiquadFilter();
  lp.frequency.value = 900;
  lp.connect(out);
  const lfo = actx.createOscillator();
  const depth = actx.createGain();
  lfo.frequency.value = 0.07;
  depth.gain.value = 500;
  lfo.connect(depth).connect(lp.frequency);
  const oscs = [0, major ? 4 : 3, 7, 12, major ? 16 : 15].flatMap(n => [-7, 7].map(detune => {
    const o = actx.createOscillator();
    o.type = 'triangle';
    o.frequency.value = 440 * 2 ** ((root + n - 69) / 12);
    o.detune.value = detune;
    o.connect(lp);
    return o;
  }));
  [lfo, ...oscs].forEach(o => o.start(t));
  return () => {
    const now = actx.currentTime;
    out.gain.cancelScheduledValues(now);
    out.gain.setValueAtTime(out.gain.value, now);
    out.gain.linearRampToValueAtTime(0, now + 0.6);
    [lfo, ...oscs].forEach(o => o.stop(now + 0.7));
  };
}

function edit(text) {
  if (!text) return;
  S.edits.push(text);
  reel();
}
$('#edit').onsubmit = e => {
  e.preventDefault();
  const i = $('#edit input');
  edit(i.value.trim());
  i.value = '';
};
document.querySelectorAll('.chips button').forEach(b => (b.onclick = () => edit(b.textContent)));

// The slate is the actor's real voice from the mic, never a generated one.
$('#slate').onclick = async () => {
  const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  const rec = new MediaRecorder(mic);
  const chunks = [];
  rec.ondataavailable = e => chunks.push(e.data);
  rec.onstop = () => {
    mic.getTracks().forEach(t => t.stop());
    S.slate = URL.createObjectURL(new Blob(chunks, { type: rec.mimeType }));
    $('#slate').disabled = false;
    $('#slate').textContent = '🎙 Re-record slate';
    $('#playSlate').hidden = false;
  };
  rec.start();
  $('#slate').disabled = true;
  $('#slate').textContent = '● Recording… say your name, height, agency';
  setTimeout(() => rec.stop(), 8000);
};
$('#playSlate').onclick = () => S.slate && new Audio(S.slate).play();
$('#mute').onclick = () => {
  S.muted = !S.muted;
  $('#mute').textContent = S.muted ? '🔇 Sound off' : '🔊 Sound on';
  S.muted ? stopAudio() : S.music && playMusic(S.music);
};
$('#back').onclick = () => { S.reelRun++; stopAudio(); show('grid'); };

fetch('/api/config').then(r => r.json()).then(cfg => ($('#mock').hidden = !cfg.mock));
