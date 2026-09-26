"""Creative direction for AdMate: prompts, JSON schemas and high-level model calls.

This module is where output *quality* is won. It never touches the Google SDK —
every call goes through :class:`app.genai_client.GenMedia` — and it owns:

* **JSON schemas** (module constants, plain JSON-Schema dicts using only
  ``type/properties/required/items/enum``) for the Plan, Judgement,
  DirectionPlan, clip-edit interpretation and LocalizePlan contracts —
  including the narrator ``voice`` and per-scene ``voiceover`` lines (§9b).
* **System prompts** for the creative director, the vision judge, the one-line
  "direct the whole ad" planner, the per-clip edit interpreter and the
  localizer.
* **Async creative functions** (:func:`plan_campaign`, :func:`judge_scene`,
  :func:`plan_direction`, :func:`interpret_clip_edit`, :func:`localize_plan`)
  which call the model and then *normalize* the result so downstream code can
  rely on exact keys, ids, ranges and lengths even when the model drifts.
* **Prompt builders** (:func:`scene_image_prompt`, :func:`scene_motion_prompt`,
  :func:`music_prompt`) that turn a plan into self-contained, continuity-locked
  prompts for Nano Banana 2 Lite, Omni Flash and Lyria.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app import mock as mockgen
from app.config import settings
from app.genai_client import TTS_VOICES, GenMedia, GenResult, img_part

# ─────────────────────────────────────────────────────────────────────────────
# JSON schemas (kept deliberately simple: type / properties / required / items / enum)
# ─────────────────────────────────────────────────────────────────────────────

BEATS = ["hook", "build", "reveal", "cta"]


def _obj(props: dict, required: list[str] | None = None) -> dict:
    """Shorthand for an object schema whose properties are all required by default."""
    return {"type": "object", "properties": props, "required": required if required is not None else list(props)}


_STR = {"type": "string"}
_NUM = {"type": "number"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

PLAN_SCHEMA: dict = _obj({
    "campaign_name": _STR,
    "tagline": _STR,
    "cta": _STR,
    "brand": _obj({
        "name": _STR,
        "palette": {"type": "array", "items": _STR},
        "visual_style": _STR,
        "mood": _STR,
        "typography": _STR,
        "product": _STR,
        "hero": _STR,
    }),
    "anchor_prompt": _STR,
    "scenes": {"type": "array", "items": _obj({
        "id": _STR,
        "title": _STR,
        "beat": {"type": "string", "enum": BEATS},
        "duration_s": _INT,
        "image_prompt": _STR,
        "motion_prompt": _STR,
        "camera": _STR,
        "mood": _STR,
        "energy": _NUM,
        "on_screen_text": _STR,
        "voiceover": _STR,
    })},
    "music": _obj({
        "genre": _STR,
        "bpm": _INT,
        "key": _STR,
        "instruments": {"type": "array", "items": _STR},
        "arc": _STR,
    }),
    "voice": _obj({"name": {"type": "string", "enum": list(TTS_VOICES)}, "style": _STR}),
})

JUDGE_SCHEMA: dict = _obj({
    "scores": {"type": "array", "items": _obj({
        "index": _INT,
        "brief_fit": _NUM,
        "brand_consistency": _NUM,
        "composition": _NUM,
        "continuity": _NUM,
        "artifact_free": _NUM,
        "overall": _NUM,
        "notes": _STR,
    })},
    "winner_index": _INT,
    "rationale": _STR,
    "fix_instructions": _STR,
})

DIRECTION_SCHEMA: dict = _obj({
    "summary": _STR,
    "scene_edits": {"type": "array", "items": _obj({"scene_id": _STR, "omni_instruction": _STR})},
    "restyle_keyframes": _BOOL,
    "music": _obj({"rescore": _BOOL, "instruction": _STR}),
    "mood_updates": {"type": "array", "items": _obj({"scene_id": _STR, "mood": _STR, "energy": _NUM})},
    "voiceover_updates": {"type": "array", "items": _obj({"scene_id": _STR, "text": _STR})},
    "voice_style": _STR,
})

CLIP_EDIT_SCHEMA: dict = _obj({
    "mood": _STR,
    "energy": _NUM,
    "mood_changed": _BOOL,
    "omni_instruction": _STR,
})

LOCALIZE_SCHEMA: dict = _obj({
    "market": _STR,
    "language": _STR,
    "tagline": _STR,
    "cta": _STR,
    "scene_edits": {"type": "array", "items": _obj({"scene_id": _STR, "nb2_instruction": _STR})},
    "music_style": _STR,
    "voiceover": {"type": "array", "items": _obj({"scene_id": _STR, "text": _STR})},
    "voice": {"type": "string", "enum": list(TTS_VOICES)},
})

#: Narrator defaults used when the model omits or garbles the voice (CONTRACT §9b).
DEFAULT_VOICE = {"name": "Kore", "style": "warm, confident, upbeat narrator"}

#: Judge rubric weights used to (re)compute ``overall`` when the model omits it.
RUBRIC_WEIGHTS = {"brief_fit": 0.25, "brand_consistency": 0.20, "composition": 0.20,
                  "continuity": 0.15, "artifact_free": 0.20}

# ─────────────────────────────────────────────────────────────────────────────
# System prompts
# ─────────────────────────────────────────────────────────────────────────────

DIRECTOR_SYSTEM = """\
You are an award-winning advertising creative director (Cannes Lions, D&AD) who writes shootable
storyboards for AI film pipelines. You turn a short brief into ONE tight, emotionally escalating spot.

Output a single JSON object matching the schema. Rules:

STORY
- Exactly the number of scenes requested, in the beat order given (hook -> build(s) -> reveal -> cta).
  hook = an arresting, curiosity-grabbing first image; build = rising stakes / sensory detail;
  reveal = the product's hero moment, the biggest visual; cta = a clean end card-like frame with the brand promise.
- Energy (0.0–1.0) rises across the spot to peak at the reveal (0.85–1.0), then settles slightly for the cta.
- One continuous world: same hero, same product, same palette, same time-of-day logic unless the story turns.

BRAND BIBLE
- palette: 4–5 hex colours (#RRGGBB) that actually suit the brief; dominant first, then accents.
- hero: ONE specific, castable person description (age, look, hair, wardrobe) — or "none" if the product is the hero.
- product: ONE precise physical description (form, material, colours, label/markings, scale). If a product photo is
  attached, describe THAT product exactly. These two strings are pasted verbatim into every image prompt.
- typography: a concrete type direction (e.g. "condensed geometric sans, heavy, all caps").

IMAGE PROMPTS (one per scene, each fully self-contained — the image model sees nothing else)
- Write like a cinematographer: subject + action, setting, lens (e.g. 35mm, 85mm macro), camera height/angle,
  lighting (key/rim/practicals, time of day), palette hexes, texture, depth of field, and composition for the
  requested aspect ratio (16:9 = horizontal, rule of thirds, room for text on one side; 9:16 = vertical, stacked,
  subject in the upper two-thirds, text in the lower third).
- Repeat the hero and product descriptions VERBATIM in every prompt where they appear.
- Physically plausible, photoreal, no collage/split-screen, no brand logos you were not given.

ON-SCREEN TEXT
- Short (max ~5 words), only where it helps: usually the hook (a teaser) and the cta (tagline or CTA). Empty string
  elsewhere. Plain words only — no emojis, no hashtags — so it can be rendered and translated cleanly.

MOTION PROMPTS (one per scene, ~6 seconds, for an image-to-video model)
- One continuous shot: a single, physically plausible camera move (push-in, dolly, orbit, crane, handheld drift)
  plus natural subject/environment motion (steam, hair, fabric, rain, traffic, light shifts). No cuts, no morphing.
- camera: a 2–5 word label of the move (e.g. "slow low-angle orbit").

MUSIC
- A single coherent brief matching the moods: genre, bpm (integer), key (e.g. "D major"), 3–6 instruments,
  and an arc sentence mapping the music to the beats. Instrumental.

VOICEOVER (one spoken narration line per scene, read by a TTS narrator over that shot)
- Read in order, the lines tell a mini-story that SELLS: hook = a question or bold claim about the viewer's moment;
  build = the desire or the everyday problem, sensory and relatable; reveal = name the product and give 1–2
  CONCRETE benefits (taste, speed, material, feature, number — taken from the brief, never contradicting it);
  cta = the promise/tagline plus the call to action, ending with the brand name.
- Hard word budget per line: the VOICEOVER WORD BUDGET in FORMAT (it must be speakable within the shot with a
  breath to spare). Fewer, stronger words beat more words.
- Write for the ear: short sentences, contractions, vivid concrete nouns. No hashtags, emojis, URLs, quotation
  marks, stage directions or "Narrator:" labels. Complement the on-screen text rather than reading it out verbatim.
- Spoken language: English unless the brief asks for another language (then write in that language's native script).
- voice.name: the prebuilt narrator that suits the brand — Kore (firm, clear), Puck (upbeat), Charon (warm,
  informative), Fenrir (excitable), Aoede (breezy), Zephyr (bright), Leda (youthful), Orus (firm, deep).
  voice.style: a 4–10 word delivery note (e.g. "warm, confident, smiling, unhurried").

anchor_prompt: a clean continuity reference frame — the hero (if any) with the product, three-quarter view, neutral
backdrop in a light palette colour, soft even studio light, product fully visible and in sharp focus, no text.
campaign_name: 2–5 memorable words. tagline: <= 8 words. cta: 2–4 words, imperative.
"""

JUDGE_SYSTEM = """\
You are the strictest creative-QA lead at a top ad agency, judging AI-generated storyboard keyframes for ONE scene.
You receive the scene brief, an optional ANCHOR image (the campaign's continuity reference — NOT a candidate) and
candidate images labelled "Variant 0", "Variant 1", ... in order.

Score EVERY variant 0–10 (integers for the five criteria; overall with one decimal):
- brief_fit: does it show this scene's action, setting and beat purpose?
- brand_consistency: palette adherence to the given hexes, visual style, typography of any on-screen text.
- composition: readable focal point, strong framing for the aspect ratio, clean space for text, cinematic light.
- continuity: hero face/hair/wardrobe and product design match the ANCHOR (score 7 if no anchor is given).
- artifact_free: no garbled or misspelled text, no extra/malformed fingers or limbs, no melted or deformed product,
  no duplicated objects, no watermarks, no collage/borders.
overall = 0.25*brief_fit + 0.20*brand_consistency + 0.20*composition + 0.15*continuity + 0.20*artifact_free,
then apply hard caps: any misspelled/garbled/extra on-screen text -> overall <= 6.0; deformed product or anatomy ->
overall <= 5.0; required on-screen text missing -> overall <= 6.5. Be discriminating: spread scores, do not give
everything 8. A 9+ must be genuinely broadcast-ready.

winner_index = the variant with the highest overall. rationale: 1–2 sentences on why it wins.
fix_instructions: concrete, imperative edits that would make the WINNER broadcast-ready (e.g. "Correct the text to
read exactly 'Own the Monsoon'. Warm the backdrop to #C8782E. Remove the extra fingers on the left hand.").
Empty string if the winner is already 8.5 or above. notes: <= 15 words per variant, specific.
Return one JSON object matching the schema.
"""

DIRECTION_SYSTEM = """\
You are the director running the edit suite for an AI-generated commercial. The client gives ONE sentence of
direction for the whole ad. Translate it into the MINIMAL set of concrete changes across modalities:

- scene_edits: one entry per scene that must change. A global note ("make it a monsoon evening") touches every
  scene; a targeted note ("the reveal needs more drama") touches only those scenes. Each omni_instruction is an
  imperative instruction for a video-editing model working on that existing clip: state the change precisely
  (light, weather, time of day, pacing, camera, props) and what must stay identical (hero identity, product design,
  on-screen text, framing). 1–2 sentences, no scene numbers inside the text.
- restyle_keyframes: true ONLY if the change is a fundamental look change a video edit cannot deliver
  (e.g. new wardrobe, new product colourway, whole new palette); otherwise false.
- music: rescore=true if the note changes mood, energy, pacing, genre or setting; instruction = what the new score
  should do (genre/tempo/instrument/mood shifts), one sentence. rescore=false with empty instruction otherwise.
- mood_updates: new mood (2–4 words) and energy (0.0–1.0) for every scene whose feeling changes.
- voiceover_updates: the narration lines to re-voice. If the note changes WHAT is said (new offer, new benefit, a
  different CTA, a new language), rewrite the affected lines (same word budget as the current line, still ending the
  cta line with the brand name). If it changes only HOW it should sound, list every affected scene with its CURRENT
  text unchanged. Empty list when the voiceover is unaffected.
- voice_style: a new 4–10 word delivery note for the narrator when the tone changes (e.g. "make it playful" ->
  "playful, bright and bouncy, smiling"); empty string otherwise.
- summary: one short sentence the client sees, e.g. "Shifting all 4 shots to a rain-soaked evening and re-scoring
  with slower, moodier keys."
Only reference scene ids that exist. Return one JSON object matching the schema.
"""

CLIP_EDIT_SYSTEM = """\
You interpret a client's chat message about ONE video clip of a commercial.
- omni_instruction: rewrite the message as a precise, imperative instruction for a video-editing model editing this
  existing clip: the change, plus what must stay identical (hero identity, product design, on-screen text, framing
  unless the message changes it). 1–2 sentences.
- mood: the scene's mood AFTER the edit (2–4 words). energy: 0.0–1.0 after the edit.
- mood_changed: true only if the edit clearly changes the emotional tone or energy (weather, time of day, pace,
  intensity), false for purely technical changes (e.g. "slower push-in", "fix the hand").
Return one JSON object matching the schema.
"""

LOCALIZE_SYSTEM = """\
You are a senior transcreation lead adapting a finished commercial for a new market.
- language: the market's primary language name in English (e.g. "Telugu").
- tagline and cta: transcreated (not literally translated) into that language, written in its native script,
  natural and punchy, similar length to the original.
- scene_edits: one per scene. nb2_instruction is an imperative instruction for an image-EDITING model that edits the
  existing keyframe: (1) replace any on-image text with the exact transcreated words in native script (quote them),
  (2) culturally adapt setting details, signage, cast styling, wardrobe and props to the market where natural,
  (3) KEEP the composition, camera angle, lighting, palette and the product exactly identical.
  If a scene has no on-image text, do not add any.
- music_style: one sentence describing a regional variant of the soundtrack (instruments/rhythms from the region)
  that keeps the original tempo and arc.
- voiceover: one entry per scene — transcreate that scene's narration line into the market language in its NATIVE
  script (never transliterated), natural spoken register, keeping the story role of the line (hook / desire / product
  benefit / CTA with the brand name kept as-is). Keep it about as long to SAY as the original so it fits the shot.
- voice: the prebuilt narrator for this market (usually keep the original voice).
Return one JSON object matching the schema.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")
_DEFAULT_PALETTE = ["#111827", "#6366F1", "#F59E0B", "#F9FAFB", "#10B981"]


def _s(value: Any, default: str = "") -> str:
    """Coerce to a clean single-spaced string."""
    if value is None:
        return default
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text or default


def _f(value: Any, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Coerce to a float clamped to ``[lo, hi]``."""
    try:
        v = float(value)
        if v != v:  # NaN
            raise ValueError
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _clip_words(text: str, max_chars: int) -> str:
    """Trim at a word boundary to at most ``max_chars``."""
    text = text.strip().strip('"“”').strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut.rstrip(",;:-— ")


def vo_text(value: Any) -> str:
    """A scene's narration line (``value`` is a plan scene's ``voiceover`` string or run-state dict)."""
    return _s(mockgen.vo_text({"voiceover": value}))


def fit_voiceover(text: str, budget: int) -> str:
    """Trim a narration line to at most ``budget`` words, cutting at a word boundary.

    Prefers ending on a complete sentence when that keeps at least half the
    budget; otherwise hard-cuts and closes the line with a full stop so the TTS
    narrator lands it instead of trailing off mid-phrase.
    """
    text = _s(text).strip('"“”').strip()
    words = text.split()
    if len(words) <= budget:
        return text
    kept = words[:budget]
    min_keep = max(1, budget // 2)
    for i in range(len(kept) - 1, min_keep - 2, -1):
        if kept[i][-1] in ".!?。！？।":
            return " ".join(kept[: i + 1])
    return " ".join(kept).rstrip(",;:-—–… ") + "."


def normalize_voice(raw: Any, default: dict | None = None) -> dict:
    """Coerce a narrator voice to ``{"name": <prebuilt voice>, "style": str}`` (unknown names -> default)."""
    default = default or DEFAULT_VOICE
    if isinstance(raw, str):
        raw = {"name": raw}
    raw = raw if isinstance(raw, dict) else {}
    wanted = _s(raw.get("name")).lower()
    name = next((v for v in TTS_VOICES if v.lower() == wanted), default["name"])
    return {"name": name, "style": _clip_words(_s(raw.get("style"), default["style"]), 120)}


def _orientation(aspect: str) -> str:
    return "vertical" if aspect == "9:16" else "horizontal"


def _composition(aspect: str) -> str:
    """Aspect-specific composition guidance for image prompts."""
    if aspect == "9:16":
        return ("vertical 9:16 frame for mobile: subject in the upper two-thirds, stacked depth layers, "
                "clean lower third for text, nothing important in the top 8% or bottom 12% (UI safe zones)")
    return ("horizontal 16:9 cinematic frame: rule of thirds, subject on one third with breathing room on the other "
            "for text, strong foreground/background depth")


def _fmt_tc(seconds: float) -> str:
    """Seconds -> ``m:ss`` timecode."""
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _energy_word(energy: float) -> str:
    if energy < 0.35:
        return "low"
    if energy < 0.65:
        return "medium"
    if energy < 0.85:
        return "high"
    return "peak"


def _scene_index(plan: dict, scene: dict) -> tuple[int, int]:
    """(1-based position, total) of ``scene`` within the plan."""
    scenes = plan.get("scenes") or []
    for i, sc in enumerate(scenes):
        if sc.get("id") == scene.get("id"):
            return i + 1, len(scenes)
    return 1, max(1, len(scenes))


def _palette_str(plan: dict) -> str:
    return ", ".join((plan.get("brand") or {}).get("palette") or _DEFAULT_PALETTE)


def _merged_scenes(plan: dict, scenes: list[dict] | None) -> list[dict]:
    """Plan scenes overlaid with live run-state fields (mood/energy/duration may have been edited)."""
    by_id = {sc.get("id"): dict(sc) for sc in plan.get("scenes") or []}
    if not scenes:
        return list(by_id.values())
    out = []
    for sc in scenes:
        base = by_id.get(sc.get("id"), {})
        merged = {**base, **{k: v for k, v in sc.items() if v not in (None, "")}}
        out.append(merged)
    return out


def _brief_scene_view(sc: dict) -> dict:
    """Compact scene summary sent to text models (keeps prompts small and fast)."""
    view = {k: sc.get(k) for k in ("id", "title", "beat", "mood", "energy", "camera", "on_screen_text",
                                   "motion_prompt") if k in sc}
    if vo_text(sc.get("voiceover")):
        view["voiceover"] = vo_text(sc.get("voiceover"))
    return view


# ─────────────────────────────────────────────────────────────────────────────
# Normalizers (make model output contract-exact)
# ─────────────────────────────────────────────────────────────────────────────


def normalize_plan(raw: dict, *, brief: str, brand: str, aspect: str, n_scenes: int) -> dict:
    """Coerce a model plan into the exact Plan contract.

    Guarantees: exactly ``n_scenes`` scenes with ids ``s1..sN`` and the canonical
    beat sequence, integer ``duration_s`` = ``settings.video_seconds``, energies
    in [0, 1], 4–5 valid ``#RRGGBB`` palette colours, non-empty prompts, a
    prebuilt narrator ``voice`` and a non-empty ``voiceover`` line per scene that
    fits ``floor(duration_s * 2.4)`` words. Adds ``plan["aspect"]`` (extra key)
    for prompt builders.
    """
    if not isinstance(raw, dict):
        raw = {}
    if "scenes" not in raw and isinstance(raw.get("plan"), dict):
        raw = raw["plan"]
    n = max(1, int(n_scenes))
    fallback = mockgen.fake_plan(brief=brief, brand=brand, aspect=aspect, n_scenes=n)

    b = raw.get("brand") if isinstance(raw.get("brand"), dict) else {}
    palette = []
    for c in b.get("palette") or []:
        m = _HEX_RE.match(_s(c))
        if m and f"#{m.group(1).upper()}" not in palette:
            palette.append(f"#{m.group(1).upper()}")
    for c in fallback["brand"]["palette"]:
        if len(palette) >= 4:
            break
        if c not in palette:
            palette.append(c)
    brand_out = {
        "name": _s(b.get("name"), _s(brand) or fallback["brand"]["name"]),
        "palette": palette[:5],
        "visual_style": _s(b.get("visual_style"), fallback["brand"]["visual_style"]),
        "mood": _s(b.get("mood"), fallback["brand"]["mood"]),
        "typography": _s(b.get("typography"), fallback["brand"]["typography"]),
        "product": _s(b.get("product"), fallback["brand"]["product"]),
        "hero": _s(b.get("hero"), fallback["brand"]["hero"]),
    }

    beats = mockgen.beats_for(n)
    curve = mockgen._energy_curve(beats)  # noqa: SLF001 - shared canonical curve
    secs = int(settings.video_seconds)
    budget = mockgen.vo_word_budget(secs)
    fallback_lines = mockgen.fake_voiceover(brief, beats, brand_out["name"])
    raw_scenes = [s for s in (raw.get("scenes") or []) if isinstance(s, dict)]
    scenes = []
    for i in range(n):
        src = raw_scenes[i] if i < len(raw_scenes) else {}
        fb = fallback["scenes"][i]
        scenes.append({
            "id": f"s{i + 1}",
            "title": _clip_words(_s(src.get("title"), f"Scene {i + 1}"), 48),
            "beat": beats[i],
            "duration_s": secs,
            "image_prompt": _s(src.get("image_prompt"),
                               f"{brand_out['hero']} with {brand_out['product']}, {brand_out['visual_style']}"),
            "motion_prompt": _s(src.get("motion_prompt"), fb["motion_prompt"]),
            "camera": _s(src.get("camera"), fb["camera"]),
            "mood": _s(src.get("mood"), brand_out["mood"]),
            "energy": round(_f(src.get("energy"), curve[i]), 2),
            "on_screen_text": _clip_words(_s(src.get("on_screen_text")), 48),
            "voiceover": fit_voiceover(vo_text(src.get("voiceover")) or fallback_lines[i], budget),
        })

    m = raw.get("music") if isinstance(raw.get("music"), dict) else {}
    try:
        bpm = int(float(m.get("bpm")))
    except (TypeError, ValueError):
        bpm = fallback["music"]["bpm"]
    instruments = [_s(x) for x in (m.get("instruments") or []) if _s(x)]
    music = {
        "genre": _s(m.get("genre"), fallback["music"]["genre"]),
        "bpm": max(60, min(180, bpm)),
        "key": _s(m.get("key"), fallback["music"]["key"]),
        "instruments": instruments[:6] or list(fallback["music"]["instruments"]),
        "arc": _s(m.get("arc"), fallback["music"]["arc"]),
    }

    campaign = _clip_words(_s(raw.get("campaign_name"), fallback["campaign_name"]), 60)
    anchor_core = _s(raw.get("anchor_prompt"), f"{brand_out['hero']} presenting {brand_out['product']}")
    light = brand_out["palette"][3] if len(brand_out["palette"]) > 3 else brand_out["palette"][-1]
    anchor_prompt = (
        f"Continuity reference frame for the \"{campaign}\" campaign. {anchor_core}. "
        f"Hero (exact): {brand_out['hero']}. Product (exact): {brand_out['product']}. "
        f"Three-quarter view, product fully visible and in sharp focus, clean seamless backdrop in {light}, "
        f"soft even studio key light with a gentle rim, true-to-life colours from the palette "
        f"{', '.join(brand_out['palette'])}. {_composition(aspect)}. If a product photo is provided as a reference image, reproduce that exact "
        f"product faithfully. No text, no logos, no watermarks."
    )
    return {
        "campaign_name": campaign,
        "tagline": _clip_words(_s(raw.get("tagline"), fallback["tagline"]), 80),
        "cta": _clip_words(_s(raw.get("cta"), fallback["cta"]), 40),
        "brand": brand_out,
        "anchor_prompt": anchor_prompt,
        "scenes": scenes,
        "music": music,
        "voice": normalize_voice(raw.get("voice")),
        "aspect": aspect if aspect in ("16:9", "9:16") else "16:9",
    }


def normalize_judgement(raw: dict, n_variants: int) -> dict:
    """Coerce a judge response: one score row per variant (index 0..K-1), winner = best overall."""
    raw = raw if isinstance(raw, dict) else {}
    n = max(1, int(n_variants))
    rows: dict[int, dict] = {}
    for pos, row in enumerate(raw.get("scores") or []):
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("index", pos))
        except (TypeError, ValueError):
            idx = pos
        if 0 <= idx < n and idx not in rows:
            rows[idx] = row
    scores = []
    for i in range(n):
        row = rows.get(i, {})
        dims = {d: int(round(_f(row.get(d), 5.0, 0.0, 10.0))) for d in RUBRIC_WEIGHTS}
        weighted = sum(RUBRIC_WEIGHTS[d] * dims[d] for d in RUBRIC_WEIGHTS)
        overall = _f(row.get("overall"), weighted, 0.0, 10.0) if row else 0.0
        scores.append({"index": i, **dims, "overall": round(overall, 1),
                       "notes": _clip_words(_s(row.get("notes"), "" if row else "not scored"), 140)})
    try:
        model_pick = int(raw.get("winner_index"))
    except (TypeError, ValueError):
        model_pick = -1
    best = max(s["overall"] for s in scores)
    # Winner is always a top-scoring variant (keeps crown and score badges consistent); ties -> model's pick.
    winner = model_pick if 0 <= model_pick < n and scores[model_pick]["overall"] >= best else \
        max(range(n), key=lambda i: scores[i]["overall"])
    return {
        "scores": scores,
        "winner_index": winner,
        "rationale": _clip_words(_s(raw.get("rationale")), 400),
        "fix_instructions": _clip_words(_s(raw.get("fix_instructions")), 600),
    }


def normalize_direction(raw: dict, plan: dict, instruction: str, scenes: list[dict] | None = None) -> dict:
    """Coerce a DirectionPlan: only known scene ids, one edit per scene, clamped energies.

    ``scenes`` (optional) are the live scenes (plan overlaid with run state) whose
    current narration is reused when the model asks for a tone-only re-voice;
    rewritten lines are fitted to each scene's word budget. ``voice_style`` is
    ``None`` when the narrator's delivery is unchanged.
    """
    raw = raw if isinstance(raw, dict) else {}
    valid = [sc["id"] for sc in plan.get("scenes") or [] if sc.get("id")]
    live = {sc.get("id"): sc for sc in _merged_scenes(plan, scenes)}
    edits, seen = [], set()
    for e in raw.get("scene_edits") or []:
        if not isinstance(e, dict):
            continue
        sid, text = _s(e.get("scene_id")), _s(e.get("omni_instruction"))
        if sid in valid and sid not in seen and text:
            edits.append({"scene_id": sid, "omni_instruction": text})
            seen.add(sid)
    moods, mseen = [], set()
    for mu in raw.get("mood_updates") or []:
        if not isinstance(mu, dict):
            continue
        sid = _s(mu.get("scene_id"))
        if sid in valid and sid not in mseen and _s(mu.get("mood")):
            moods.append({"scene_id": sid, "mood": _clip_words(_s(mu.get("mood")), 40),
                          "energy": round(_f(mu.get("energy"), 0.5), 2)})
            mseen.add(sid)
    voice_style = _clip_words(_s(raw.get("voice_style")), 120) or None
    vo_updates, vseen = [], set()
    for vu in raw.get("voiceover_updates") or []:
        if not isinstance(vu, dict):
            continue
        sid = _s(vu.get("scene_id"))
        if sid not in valid or sid in vseen:
            continue
        sc = live.get(sid, {})
        text = _s(vu.get("text")) or vo_text(sc.get("voiceover"))
        if text:
            budget = mockgen.vo_word_budget(sc.get("duration_s") or settings.video_seconds)
            vo_updates.append({"scene_id": sid, "text": fit_voiceover(text, budget)})
            vseen.add(sid)
    if voice_style and not vo_updates:
        # A new delivery with no listed lines means "re-voice everything, same words".
        vo_updates = [{"scene_id": sid, "text": vo_text(live[sid].get("voiceover"))}
                      for sid in valid if sid in live and vo_text(live[sid].get("voiceover"))]
    music = raw.get("music") if isinstance(raw.get("music"), dict) else {}
    rescore = bool(music.get("rescore", bool(moods)))
    if not edits and not rescore and not vo_updates:
        # The model produced nothing actionable: apply the note verbatim to every clip.
        edits = [{"scene_id": sid, "omni_instruction": f"{instruction.strip()}. Keep the hero, product, on-screen "
                                                       f"text and framing identical."} for sid in valid]
    return {
        "summary": _clip_words(_s(raw.get("summary"), f"Applying “{instruction.strip()}” across the ad."), 200),
        "scene_edits": edits,
        "restyle_keyframes": bool(raw.get("restyle_keyframes", False)),
        "music": {"rescore": rescore, "instruction": _s(music.get("instruction"), instruction if rescore else "")},
        "mood_updates": moods,
        "voiceover_updates": vo_updates,
        "voice_style": voice_style,
    }


def normalize_clip_edit(raw: dict, scene: dict, instruction: str) -> dict:
    """Coerce a clip-edit interpretation into ``{mood, energy, mood_changed, omni_instruction}``."""
    raw = raw if isinstance(raw, dict) else {}
    mood = _clip_words(_s(raw.get("mood"), scene.get("mood", "")), 40)
    return {
        "mood": mood,
        "energy": round(_f(raw.get("energy"), _f(scene.get("energy"), 0.5)), 2),
        "mood_changed": bool(raw.get("mood_changed", False)) and bool(mood),
        "omni_instruction": _s(raw.get("omni_instruction"),
                               f"{instruction.strip()}. Keep the hero, product and framing identical."),
    }


def normalize_localize(raw: dict, plan: dict, market: str) -> dict:
    """Coerce a LocalizePlan: exactly one nb2 instruction and one voiceover line per plan scene.

    Missing narration falls back to the scene's original line (accurate copy beats
    generic filler); lines are capped at twice the scene's word budget because
    word counts vary across scripts. ``voice`` is always a prebuilt voice name.
    """
    raw = raw if isinstance(raw, dict) else {}
    fallback = mockgen.fake_localize(plan=plan, market=market)
    given = {}
    for e in raw.get("scene_edits") or []:
        if isinstance(e, dict) and _s(e.get("scene_id")) and _s(e.get("nb2_instruction")):
            given.setdefault(_s(e.get("scene_id")), _s(e.get("nb2_instruction")))
    edits = []
    for fb in fallback["scene_edits"]:
        sid = fb["scene_id"]
        edits.append({"scene_id": sid, "nb2_instruction": given.get(sid, fb["nb2_instruction"])})
    lines = {}
    for e in raw.get("voiceover") or []:
        if isinstance(e, dict) and _s(e.get("scene_id")) and _s(e.get("text")):
            lines.setdefault(_s(e.get("scene_id")), _s(e.get("text")))
    voiceover = []
    for sc in plan.get("scenes") or []:
        budget = 2 * mockgen.vo_word_budget(sc.get("duration_s") or settings.video_seconds)
        text = lines.get(sc.get("id")) or vo_text(sc.get("voiceover"))
        voiceover.append({"scene_id": sc.get("id"), "text": fit_voiceover(text, budget)})
    plan_voice = normalize_voice(plan.get("voice"))
    return {
        "market": market,
        "language": _s(raw.get("language"), fallback["language"]),
        "tagline": _s(raw.get("tagline"), fallback["tagline"]),
        "cta": _s(raw.get("cta"), fallback["cta"]),
        "scene_edits": edits,
        "music_style": _s(raw.get("music_style"), fallback["music_style"]),
        "voiceover": voiceover,
        "voice": normalize_voice(raw.get("voice"), plan_voice)["name"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# High-level creative calls
# ─────────────────────────────────────────────────────────────────────────────


async def plan_campaign(gm: GenMedia, *, brief: str, brand: str, aspect: str, n_scenes: int,
                        markets: list[str] | None, product_image: bytes | None) -> tuple[dict, GenResult]:
    """Brief -> full creative Plan (brand bible, N scene prompts, motion, music brief, narration)."""
    n = max(1, int(n_scenes))
    beats = mockgen.beats_for(n)
    secs = int(settings.video_seconds)
    beat_sheet = ", ".join(f"s{i + 1} = {b}" for i, b in enumerate(beats))
    parts: list[Any] = [
        f"BRIEF:\n{brief.strip()}",
        f"BRAND NAME: {brand.strip() if brand and brand.strip() else '(none given — invent a fitting, ownable name)'}",
        f"FORMAT: {aspect} ({_orientation(aspect)}), exactly {n} scenes x {secs}s = a {n * secs}s spot. "
        f"Composition: {_composition(aspect)}. VOICEOVER WORD BUDGET: at most "
        f"{mockgen.vo_word_budget(secs)} words per scene line.",
        f"BEAT SHEET (exact order): {beat_sheet}. Scene ids must be s1..s{n}; duration_s = {secs}.",
    ]
    if markets:
        parts.append("LAUNCH MARKETS (the ad will be localized later — keep on-screen text short and "
                     f"translatable, avoid culture-specific wordplay): {', '.join(markets)}")
    if product_image:
        parts.append("PRODUCT PHOTO (attached next): this is the real product. Describe it precisely in "
                     "brand.product (shape, material, colours, label) and keep it identical in every scene.")
        parts.append(img_part(product_image))
    parts.append("Return the plan JSON now.")
    ctx = {"kind": "plan", "brief": brief, "brand": brand, "aspect": aspect, "n_scenes": n,
           "markets": list(markets or [])}
    raw, res = await gm.generate_json(parts, PLAN_SCHEMA, system=DIRECTOR_SYSTEM, temperature=0.9,
                                      mock_context=ctx)
    return normalize_plan(raw, brief=brief, brand=brand, aspect=aspect, n_scenes=n), res


async def judge_scene(gm: GenMedia, *, plan: dict, scene: dict, variants: list[bytes],
                      anchor: bytes | None) -> tuple[dict, GenResult]:
    """Vision-judge tournament for one scene; ``scores[i].index`` refers to ``variants[i]``."""
    brand = plan.get("brand") or {}
    pos, total = _scene_index(plan, scene)
    text = _s(scene.get("on_screen_text"))
    brief = (
        f"CAMPAIGN: {plan.get('campaign_name', '')} — tagline \"{plan.get('tagline', '')}\".\n"
        f"SCENE {pos} of {total} ({scene.get('beat', '')} beat): \"{scene.get('title', '')}\".\n"
        f"SHOT BRIEF: {scene.get('image_prompt', '')}\n"
        f"HERO: {brand.get('hero', '')}\nPRODUCT: {brand.get('product', '')}\n"
        f"PALETTE: {_palette_str(plan)} · STYLE: {brand.get('visual_style', '')} · MOOD: {scene.get('mood', '')}\n"
        f"ASPECT: {plan.get('aspect', '16:9')}\n"
        f"REQUIRED ON-SCREEN TEXT: {('exactly ' + json.dumps(text, ensure_ascii=False)) if text else 'none — any text is an artifact'}"
    )
    parts: list[Any] = [brief]
    if anchor:
        parts += ["ANCHOR — continuity reference (NOT a candidate):", img_part(anchor)]
    for i, v in enumerate(variants):
        parts += [f"Variant {i}:", img_part(v)]
    parts.append(f"Score all {len(variants)} variants (index 0..{len(variants) - 1}) and return the JSON.")
    ctx = {"kind": "judge", "plan": plan, "scene": scene, "n_variants": len(variants)}
    raw, res = await gm.generate_json(parts, JUDGE_SCHEMA, system=JUDGE_SYSTEM, temperature=0.2,
                                      mock_context=ctx)
    return normalize_judgement(raw, len(variants)), res


async def plan_direction(gm: GenMedia, *, plan: dict, scenes_state: list[dict],
                         instruction: str) -> tuple[dict, GenResult]:
    """One sentence -> DirectionPlan fanning out to every modality (clips, score, narration)."""
    merged = _merged_scenes(plan, scenes_state)
    scenes = [_brief_scene_view(sc) for sc in merged]
    context = {
        "campaign_name": plan.get("campaign_name"), "tagline": plan.get("tagline"),
        "brand": {k: (plan.get("brand") or {}).get(k) for k in ("name", "palette", "visual_style", "mood")},
        "music": plan.get("music"), "voice": plan.get("voice"), "scenes": scenes,
        "voiceover_word_budget": mockgen.vo_word_budget(settings.video_seconds),
    }
    parts = [f"CURRENT AD:\n{json.dumps(context, ensure_ascii=False)}",
             f"CLIENT DIRECTION (one sentence for the whole ad): {instruction.strip()}",
             "Return the DirectionPlan JSON."]
    ctx = {"kind": "direction", "plan": plan, "scenes_state": scenes, "instruction": instruction}
    raw, res = await gm.generate_json(parts, DIRECTION_SCHEMA, system=DIRECTION_SYSTEM, temperature=0.6,
                                      mock_context=ctx)
    return normalize_direction(raw, plan, instruction, merged), res


async def interpret_clip_edit(gm: GenMedia, *, plan: dict, scene: dict, instruction: str) -> tuple[dict, GenResult]:
    """Per-clip chat message -> ``{mood, energy, mood_changed, omni_instruction}``."""
    view = _brief_scene_view(scene)
    parts = [f"AD: {plan.get('campaign_name', '')} ({(plan.get('brand') or {}).get('visual_style', '')})",
             f"CLIP: {json.dumps(view, ensure_ascii=False)}",
             f"CLIENT MESSAGE: {instruction.strip()}",
             "Return the JSON."]
    ctx = {"kind": "clip_edit", "plan": plan, "scene": scene, "instruction": instruction}
    raw, res = await gm.generate_json(parts, CLIP_EDIT_SCHEMA, system=CLIP_EDIT_SYSTEM, temperature=0.4,
                                      mock_context=ctx)
    return normalize_clip_edit(raw, scene, instruction), res


async def localize_plan(gm: GenMedia, *, plan: dict, market: str) -> tuple[dict, GenResult]:
    """Plan + market (e.g. "Hyderabad · Telugu") -> LocalizePlan (incl. native-script narration)."""
    scenes = [{**{k: sc.get(k) for k in ("id", "title", "beat", "on_screen_text", "image_prompt")},
               "voiceover": vo_text(sc.get("voiceover"))}
              for sc in plan.get("scenes") or []]
    context = {"campaign_name": plan.get("campaign_name"), "tagline": plan.get("tagline"), "cta": plan.get("cta"),
               "brand": plan.get("brand"), "music": plan.get("music"), "voice": plan.get("voice"),
               "scenes": scenes}
    parts = [f"ORIGINAL AD:\n{json.dumps(context, ensure_ascii=False)}",
             f"TARGET MARKET: {market}",
             "Return the LocalizePlan JSON."]
    ctx = {"kind": "localize", "plan": plan, "market": market}
    raw, res = await gm.generate_json(parts, LOCALIZE_SCHEMA, system=LOCALIZE_SYSTEM, temperature=0.5,
                                      mock_context=ctx)
    return normalize_localize(raw, plan, market), res


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders for the media models
# ─────────────────────────────────────────────────────────────────────────────

_BEAT_PURPOSE = {
    "hook": "stop the scroll with an intriguing first image",
    "build": "raise curiosity with sensory detail and momentum",
    "reveal": "the product's hero moment — the most striking frame of the ad",
    "cta": "a confident closing frame that lands the brand promise",
}


def anchor_image_prompt(plan: dict) -> str:
    """The NB2 prompt for the campaign's continuity anchor frame."""
    return plan.get("anchor_prompt") or ""


def scene_image_prompt(plan: dict, scene: dict, *, fix: str | None = None, instruction: str | None = None) -> str:
    """Full, self-contained NB2 prompt for a storyboard keyframe.

    * ``fix`` — repair round: edit the first reference image (the current winner)
      applying the judge's ``fix_instructions`` while preserving its composition.
    * ``instruction`` — a director's note (regenerate with guidance, or a
      localization ``nb2_instruction``); if the first reference is this very shot
      it is treated as an edit of it.
    Reference convention: base image first (for edits), continuity anchor next.
    """
    brand = plan.get("brand") or {}
    aspect = plan.get("aspect", "16:9")
    pos, total = _scene_index(plan, scene)
    text = _s(scene.get("on_screen_text"))
    lines: list[str] = []
    if fix:
        lines.append(
            "EDIT TASK: The first reference image is the current best take of this shot. Edit it — keep its "
            "composition, camera angle, hero pose and product placement — and change ONLY what is needed to apply "
            f"these fixes: {fix.strip()}")
    lines.append(f"Storyboard keyframe — scene {pos} of {total}: \"{scene.get('title', '')}\" "
                 f"({scene.get('beat', '')} beat: {_BEAT_PURPOSE.get(scene.get('beat', ''), 'advance the story')}) "
                 f"for the \"{plan.get('campaign_name', '')}\" ad by {brand.get('name', 'the brand')}.")
    lines.append(f"SHOT: {scene.get('image_prompt', '')}")
    lines.append(f"CAMERA & FRAMING: {scene.get('camera', 'cinematic')}; {_composition(aspect)}.")
    hero = _s(brand.get("hero"))
    if hero and hero.lower() not in ("none", "n/a", "no hero"):
        lines.append(f"HERO (identical in every scene): {hero}.")
    lines.append(f"PRODUCT (identical in every scene — same shape, colours, label and proportions): "
                 f"{brand.get('product', '')}.")
    lines.append(f"LOOK: {brand.get('visual_style', '')}. Mood: {scene.get('mood') or brand.get('mood', '')}. "
                 f"Colour palette strictly within {_palette_str(plan)} (first colour dominant, others as accents).")
    if text:
        lines.append(f"ON-SCREEN TEXT: render exactly \"{text}\" once — spelled exactly like that, in "
                     f"{brand.get('typography', 'a clean bold sans-serif')}, large, crisp and legible, placed in clean "
                     f"negative space. No other words, letters, logos or watermarks.")
    else:
        lines.append("ON-SCREEN TEXT: none — no words, letters, captions, logos or watermarks anywhere.")
    if instruction:
        lines.append(f"DIRECTOR'S NOTE (overrides the shot description where they conflict): {instruction.strip()} "
                     "If the first reference image is this same shot, treat this as an edit of it: preserve its "
                     "composition, lighting and the product; change only what the note asks.")
    lines.append("CONTINUITY: a reference image of the campaign's continuity anchor is provided — match the hero's "
                 "face, hair and wardrobe and the product's exact design from it; do not copy its plain backdrop "
                 "or pose.")
    lines.append("QUALITY: photoreal, cinematic lighting, natural anatomy (correct hands), undistorted product "
                 "geometry, one coherent full-bleed frame — no collage, split screen, borders or frames.")
    return "\n".join(lines)


def scene_motion_prompt(plan: dict, scene: dict) -> str:
    """Full Omni image-to-video prompt: camera, physics, mood/pacing and an identity lock on the keyframe."""
    brand = plan.get("brand") or {}
    secs = int(scene.get("duration_s") or settings.video_seconds)
    energy = _f(scene.get("energy"), 0.5)
    pacing = {"low": "unhurried, contemplative pacing", "medium": "steady, confident pacing",
              "high": "brisk, energetic pacing", "peak": "bold, dramatic pacing with a decisive move"}[
        _energy_word(energy)]
    return "\n".join([
        f"Animate this keyframe into ONE continuous {secs}-second shot for the \"{plan.get('campaign_name', '')}\" "
        f"ad — {scene.get('beat', '')} beat: {_BEAT_PURPOSE.get(scene.get('beat', ''), 'advance the story')}.",
        f"MOTION: {scene.get('motion_prompt', '')}",
        f"CAMERA: {scene.get('camera', 'slow push-in')} — smooth, stabilised and physically plausible; no cuts, "
        "no sudden zooms, no scene changes.",
        "PHYSICS: real-world motion — natural weight and momentum, hair and fabric respond to movement, liquids, "
        "steam, smoke and rain behave realistically, people move at real speed with natural anatomy.",
        f"MOOD & PACING: {scene.get('mood') or brand.get('mood', '')}; energy {energy:.1f}/1 — {pacing}.",
        f"IDENTITY LOCK: the first frame is the approved keyframe. Keep the hero's face and wardrobe, the "
        f"product's exact design and label, any on-screen text, and the palette ({_palette_str(plan)}) unchanged "
        "for the whole shot. Do not add new text, logos or people.",
        "AUDIO: natural ambient sound only — no music, no dialogue (the score is added separately).",
    ])


_BEAT_MUSIC = {
    "hook": "a sparse, intriguing motif that makes you lean in",
    "build": "add rhythm and layers, tension rising",
    "reveal": "full arrangement — the main hook melody at its biggest",
    "cta": "pull back to the motif and resolve",
}


def music_prompt(plan: dict, scenes: list[dict], *, total_seconds: int, reason: str | None = None,
                 instruction: str | None = None) -> str:
    """Lyria prompt with a timed section map (``m:ss–m:ss beat — mood, energy``) that resolves on the CTA."""
    music = plan.get("music") or {}
    brand = plan.get("brand") or {}
    merged = _merged_scenes(plan, scenes) or [{"id": "s1", "beat": "reveal", "mood": brand.get("mood", ""),
                                               "energy": 0.7, "duration_s": total_seconds}]
    total = max(1, int(total_seconds))
    durations = [max(1.0, float(sc.get("duration_s") or settings.video_seconds)) for sc in merged]
    scale = total / sum(durations)
    lines = [
        f"Original instrumental soundtrack for a {total}-second commercial, \"{plan.get('campaign_name', '')}\" "
        f"for {brand.get('name', 'the brand')}.",
        f"Genre: {music.get('genre', 'cinematic pop')}. Tempo: {music.get('bpm', 100)} BPM. "
        f"Key: {music.get('key', 'C major')}. Instruments: {', '.join(music.get('instruments') or ['piano', 'strings'])}.",
        f"Overall arc: {music.get('arc', 'builds to the reveal, then resolves')}. Brand mood: {brand.get('mood', '')}.",
        "Structure (change sections exactly on these timecodes):",
    ]
    t = 0.0
    cta_start = None
    for sc, dur in zip(merged, durations):
        start, end = t, min(total, t + dur * scale)
        beat = sc.get("beat", "build")
        energy = _f(sc.get("energy"), 0.5)
        if beat == "cta" and cta_start is None:
            cta_start = start
        lines.append(f"{_fmt_tc(start)}–{_fmt_tc(end)} {beat} — {sc.get('mood') or brand.get('mood', '')}, "
                     f"{_energy_word(energy)} energy ({energy:.1f}): {_BEAT_MUSIC.get(beat, 'develop the theme')}.")
        t = end
    sting_at = cta_start if cta_start is not None else max(0.0, total - 3)
    lines.append(f"Ending: land a clean, resolved sting on the tonic at {_fmt_tc(sting_at + 1)} as the call to "
                 f"action appears, then let it ring out naturally to {_fmt_tc(total)} — no abrupt cut, no fade "
                 "mid-phrase.")
    lines.append("Instrumental only — no vocals, no lyrics, no spoken word. Broadcast-quality mix, clear low end.")
    if reason:
        lines.append(f"Why this re-score: {reason.strip()}.")
    if instruction:
        lines.append(f"Director's note for the score (takes priority): {instruction.strip()}")
    lines.append(f"Total length: {total} seconds.")
    return "\n".join(lines)
