# AdMate Studio: Brief to Storyboard to Film to Score, in One Loop

*A pipelined GenMedia ad studio where Nano Banana 2 Lite, Gemini Omni Flash and Lyria 3.5 work as one chained system, and you direct the result in plain English.*

**Track:** Problem Statement 3

## The problem with "prompt box" creative tools

Most generative creative tools are one prompt box per modality. Each step forgets the last: the hero changes face between shots, the music ignores the pacing, every revision starts from zero.

An ad exposes this. It needs a consistent product and cast across scenes, motion that respects the keyframe, a score that follows the story's energy, a voice that sells, and fast iteration when a client says "make it feel like a monsoon evening." AdMate Studio makes that one loop: each model's output is the next model's input, and a change in one modality flows through the others.

## What AdMate Studio does

You give AdMate a brief, typed or spoken, plus an optional brand name and product photo. Then:

1. **Gemini 3.8 Flash** acts as creative director and returns a structured plan: campaign name, tagline, palette, style, scenes with beats, durations, moods, energy and a voiceover line each, plus a music brief and a narrator voice.
2. **Nano Banana 2 Lite** renders a continuity anchor, then fans out K variants for every scene in parallel.
3. A **Flash vision judge** scores each scene's variants on brief fit, brand consistency, composition, continuity and artefacts. If the best score falls below a threshold, it writes concrete fix instructions, NB2 repairs the winner, and the judge re-runs.
4. **Gemini Omni Flash** animates each winning keyframe into a clip.
5. **Lyria 3.5** scores the ad from a timed prompt built from the scene plan, while **Flash TTS** voices every scene's line.
6. ffmpeg stitches the final cut with narration on each scene's timecode, music ducked underneath and WebVTT captions. It auto-updates whenever anything changes.
7. **Nano Banana 2 Lite** turns the winners into a **Campaign Kit**: judged, typography-rendered posters for Instagram, Stories, print, web and billboards, plus a one-click ZIP of every asset.

Then the loop keeps going. You can chat with any clip ("slower push-in", "add gentle rain"), or type one sentence to direct the whole ad. You can override a judge's pick, re-voice a line, or localize for Hyderabad, Chennai or Tokyo. The default view is deliberately simple; a "Behind the scenes" toggle reveals models, scores and telemetry. Presentation mode is a slide-by-slide narrated pitch: one slide per scene with its clip and voiceover, then the film and a "how it was made" slide from live telemetry.

## Architecture

**A pipelined scheduler with no global barrier.** After planning, every scene is an independent async chain: variants, judge, optional repair, winner, Omni render. Scene 1 can be rendering video while scene 4 is still being judged. Nothing waits for "all storyboards done."

**Latency hiding.** Lyria's first score and every scene's TTS start the moment the plan exists, in parallel with the anchor, because the plan already contains durations, moods and narration. Omni starts per scene the instant a winner exists. The slowest modalities start earliest, so the critical path is roughly director → anchor → one tournament → one Omni render; narration is off it entirely.

**The continuity anchor.** The anchor is a clean hero/product reference frame, plus the uploaded product photo if there is one. It goes into every NB2 call as a reference image, along with a brand bible (palette, typography, style) in every prompt. Repairs and localizations are NB2 edits with the base image first, so composition survives. Omni animates the exact winning frame and is told to keep its identity.

**Judge tournament with self-repair.** Draft-then-verify for images: generate K, have a vision model pick one and explain why, and if nothing clears the bar, feed its critique back as an edit instruction. A user override triggers a re-render.

**Conversational editing on Omni.** Each clip keeps its Interactions API `interaction_id`. A per-clip instruction becomes a new turn with `previous_interaction_id`, so "now add rain" builds on "golden hour" instead of starting over. In parallel, Flash reads the instruction for mood changes. **Direct the whole ad** asks Flash for a DirectionPlan: per-scene Omni instructions, mood updates, an optional keyframe restyle and a music instruction. AdMate then fans all of them out at once.

**Adaptive re-scoring and re-voicing.** When an edit changes a scene's mood, Lyria re-scores with a reason ("scene s2 → moody"). A tone change ("make it playful") re-voices the affected lines in parallel with the Omni edits. Sound follows picture.

**Campaign Kit, off the critical path.** When the last scene winner lands, a plugin (subscribed to the event stream, with no pipeline changes) has Flash write poster copy, then NB2 renders 5 formats × 2 layouts in parallel, anchored to the hero keyframe. A judge that caps any poster with garbled text picks each format's winner, all while Omni is still rendering. Localization NB2-edits only the poster text into each market's script.

**Localization fan-out.** For each market in parallel, Flash writes a localization plan (translated text and narration in native script, cultural adaptation of setting and props). NB2 edits every winning keyframe, Lyria produces a regional variant and TTS speaks the localized lines; together they become a narrated, captioned animatic per market.

## Why these technical choices

**NB2 Lite's speed is what makes the tournament affordable.** Discarding three of every four images only makes sense when an image arrives in seconds. A 4×4 storyboard plus repairs is 16–24 generations, but because they run in parallel the whole tournament costs roughly two NB2 round-trips of wall time (measured below). Speed becomes quality through selection.

**Interactions API for stateful video.** `previous_interaction_id` keeps context across edit turns, which fits conversational direction far better than stateless re-prompting.

**Narration forked at plan time, mixed at stitch time.** The director writes each line against a word budget (about 2.4 words per second of scene), so TTS needs no picture and runs concurrently with the storyboard. ffmpeg places each line at its scene start, time-stretches overruns up to 1.2×, and sidechain-ducks Lyria under the voice, keeping the two models' outputs intelligible without a second music pass.

**SSE event sourcing.** Every state change is an event, streamed over SSE and appended to `events.jsonl`. The UI rebuilds from state plus events, giving reconnect-safe dedupe, live telemetry and exact replays for free.

**Capability-probing adapters with path memory.** Preview APIs move. Every GenMedia method tries its primary path and falls back automatically (`generate_content`, `generate_videos`, a minimal request on HTTP 400), then remembers per model which path worked. `/api/health` reports it.

## Challenges we overcame

**Unverified preview API shapes.** We coded against request shapes we couldn't fully confirm in advance. Adaptive fallbacks handled most of this; for the rest, `scripts/smoke_test.py` exercises every model in pipeline order and prints the full exception chain for any failure, and `--raw` dumps raw SDK responses.

**Tail latency and rate limits.** Fan-outs create bursts. Each modality (including TTS) has its own semaphore, so an image burst never starves video, and 429/5xx errors retry with exponential backoff (0.8 s, 1.6 s, 3.2 s). Every error becomes an event and a readable status.

**Cross-modal sync.** The music prompt is a timed structure built from planned scene durations ("0:00–0:06 hook, warm, 0.4 energy…") and ends on a resolved sting for the call to action. At stitch time ffmpeg normalizes clips, crossfades, and fits the score to the real video length with a fade-out and loudness normalization.

**Demo reliability.** A failed voiceover is dropped from the mix and a failed Omni render falls back to a Ken Burns move over the keyframe, so the cut always completes. Mock mode runs the whole app offline with synthetic keyframes, clips, chords and speech, and the UI's LIVE/MOCK badge makes the mode obvious. Any run's event log replays at speed, so "Watch sample run" survives a network failure on stage.

## Results

Measured with `scripts/bench_nb2.py` and the live telemetry of a 4-scene × 4-variant run:

| Metric | Value |
|---|---|
| NB2 Lite latency p50 / p95 | [[NB2 p50]] / [[NB2 p95]] |
| NB2 Lite throughput | [[images/min]] images/min |
| Time to first image | [[TTFI]] |
| Time to first Omni clip | [[TTFC]] |
| Time to final cut | [[time-to-final]] |
| Generations per ad (storyboard + repairs) | [[images per run]] |
| Flash TTS voiceover p50 per scene | [[TTS p50]] |
| Campaign Kit (10 posters) wall time | [[kit time]] |

[[One sentence on what the numbers mean, e.g. first clip before the last storyboard is judged.]]

## What's next

- Brand-kit memory across campaigns, so the anchor and brand bible persist.
- Pairwise or ensemble judging, and learning the rubric from user overrides.
- Full Omni video per market, not just narrated animatics.
- Multi-instance deployment: with event sourcing this is a storage swap, not a rewrite.
