# PS3 — Multimodal Creative Pipelines with GenMedia

> **Chosen for build (2026-09-26): #4 Stage-to-Walk.** Other ideas parked for reference.

**Models:** Nano Banana 2 Lite (`gemini-3.1-flash-lite-image`), Gemini Omni Flash
(`gemini-omni-1.1-flash`), Lyria 3.5 (`lyria-3.5`). Gemini 3.8 Flash can act as the glue
(planner, critic, mood extractor), but it isn't the star here.

## What the judges are scoring

Read the PS text as a rubric:

| Phrase in PS | What it really means | How to prove it in the demo |
|---|---|---|
| "NB2 Lite generates 1K images in under 2 seconds" | They want **volume**. 1 image = fail. | Fire 20–60 images in parallel, show a grid filling live, and show a timer. |
| "Omni lets you generate and **conversationally edit** video with real-world physics" | Multi-turn video edits, and physics as a visible feature | At least 2 edit turns on the same clip, e.g. "make it rain" → "now slow-mo the splash". |
| "Lyria scores custom audio" | Music has to be *derived from* the visuals, not a stock track | Change the visuals, and the score changes with them. |
| "in a single loop" / "chained" | Output of model A **automatically** feeds model B | Show a pipeline strip: NB2 → Omni → Lyria with live status. |
| "speed and cross-modal continuity are load-bearing" | Same character/palette/mood across image, video and music. Slow = broken UX. | Same character across 12 frames and into the video, with music that matches the mood. |

**The anti-pattern (stated verbatim):** "A prompt box that spits out one image or clip
won't cut it."

**The load-bearing test:** if generation took 30s per image instead of 2s, would the app
still make sense? If yes, you've built the wrong thing.

**Crowding warning:** "localized ad engines" and "storyboard generation" are named in the
PS itself. Expect 30–50% of PS3 teams to build one of those. Build one of them only if your
execution will clearly beat the rest. Otherwise take a less obvious idea.

---

## The ideas

Each one lists: **pipeline**, the **30-second demo moment**, **why speed is load-bearing**,
**continuity trick**, **risk**.

### 1. Glocal: Localized Ad Factory
One product photo and a one-line brief go in. Out comes a grid of **8 markets × 5
variants**: local models, seasons, street backdrops, price formats, and culturally correct
props. A Gemini Flash "cultural critic" flags problems, e.g. "white flowers = funeral in
this market". Pick the best per market: Omni makes a 6s spot, and Lyria writes a
market-specific sonic sting (tabla for Mumbai, city-pop for Tokyo).
- **Pipeline:** NB2 ×40 (parallel) → Flash critic → Omni per market → Lyria per market
- **Demo moment:** 40 tiles fill in under 60s, then "Tokyo: make it raining at night" edits the video conversationally.
- **Load-bearing:** 40 images is only interactive at 2s/image.
- **Continuity:** the product reference image is locked in every prompt; a per-market palette is stored as state.
- **Risk:** Low. **Crowding: very high.** It's the PS's own example.

### 2. Beat-Locked: Song → Music Video
Most teams will go image → music. **Invert it.** Lyria generates a track, or the user
uploads or hums one. Gemini Flash maps the song structure (intro/verse/drop, BPM,
energy curve). NB2 generates **one keyframe per bar**, with style intensity following the
energy curve. Omni animates the key sections, and everything is cut exactly on the beat.
- **Pipeline:** Lyria → Flash (structure map) → NB2 ×32 (one per bar) → Omni (drop sections) → auto-edit
- **Demo moment:** press play. The frames flip on every beat, and the visuals explode on the drop.
- **Load-bearing:** 32+ frames per song. At 30s/frame you'd wait 16 minutes.
- **Continuity:** music → visuals is the *primary* continuity axis. Few teams will attempt it.
- **Risk:** Medium. You need a BPM/section map; Flash can be asked for it, or use a simple onset detector.

### 3. Branchcast: Real-Time Choose-Your-Own-Adventure Film
An interactive short film. At each decision point, NB2 instantly renders **3 preview
stills** of possible next scenes. The viewer (or the judges, via QR vote) picks one. Omni
renders it as video, continuing from the last frame, and Lyria shifts the score's tension
to match.
- **Pipeline:** Flash (story engine) → NB2 ×3 branch previews → vote → Omni continuation → Lyria mood shift
- **Demo moment:** the judges vote on their phones, and the story visibly goes their way within seconds.
- **Load-bearing:** branch previews must appear before the audience loses interest (<3s).
- **Continuity:** the last frame of each video becomes the reference image for the next generation.
- **Risk:** Medium-high, because Omni latency sets the pacing. Mitigation: pre-render the next scene while the vote runs.

### 4. Stage-to-Walk: Virtual Staging → Walkthrough
A real-estate photo of an empty room goes in, and **20 staging styles** come back in about 30
seconds (Japandi, mid-century, boho...). The buyer swipes Tinder-style, and Flash learns
their taste and regenerates the next batch closer to it. The final pick becomes an Omni
camera walkthrough with physics (curtains moving, light shifting), plus a Lyria ambient
bed matched to the style.
- **Pipeline:** NB2 ×20 → swipe-feedback loop → NB2 ×20 (refined) → Omni walkthrough → Lyria ambience
- **Demo moment:** swipe 5 times and batch 2 is visibly "your taste".
- **Load-bearing:** the preference loop only works if each batch lands in seconds.
- **Continuity:** the room geometry is locked via the source photo as reference.
- **Risk:** Low-medium. Clear business value (a real, paid industry). Judges like "this is a company".

### 5. Runway: Fashion Drop Engine
One garment sketch or photo goes in. Out come 30 colorway and fabric variants, each shown
on diverse body types. Pick 5 and Omni generates a **runway walk with real cloth physics**,
which is Omni's headline capability. Lyria scores the show to the collection's mood, and
the runway is cut to its beat.
- **Pipeline:** NB2 ×30 → pick → NB2 lookbook (consistent model) → Omni runway ×5 → Lyria show track
- **Demo moment:** the silk dress catching the air mid-walk, on the beat.
- **Load-bearing:** designers iterate hundreds of colorways, and speed is the product.
- **Continuity:** the same garment across stills, video, and body types.
- **Risk:** Medium. Cloth physics quality is the gamble. Smoke-test Omni on fabric in hour 0.

### 6. Game-Jam-in-a-Box
Type a game pitch: "cozy underwater farming sim". You get a full asset pack: 16 tiles,
8 character sprites, 4 props, and a title screen in one consistent style, laid out on a
**playable** tiny canvas map. Omni makes the intro cutscene, and Lyria makes the level
theme plus a variant for each biome.
- **Pipeline:** Flash (art bible) → NB2 ×30 (style-anchored) → canvas map → Omni cutscene → Lyria ×3 loops
- **Demo moment:** walk a character across a generated map while the music changes by zone.
- **Load-bearing:** an asset pack is volume by definition.
- **Continuity:** one "art bible" image anchors every asset.
- **Risk:** Medium. Sprite transparency is a pain; use a solid background plus chroma key.

### 7. Memory Sketch Artist
A police-sketch-artist workflow for **memories**. "My grandmother's kitchen in 1994, yellow
walls, radio on the counter." NB2 shows 6 interpretations, and the user corrects them
conversationally ("no, the window was on the left, warmer light"), converging over a few
turns. The final memory becomes an Omni scene with gentle motion (steam, curtains), and
Lyria scores it in the style of that era.
- **Pipeline:** NB2 ×6 per turn (multi-turn convergence) → Omni → Lyria (era-matched)
- **Demo moment:** a judge describes a real memory and gets a 10s film of it, with music, in about 2 minutes.
- **Load-bearing:** convergence needs many fast turns; slow = user gives up.
- **Continuity:** each turn edits the chosen image rather than regenerating from scratch.
- **Risk:** Low tech risk, **very high emotional impact**. Easy to demo live with a judge.

### 8. Crowd VJ: Audience-Driven Live Visual Show
A live-performance tool. The audience scans a QR code and submits words. Every bar, NB2
turns the top-voted prompt into a visual that's synced to the Lyria music, which is itself
steered by the room's aggregate mood. Periodically Omni renders a "hero moment" clip from
the best stills.
- **Pipeline:** audience input → Flash (aggregate + moderate) → NB2 (one per bar) + Lyria (steered) → Omni hero clips
- **Demo moment:** the entire judging room is steering the show from their phones.
- **Load-bearing:** one new visual every ~2s is only possible with NB2 Lite speed.
- **Continuity:** a rolling style anchor (the last 3 frames) prevents visual whiplash.
- **Risk:** Medium-high. Lyria steering latency is the unknown. Needs moderation, because the crowd will type rude things.

### 9. Brand Stress-Test
Upload a brand kit (logo, colors, font, 3 sample ads). The system generates the brand
across **50 touchpoints**: billboard, IG story, packaging, app splash, merch, storefront.
A Flash "brand guardian" scores each one for guideline compliance and auto-regenerates
failures. The output also includes a sonic logo (Lyria) and a 15s brand film (Omni).
- **Pipeline:** NB2 ×50 → Flash critic → regen failures → Omni brand film → Lyria sonic logo
- **Demo moment:** a compliance heatmap goes red → green as auto-fixes land.
- **Load-bearing:** 50 touchpoints plus regen cycles.
- **Continuity:** that *is* the product: brand consistency across modalities.
- **Risk:** Low-medium. B2B-flavoured and clear value, but less "wow" than the others.

### 10. Evolve: Creative Darwinism
Generate 60 ad creatives. A Flash judge (optionally with a crowd vote) scores them on
hook, clarity, and brand fit. **Kill the bottom 50 and mutate the top 10** (swap color,
angle, headline, subject). Run 4–5 generations live, then send the champion to Omni and
Lyria as the final spot.
- **Pipeline:** NB2 ×60 → Flash fitness → mutate → NB2 ×60 → ... ×5 → Omni + Lyria on the winner
- **Demo moment:** generation 1 and generation 5 side by side, with a fitness chart climbing.
- **Load-bearing:** 300 images in a few minutes. This is the most literal "high-throughput pipeline" possible.
- **Continuity:** the lineage tree shows each image's ancestors.
- **Risk:** Low-medium. The judge's scores must actually correlate with quality, or the chart is fake.

### 11. Recipe → Cooking Short (Physics Showcase)
Paste any recipe. NB2 generates step-by-step photos with consistent kitchen and hands.
Omni animates the physics-heavy steps (pouring, sizzling, dough folding), which play to
Omni's real-world physics claim. Lyria scores it in the cuisine's style, and the result is
auto-edited into a 30s vertical short. Conversational edits like "make it an overhead
shot" or "slower pour" work too.
- **Pipeline:** Flash (step breakdown) → NB2 ×8 steps → Omni physics steps → Lyria → vertical edit
- **Demo moment:** honey pouring in slow motion, on the beat.
- **Load-bearing:** 8 stills plus several video segments per recipe. A creator makes 5 a day.
- **Continuity:** the same kitchen, bowl, and hands across every step.
- **Risk:** Medium. Liquid physics may look uncanny, so test it in hour 0.

### 12. Living Storybook: Kid Co-Author
A child speaks or types one sentence at a time, and the page is illustrated in about 2s,
**before they finish thinking of the next sentence**. The same hero stays consistent across
12 pages. Lyria underscores each page's mood, and at "The End" Omni animates the whole book
into a short film.
- **Pipeline:** NB2 per page (character-locked) → Lyria per mood → Omni finale film
- **Demo moment:** a 10-page book, with a consistent hero, built live in 3 minutes, then played as a film.
- **Load-bearing:** kids' attention span is about 5s; slow generation breaks the magic.
- **Continuity:** a character reference sheet generated on page 1 is reused on every page.
- **Risk:** Low. Highly relatable to judges, but "storybook" will also be a common idea.

### 13. Trailer Forge: Pitch → Launch Trailer
Paste your startup's landing page URL or pitch. Flash writes a trailer script with shot
list. NB2 renders the storyboard (12 shots), you conversationally edit it, Omni renders
the shots, and Lyria makes an epic trailer score whose hits land on the title cards. Meta
bonus: **use it to make your own hackathon demo video.**
- **Pipeline:** Flash script → NB2 ×12 → edit loop → Omni ×N → Lyria (hit points) → assembled trailer
- **Demo moment:** "we made our own pitch video with it" as the closing line.
- **Load-bearing:** storyboard iteration speed.
- **Risk:** Medium. The storyboard → video path is close to the "storyboard" crowd.

---

## Comparison

Wow = judge reaction. Unique = how few other teams will build it. Fit = how literally it
matches the PS rubric (volume + chain + continuity + multi-turn video).

| # | Idea | Wow | Unique | Fit | Build risk | Live-judge interaction |
|---|------|-----|--------|-----|-----------|------------------------|
| 1 | Glocal ad factory | ★★★ | ★ | ★★★★★ | Low | – |
| 2 | **Beat-Locked (song → MV)** | ★★★★★ | ★★★★★ | ★★★★ | Med | play any song |
| 3 | Branchcast CYOA film | ★★★★ | ★★★★ | ★★★★ | Med-High | vote |
| 4 | Stage-to-Walk | ★★★ | ★★★★ | ★★★★ | Low-Med | swipe |
| 5 | Runway fashion | ★★★★ | ★★★★ | ★★★★ | Med | – |
| 6 | Game-Jam-in-a-Box | ★★★★ | ★★★ | ★★★★ | Med | pitch a game |
| 7 | **Memory Sketch Artist** | ★★★★★ | ★★★★★ | ★★★ | Low | describe memory |
| 8 | Crowd VJ | ★★★★★ | ★★★★ | ★★★★ | Med-High | whole room |
| 9 | Brand Stress-Test | ★★★ | ★★★ | ★★★★ | Low-Med | – |
| 10 | **Evolve (creative Darwinism)** | ★★★★ | ★★★★ | ★★★★★ | Low-Med | vote |
| 11 | Recipe → cooking short | ★★★ | ★★★★ | ★★★★ | Med | paste recipe |
| 12 | Living Storybook | ★★★★ | ★★ | ★★★★ | Low | kid sentence |
| 13 | Trailer Forge | ★★★★ | ★★ | ★★★★ | Med | paste URL |

## Recommendation: top 3

1. **Beat-Locked (#2).** Most unique, and it scores continuity in the direction nobody
   else will try (audio drives visuals). The demo is instantly legible: frames snapping to
   the beat need no explanation. It uses all three models as load-bearing.
2. **Evolve (#10).** The most literal answer to "chained, high-throughput pipeline". It's
   technically the safest (mostly NB2 + Flash loops), and the fitness chart makes invisible
   work visible. It can **absorb #1 (ad factory)** as the use case while standing out from
   the ad-engine crowd.
3. **Memory Sketch Artist (#7).** Highest emotional ceiling, lowest tech risk. It loses a
   little on "throughput", but it wins the room.

**Combo play:** Evolve with a *localized* ad use case (#10 + #1). Judges see their own
example done at 10× the scale anyone else attempted.

---

## Build-agnostic technical notes (apply to whichever idea we pick)

- **Hour 0 smoke tests (non-negotiable):** one call each to NB2 Lite (text → image,
  image + ref → image), Omni (text → video, *edit* an existing video), and Lyria
  (prompt → track). Record real latencies, which decide pacing. If Omni editing or Lyria
  is flaky, we choose an idea that degrades gracefully.
- **Continuity toolkit:** (a) a reference/anchor image passed to every NB2 call,
  (b) a shared JSON "style bible" (palette, subject, lens, era) prepended to every prompt,
  (c) the last video frame fed back as the reference for the next shot, and (d) Flash
  reading generated images and producing the Lyria prompt (mood, tempo, instruments), so
  the music is *derived* from the visuals.
- **Throughput:** fire NB2 calls in parallel with a concurrency cap (start at ~8 and tune to
  the rate limit), and stream results into the grid as each lands. Don't wait for all of them.
- **Visible pipeline strip:** a top bar showing NB2 → Flash → Omni → Lyria with live
  counts, per-stage latency, and a running "images generated" counter. Cheap to build, and
  it's what makes "high-throughput" legible to the judges.
- **Stack default:** Vite + React + Tailwind, a thin Node/Express (or FastAPI) proxy
  holding the API key, and SSE to stream results. No DB; keep state in memory plus
  localStorage.
- **Demo insurance:** pre-generate one full run and keep it cached, and record a fallback
  video by hour 7.
