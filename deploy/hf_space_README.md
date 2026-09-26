---
title: AdMate Studio
emoji: 🎬
colorFrom: indigo
colorTo: pink
sdk: docker
app_port: 7860
pinned: false
license: apache-2.0
short_description: Brief to storyboard to film to score, in one loop.
---

# AdMate Studio — one-loop GenMedia ad studio

AdMate Studio turns a one-line brief (typed or spoken) into a finished, scored video ad in a single pipelined loop:
Gemini Flash plans the campaign, **Nano Banana 2 Lite** fans out a continuity-anchored storyboard
(N scenes x K variants, in parallel), a Flash vision judge runs a tournament with self-repair, **Gemini Omni Flash**
animates each winning keyframe the instant it is picked and lets you direct every shot conversationally, and
**Lyria** scores the whole thing — re-scoring automatically when your edits change the mood — while Gemini Flash TTS
narrates it scene by scene. ffmpeg stitches a captioned final cut with the music ducked under the voice, presentation
mode plays it back slide by slide, and NB2 + TTS localize it into narrated animatics for new markets. Every asset shows its latency and a live telemetry
panel tracks throughput, p50/p95 and in-flight work. Built for the Kaggle GDM Hyderabad Hackathon (Problem Statement 3).

Without a `GEMINI_API_KEY` Space secret the app runs in **mock mode** (synthetic assets) — use
"Watch sample run" to replay a recorded live run.
