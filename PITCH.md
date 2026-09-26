# AdLoop pitch script (3 minutes + Q&A)

> **Rule for the whole pitch: only claim what the demo shows.** Every number in `[brackets]` must come from your own live run's telemetry drawer or `scripts/bench_nb2.py`. Never guess numbers on stage. Voiceover narration was added in commit `90690d7`: confirm your deployed run's final cut actually has narration before you mention it.

---

## Before you go on stage (setup checklist)

1. **Tab A: a finished showcase run.** Run one full live ad 30–60 min before the pitch (brief below) and let it finish. Then:
   - run one **Direct** ("make it a monsoon evening")
   - run one **Localize** (Hyderabad · Telugu + Tokyo · Japanese)

   This tab is your safety net and your "after" state. Note the numbers from its telemetry drawer:
   - `[images generated]`, `[NB2 p50]`, `[time to first image]`, `[time to first clip]`, `[time to final]`
   - `[judge calls]`, `[repair rounds]`, `[Omni edits]`, `[music versions]`
2. **Tab B: the studio, empty**, with the brief pre-typed, ready to click **Launch loop**:
   - **Brief:** *"Launch ad for an Irani chai café in Old Hyderabad: warm, nostalgic, evening crowd, Osmania biscuits"*
   - **Brand:** *Chai Charminar* · **16:9** · **4 scenes × 4 variants**
3. **Check the badge:** the top bar must say **● LIVE**, not MOCK. If Google is unreachable, say so honestly and use Tab A's **Watch sample run** (a replay of a real run).
4. **Screen:** telemetry drawer **closed** (open it once, on cue), browser zoom so the storyboard grid fits, sound on for the final cut.
5. **Fallback order:** a live run → Tab A (finished) → **Watch sample run** replay → `ADMATE_MOCK=1` (say "offline mode").

---

## The 3-minute script

### 0:00–0:20 · Hook (the problem)
> "A 30-second ad takes an agency weeks: storyboard, shoot, edit, score, then do it again for every city.
> Today's AI tools don't fix that. They're prompt boxes. You make an image here, a clip there, music somewhere else, and **each step forgets the last**: the hero's face changes between shots and the music ignores the story."

### 0:20–0:35 · What AdLoop is (one line), then click Launch
*(Click **Launch loop** in Tab B while saying this.)*
> "AdLoop turns **one line of brief into a finished, scored film ad**, and then lets you *direct* it in plain English. Nano Banana 2 Lite, Omni Flash and Lyria 3.5 work as **one chained loop**, where every model's output is the next model's input."

### 0:35–1:35 · Live run (narrate what's appearing)
*(Point at each part as it lights up. The run keeps going while you talk.)*

- **Pipeline rail and plan card:**
  > "Gemini 3.8 Flash is the creative director. In seconds it wrote a plan: campaign, tagline, a brand bible with this palette, and four scenes, each with a beat, a mood and a duration."
- **Music chip pulsing:**
  > "Notice Lyria is **already composing**. The plan tells it every scene's length and mood, so the slowest model starts first, before a single image exists."
- **Anchor frame, then tiles streaming in with latency badges:**
  > "Nano Banana first makes a **continuity anchor** of our hero and product, and every storyboard frame is generated *against* it. Now four variants per scene, all in parallel. Each tile shows its own latency, about `[NB2 p50]` seconds."
- **Crowns and scores appearing:**
  > "A Flash vision judge scores every variant on five axes (brief fit, brand consistency, composition, continuity, artefacts) and crowns a winner. If nothing clears the bar, it writes fix instructions and Nano Banana **repairs** the winner. Fast image generation is what makes it affordable to draft four and keep one."
- **Motion lab, a clip rendering while another scene is still judging:**
  > "And this is the key design choice: **there's no global barrier**. Scene one is already being animated by Omni Flash while scene four is still being judged. Every scene is its own chain."

### 1:35–2:15 · Direct it (switch to Tab A: the finished run)
*(Switch tabs. Say "here's one we launched earlier, same pipeline", then play the final cut for 5–8 seconds with sound.)*
> "That's the stitched, scored cut. Now the part no prompt box can do. I'll direct the **whole ad** in one sentence."

*(Type into the director bar: **"make it a monsoon evening"** and press Enter.)*
> "Flash turns that one sentence into a plan for every modality. Each clip gets its own Omni edit, and these are **multi-turn edits**: Omni remembers the previous take, so it changes the weather and keeps the shot. At the same time Lyria re-scores to the new mood, and the final cut re-stitches itself when they land."

*(Point at the clip cards going to "rendering", the version pills v2, and the soundtrack version list showing "direction: monsoon evening".)*

### 2:15–2:35 · Localize + proof of speed
*(Scroll to the Localize panel you ran earlier. Then open the telemetry drawer.)*
> "Localization for Hyderabad in Telugu and for Tokyo: Nano Banana edits every winning keyframe, translating the on-image text and adapting the setting while keeping the composition, and Lyria makes a regional variant of the score."
>
> "And here's the proof speed matters: `[images generated]` images, first frame in `[time to first image]`, first clip in `[time to first clip]`, a finished scored film in `[time to final]`, with `[judge calls]` judge calls and `[repair rounds]` repairs along the way."

### 2:35–3:00 · Why it clears the bar + close
> "So that's Problem Statement 3's bar, met on screen:
> - it's a **chain, not a prompt box**
> - **throughput** makes the judge-and-repair tournament affordable
> - **continuity** comes from the anchor frame through Omni's stateful edits
> - and the **soundtrack follows the picture**
>
> AdLoop: brief → storyboard → film → score, in one loop. Thank you."

---

## 60-second version (if they cut you short)

> "Ad creative takes weeks, and AI tools are disconnected prompt boxes that forget the last step. AdLoop turns one line of brief into a finished, scored film ad.
> Gemini 3.8 Flash plans the campaign. Nano Banana 2 Lite drafts four variants of every scene in parallel against a continuity anchor. A Flash judge picks and repairs the best. Omni Flash animates each winner the moment it's picked, with no waiting for the other scenes. Lyria scores it from the scene plan. Then you direct the whole ad in one sentence ('make it a monsoon evening') and every clip, plus the music, updates in parallel.
> `[time to final]` from brief to finished film. That's AdLoop."

---

## Likely judge questions (and honest answers)

| Question | Answer |
|---|---|
| **How do you keep the same character and product across scenes?** | A continuity anchor frame from NB2, passed as a reference image into every storyboard call, plus a brand bible (palette, style, typography) in every prompt. Repairs and localizations are *edits* with the base image first. Omni animates the exact winning keyframe. |
| **Why a judge? Isn't that slow?** | It's the opposite: it only works *because* NB2 Lite is fast. We draft K per scene, keep one and repair if needed. It runs per scene, so it never blocks the other scenes. |
| **What makes the edits "conversational"?** | Every clip keeps its Interactions API `interaction_id`. The next instruction is a new turn with `previous_interaction_id`, so edits stack. If a multi-turn edit fails, it falls back to a single-turn edit with the clip bytes, then to a re-render from the keyframe. The version pill shows which one happened. |
| **What if a model call fails mid-run?** | One adapter handles all Google calls: it tries ordered API paths, shrinks the request on 400s, backs off on 429/5xx, and remembers what worked per model. Failures become events, never crashes. A failed scene doesn't block the cut; the film stitches from what finished. |
| **Is this real or mocked?** | The LIVE badge comes from `/api/health`, and every tile shows which API path produced it. Mock mode exists for offline development and uses the same pipeline and UI. |
| **How long and how much per ad?** | `[time to final]` for 4 scenes × 4 variants. That's about `[16 + repairs]` images, `[4]` Omni clips and `[1]` Lyria track. Quote costs only if you've calculated them. |
| **Why is the music good for *this* ad?** | Lyria gets a timed prompt built from the scene plan (durations, moods, energy per scene) and re-scores when an edit changes a scene's mood. |
| **Does it do voiceover / narration?** | Yes: Flash TTS voices one line per scene (hook → benefit → CTA), placed on each scene's timecode with the music ducked underneath, plus captions. *(Only say this if your demo run's final cut actually has narration.)* |
| **How does it scale?** | State is event-sourced: every run is an append-only event log plus assets, which is also what powers replay. Today it's a single instance on local disk; scaling out means moving the log and assets to shared storage, not a rewrite. |
| **What's next?** | Lip-synced talent, brand-kit memory across campaigns, export to ad platforms, and learning the judge rubric from user overrides. |

---

## Delivery tips

- **Talk over the loading.** The live run takes minutes. Never wait in silence; the script above is paced so Tab B keeps moving while you talk, and Tab A holds the finished "wow".
- **Point, don't read.** Each script line maps to a UI element lighting up. Rehearse it twice with a real run so you know the rough timing of each step on your key.
- **One hero moment:** "make it a monsoon evening". Say it slowly, then stop talking for two seconds while the clips flip to rendering.
- **Be precise about models.** Nano Banana 2 Lite = storyboard, Omni Flash = motion and edits, Lyria 3.5 = score, Gemini 3.8 Flash = director and judge. Judges score "load-bearing use" of the focus models.
