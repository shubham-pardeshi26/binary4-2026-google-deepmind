# AdMate Studio: brief to storyboard to film to score, in one loop

**Kaggle GDM Hyderabad Hackathon · Problem Statement 3: Multimodal Creative Pipelines with GenMedia**
**Focus models:** Nano Banana 2 Lite · Gemini Omni Flash · Lyria 3.5. **Supporting:** Gemini 3.8 Flash · Gemini 3.8 Flash TTS · Gemini 3.5 Transcribe

## The problem

A 30-second ad normally takes weeks: a storyboard, a shoot, an edit, a score, then the same again for every market. Generative AI promised to collapse that, but most tools are still one prompt box per modality. You generate an image in one place, animate it in another, and find music somewhere else. Each step forgets the last. The hero's face changes between shots, the music ignores the pacing, and every revision starts from zero.

An ad needs the same product and cast in every scene, motion that respects the keyframe, a soundtrack that follows the story's energy, a voice that sells, and fast iteration when a client says "make it feel like a monsoon evening". In AdMate Studio, each model's output becomes the next model's input, and a change in one modality flows through the others.

## What it does

You give AdMate a one-line brief, typed or spoken, with an optional brand name, product photo, aspect ratio (16:9 or 9:16), number of scenes and target markets. A few minutes later you have a finished, narrated, scored and captioned video ad, a campaign poster kit, and localized versions. Then you keep directing it in plain English.

1. **Creative director (Gemini 3.8 Flash).** Turns the brief into a structured campaign plan:
   - a name, tagline and CTA
   - a brand bible: palette, visual style, typography, hero and product
   - a narrator voice
   - three to six scenes, each with a story beat (hook, build, reveal, CTA), duration, mood, energy, camera move and a word-budgeted voiceover line
2. **Continuity anchor (Nano Banana 2 Lite).** Renders one clean reference frame of the hero and product. This anchor, plus the uploaded product photo, goes into every storyboard call, so the same product and character carry through the ad.
3. **Storyboard fan-out (Nano Banana 2 Lite).** Generates several variants of every scene in parallel. Tiles stream into the UI the moment each image lands, each labelled with its own latency.
4. **Judge tournament with self-repair (Gemini 3.8 Flash vision).**
   - scores every variant on brief fit, brand consistency, composition, continuity and artefacts, then crowns a winner with a written rationale
   - if the best score is below a quality threshold, writes concrete fix instructions; Nano Banana edits the winner and the judge re-scores
   - you can override any pick with one click
5. **Motion (Gemini Omni Flash).** Each winning keyframe becomes a video clip the moment it is chosen, animated from that exact frame with the planned camera move.
6. **Soundtrack (Lyria 3.5).** Composes from a timed prompt built from the scene plan: durations, moods, energy curve, genre, tempo and key. It ends on a resolved sting for the call to action.
7. **Narration (Gemini Flash TTS).** Voices each scene's line in the plan's chosen voice and style. The lines tell a mini-story across the ad: hook, desire, product benefit, then brand and CTA.
8. **Final cut (ffmpeg).**
   - joins the clips with crossfades and places each narration line on its scene's timecode
   - ducks the music under the voice with a sidechain compressor
   - writes WebVTT captions
   - re-stitches automatically whenever anything changes
9. **Campaign kit (Nano Banana 2 Lite + Flash).** Once every scene has a winning keyframe, it builds marketing posters in five formats: Instagram post, Story cover, print poster, web banner and billboard.
   - two variants per format, with rendered headline and CTA typography, anchored to the hero keyframe
   - a Flash judge picks the best of each, scoring legibility of the exact copy, brand fit, composition and impact
   - the whole kit downloads as a ZIP

## Directing the ad after the first cut

The first cut is where the work starts, not where it ends.

- **Per-clip chat.** Type "slower push-in" or "add gentle rain" on any clip. Omni performs a real multi-turn edit: each clip keeps its Interactions API ID, so the next instruction builds on the previous take instead of starting over. In parallel, Flash reads the instruction for mood changes. If the mood shifts, Lyria re-scores automatically and records why ("scene s2 → moody").
- **Direct the whole ad in one sentence.** "Make it a monsoon evening" becomes a direction plan covering:
  - an Omni instruction for each scene
  - mood updates
  - an optional keyframe restyle
  - a music instruction
  - updated voiceover lines or tone

  Every clip edit, the re-score and any re-voiced lines then run concurrently, and the cut re-stitches when they land.
- **Re-voice, regenerate, override** any single scene: its line, its storyboard, or its keyframe.
- **Localize.** For each market in parallel (for example Hyderabad in Telugu, Chennai in Tamil, Tokyo in Japanese):
  - Flash writes a localization plan with translated narration in native script and cultural adaptation of setting, cast and props
  - Nano Banana edits every winning keyframe and poster, keeping the composition and product
  - Lyria makes a regional variant of the score, and TTS speaks the localized lines
  - the result is a narrated, captioned animatic for each market
- **Present.** A full-screen, slide-by-slide pitch: one narrated slide per scene, then the full film, a "how it was made" slide built from live telemetry, and the localized animatics.

## Why speed and continuity are load-bearing

Problem Statement 3 asks for chained, high-throughput pipelines where speed and cross-modal continuity carry the experience. Three design choices make that true here.

**A pipeline with no global barrier.** After planning, every scene runs as an independent chain: variants, judge, optional repair, winner, Omni render. Scene 1 can be rendering video while scene 4 is still being judged, and nothing waits for "all storyboards done". The slowest modalities start earliest: the first score and every scene's narration begin the moment the plan exists, because the plan already contains durations, moods and lines.

**Speed buys quality.** Nano Banana 2 Lite's latency makes it affordable to draft several images per scene, keep one, and repair it if it isn't good enough. The same speed pays for five poster formats with two candidates each. A slow image model could not run a tournament; it would have to accept its first draft.

**Continuity is designed in, not hoped for.**
- The anchor frame and brand bible go into every image call.
- Repairs, restyles and localizations are edits with the base image first, so composition survives.
- Omni animates the exact winning keyframe, and its stateful edits preserve what you didn't ask to change.
- The music is timed to the planned scene durations and re-scored when a scene's mood changes.
- Narration is placed on each scene's timecode, with the music ducked beneath it.

**Speed is visible.** Every tile shows its latency, a pipeline rail shows each stage's timing, and a telemetry drawer shows NB2 p50/p95, images per minute, in-flight work per modality, time to first image, clip and final cut, and which API path each model used.

## Engineering

- **One adapter for every Google model.** Preview IDs and request shapes change, so one adapter tries an ordered list of API paths and remembers which worked per model, shrinks requests on HTTP 400, backs off on rate limits, and caps concurrency per modality.
- **Failure is contained.** Exceptions become events, not crashes. A failed Omni render falls back to a Ken Burns move over the keyframe, a failed voiceover is left out of the mix, and a missing soundtrack never blocks the cut. The film always completes.
- **Event-sourced state.** Every state change is an event, streamed over server-sent events and logged to disk, so refreshes, reconnects and "Watch sample run" replays all use one mechanism.
- **Demo-safe.** A mock mode runs the whole studio offline through the same pipeline and UI; per-visitor rate limits protect the API quota.
- **Stack.** Python 3.11, FastAPI and the google-genai SDK on the backend; ffmpeg for media; a vanilla-JavaScript studio UI with no build step. It ships as a single Docker image and is deployed on Hugging Face Spaces.

## Who it's for

Small businesses that can't afford an agency, agencies that want to pitch in minutes instead of days, and regional marketers who need one campaign in five languages by tomorrow. The output is a narrated, captioned film, posters in five formats, and localized versions, all consistent with one brand.

## What's next

- lip-synced on-screen talent
- brand-kit memory across campaigns
- judge ensembles and a rubric learned from user overrides
- export straight to ad platforms
- moving run state to shared storage, so it can scale beyond a single instance
