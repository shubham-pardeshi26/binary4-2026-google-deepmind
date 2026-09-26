# CastReel

Your photo → 20 professional casting headshots (different poses, looks, lighting) in seconds → swipe to teach it your casting type → a refined batch → a casting reel video with a Lyria underscore and your real recorded voice slate. You can direct the reel conversationally ("more intense", "black & white").

Pipeline: **NB2 Lite** (20 parallel headshots) → **taste engine** (swipes → refined batch) → **Omni Flash** (reel + edits) ∥ **Lyria 3.5** (score).

A consent checkbox is required before upload, and everything generated is labelled "AI-generated". Photos of public figures are refused by the image model.

## Run

```sh
npm start                               # http://localhost:3000   (Node 22+, no npm install)
MOCK=1 npm start                        # fully offline fallback for the demo
node --env-file=.env probe.js [photo]   # which methods each model supports + image checks
npm test
```

`GEMINI_API_KEY` goes in `.env` (git-ignored). You can override the models with `IMAGE_MODEL`, `VIDEO_MODEL` and `MUSIC_MODEL`.

The server asks the API how each model is called (`supportedGenerationMethods`) and then uses `generateContent`, `predictLongRunning` (polled) or `predict`, whichever the model supports.

Ideas backlog: `IDEAS.md`.
