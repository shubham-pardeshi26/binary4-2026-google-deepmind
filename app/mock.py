"""Deterministic synthetic payloads for AdLoop's offline mock mode.

Mock mode (``ADLOOP_MOCK=1`` or no API key) must exercise the *entire* studio —
plan, storyboard fan-out, judge + repair rounds, video, edits, soundtrack and
localization — without any network. This module provides:

* JSON builders keyed off the brief (:func:`fake_plan`, :func:`fake_judgement`,
  :func:`fake_direction`, :func:`fake_clip_edit`, :func:`fake_localize`) and the
  dispatcher :func:`build_json` used by ``GenMedia.generate_json`` in mock mode.
* Media synthesis: :func:`render_image` (Pillow keyframes in the plan palette,
  visually distinct per variant), :func:`tint_image` (a visible "edit"), and
  :func:`synth_music` (pure-python chord-progression WAV, 44.1 kHz mono 16-bit).

Everything is deterministic for a given input (hash-seeded), except the judge,
which deliberately scores scene ``s2`` below threshold on its first round so the
self-repair loop is visible in demos and tests.
"""

from __future__ import annotations

import hashlib
import io
import math
import random
import re
import sys
import threading
import wave
from array import array
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ─────────────────────────────────────────────────────────────────────────────
# Themes (brief keyword -> art direction)
# ─────────────────────────────────────────────────────────────────────────────

_THEMES: list[dict[str, Any]] = [
    {
        "match": r"chai|tea|caf[eé]|coffee|bakery|biscuit|irani",
        "palette": ["#3B2416", "#C8782E", "#F2C14E", "#F7EBD9", "#1F6F5C"],
        "visual_style": "warm cinematic street-food realism, shallow depth of field, soft haze, 35mm film grain",
        "mood": "nostalgic, warm, communal",
        "typography": "rounded serif display with hand-painted signboard accents",
        "product": "a small clear glass of saffron-tinted Irani chai with a thin foam line, resting on a white "
                   "saucer beside two golden Osmania biscuits",
        "hero": "a smiling café owner in his fifties, trimmed grey beard, crisp white kurta, sleeves rolled",
        "titles": {"hook": "Steam at First Light", "build": "The Old City Wakes", "reveal": "The Perfect Pour",
                   "cta": "Your Table Is Waiting"},
        "tagline": "Every sip, a homecoming.",
        "cta": "Visit us today",
        "music": {"genre": "cinematic Indian lo-fi with sitar and warm Rhodes", "bpm": 88, "key": "D major",
                  "instruments": ["sitar", "Rhodes piano", "tabla", "soft strings", "shaker"]},
    },
    {
        "match": r"\bev\b|electric|scooter|e-bike|bike|commut|battery|charge",
        "palette": ["#0B1320", "#00D1B2", "#7C4DFF", "#F5F7FA", "#FF5A5F"],
        "visual_style": "sleek neon-lit urban tech commercial, crisp reflections, anamorphic flares",
        "mood": "confident, kinetic, youthful",
        "typography": "geometric grotesk, bold uppercase, tight tracking",
        "product": "a matte-white electric scooter with a teal light strip along the deck and a round LED headlamp",
        "hero": "a Gen-Z rider in her early twenties, short curly hair, oversized mint bomber jacket, white sneakers",
        "titles": {"hook": "Gridlock Blues", "build": "Slip Through the City", "reveal": "Silent Power",
                   "cta": "Ride the Future"},
        "tagline": "Silent. Swift. Yours.",
        "cta": "Book a test ride",
        "music": {"genre": "future-bass electronic", "bpm": 118, "key": "F minor",
                  "instruments": ["analog synth bass", "plucked synths", "808 drums", "vocal chops (wordless)"]},
    },
    {
        "match": r"sneaker|shoe|monsoon|rain|footwear|kicks|drop",
        "palette": ["#0E1A2B", "#2E6F95", "#9AD1D4", "#F4F1DE", "#E07A5F"],
        "visual_style": "moody monsoon street-style editorial, wet reflective asphalt, backlit rain droplets",
        "mood": "bold, rebellious, rain-charged",
        "typography": "condensed sans-serif, heavy weight, water-streaked texture",
        "product": "a high-top waterproof sneaker in slate blue with a coral heel tab and a translucent gum sole",
        "hero": "a street dancer in his twenties, shaved sides, charcoal rain shell, silver chain",
        "titles": {"hook": "First Drop", "build": "Puddle Rhythm", "reveal": "Built for the Storm",
                   "cta": "Own the Monsoon"},
        "tagline": "Made for the downpour.",
        "cta": "Shop the drop",
        "music": {"genre": "dark hip-hop with rain textures", "bpm": 92, "key": "A minor",
                  "instruments": ["sub bass", "trap hats", "piano stabs", "rain foley pads"]},
    },
]

_DEFAULT_THEME: dict[str, Any] = {
    "palette": ["#111827", "#6366F1", "#F59E0B", "#F9FAFB", "#10B981"],
    "visual_style": "premium cinematic product commercial, clean studio light, glossy reflections",
    "mood": "aspirational, optimistic, modern",
    "typography": "modern humanist sans-serif, generous spacing",
    "product": "the brand's hero product, centered, pristine, with a soft rim light",
    "hero": "a warm, relatable protagonist in their late twenties in smart-casual clothing",
    "titles": {"hook": "A Familiar Moment", "build": "Something Changes", "reveal": "Meet the Difference",
               "cta": "Make It Yours"},
    "tagline": "Made for moments that matter.",
    "cta": "Discover more",
    "music": {"genre": "uplifting cinematic pop", "bpm": 104, "key": "C major",
              "instruments": ["piano", "strings", "claps", "warm synth pad"]},
}

_BUILD_TITLES = ["Momentum Builds", "Closer Now", "The Details"]

#: Canonical beat sequence for n scenes (hook -> build(s) -> reveal -> cta).
def beats_for(n: int) -> list[str]:
    """Beat sequence for ``n`` scenes: 3 -> hook/reveal/cta, 4 -> +build, >4 -> extra builds."""
    n = max(1, int(n))
    if n == 1:
        return ["reveal"]
    if n == 2:
        return ["hook", "cta"]
    if n == 3:
        return ["hook", "reveal", "cta"]
    return ["hook"] + ["build"] * (n - 3) + ["reveal", "cta"]


def _theme(brief: str) -> dict[str, Any]:
    text = (brief or "").lower()
    for theme in _THEMES:
        if re.search(theme["match"], text):
            return theme
    return _DEFAULT_THEME


def _h(*parts: Any) -> int:
    """Stable 32-bit hash of ``parts`` (Python's hash() is salted per process)."""
    return int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def prompt_key(prompt: str) -> str:
    """Short stable key for a prompt (used to number repeated variants)."""
    return hashlib.sha1((prompt or "").encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# JSON builders
# ─────────────────────────────────────────────────────────────────────────────


def fake_plan(*, brief: str, brand: str = "", aspect: str = "16:9", n_scenes: int = 4,
              markets: list[str] | None = None, **_: Any) -> dict:
    """A complete, contract-shaped Plan themed off keywords in ``brief``."""
    theme = _theme(brief)
    beats = beats_for(n_scenes)
    name = (brand or "").strip() or _brand_from_brief(brief)
    energies = _energy_curve(beats)
    framing = "wide 16:9 frame, subject on the left third" if aspect == "16:9" else \
        "tall 9:16 frame, subject centered in the upper two-thirds"
    scenes = []
    build_i = 0
    for i, beat in enumerate(beats):
        title = theme["titles"][beat]
        if beat == "build" and build_i > 0:
            title = _BUILD_TITLES[(build_i - 1) % len(_BUILD_TITLES)]
        build_i += beat == "build"
        cam = {"hook": "slow push-in", "build": "lateral tracking dolly", "reveal": "low-angle orbit",
               "cta": "gentle pull-back"}[beat]
        text = theme["tagline"] if beat == "hook" else (theme["cta"] if beat == "cta" else "")
        scenes.append({
            "id": f"s{i + 1}",
            "title": title,
            "beat": beat,
            "duration_s": 6,
            "image_prompt": (f"{title}: {theme['hero']} with {theme['product']}; {theme['visual_style']}; "
                             f"palette {', '.join(theme['palette'][:3])}; {framing}."),
            "motion_prompt": f"{cam} over six seconds while steam and ambient particles drift naturally.",
            "camera": cam,
            "mood": theme["mood"].split(",")[min(i, 2) % 3].strip() or theme["mood"],
            "energy": energies[i],
            "on_screen_text": text,
        })
    return {
        "campaign_name": f"{name} — {theme['titles']['reveal']}",
        "tagline": theme["tagline"],
        "cta": theme["cta"],
        "brand": {
            "name": name,
            "palette": list(theme["palette"]),
            "visual_style": theme["visual_style"],
            "mood": theme["mood"],
            "typography": theme["typography"],
            "product": theme["product"],
            "hero": theme["hero"],
        },
        "anchor_prompt": (f"Continuity reference frame: {theme['hero']} holding {theme['product']}, neutral "
                          f"backdrop in {theme['palette'][3]}, soft even key light, full product visible."),
        "scenes": scenes,
        "music": {**theme["music"], "arc": "sparse intrigue -> rhythmic build -> full-bodied reveal -> "
                                           "resolved brand sting"},
    }


def _brand_from_brief(brief: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z']+", brief or "") if len(w) > 3]
    return " ".join(w.capitalize() for w in words[:2]) or "AdLoop Brand"


def _energy_curve(beats: list[str]) -> list[float]:
    """Energy rises to the reveal, then settles slightly for the CTA."""
    n = len(beats)
    out = []
    for i, beat in enumerate(beats):
        if beat == "reveal":
            out.append(0.95)
        elif beat == "cta":
            out.append(0.75)
        else:
            out.append(round(0.3 + 0.5 * i / max(1, n - 2), 2))
    return out


#: Per-process count of judge calls per (campaign, scene) so repairs score higher.
_JUDGE_CALLS: dict[str, int] = {}
_JUDGE_LOCK = threading.Lock()


def fake_judgement(*, plan: dict | None = None, scene: dict | None = None, n_variants: int = 4,
                   **_: Any) -> dict:
    """Varied rubric scores. Scene ``s2``'s first round stays below 7.0 to trigger a repair."""
    scene = scene or {}
    sid = scene.get("id", "s1")
    key = f"{(plan or {}).get('campaign_name', '')}|{sid}"
    with _JUDGE_LOCK:
        round_no = _JUDGE_CALLS.get(key, 0)
        _JUDGE_CALLS[key] = round_no + 1
    rng = random.Random(_h(key, round_no))
    ceiling = 6.6 if (sid == "s2" and round_no == 0) else 9.3
    n = max(1, int(n_variants))
    favourite = rng.randrange(n) if round_no == 0 else n - 1  # repairs (appended last) usually win
    scores = []
    for i in range(n):
        base = ceiling - (0 if i == favourite else rng.uniform(0.6, 2.4))
        dims = {d: int(max(1, min(10, round(base + rng.uniform(-0.9, 0.9)))))
                for d in ("brief_fit", "brand_consistency", "composition", "continuity", "artifact_free")}
        overall = round(max(1.0, min(10.0, base)), 1)
        note = rng.choice(["strong silhouette and clean negative space", "palette slightly off on the backdrop",
                           "hero expression reads well", "product label a little soft",
                           "great light, busy background", "text crisp and correctly spelled"])
        scores.append({"index": i, **dims, "overall": overall, "notes": note})
    winner = max(range(n), key=lambda i: scores[i]["overall"])
    below = scores[winner]["overall"] < 7.0
    return {
        "scores": scores,
        "winner_index": winner,
        "rationale": (f"Variant {winner} has the clearest read of the {scene.get('beat', 'scene')} beat and the "
                      f"most faithful product rendering."),
        "fix_instructions": ("Warm the key light by a stop, shift the backdrop toward the brand palette, sharpen the "
                             "product label and remove the stray background figure." if below else ""),
    }


_MOOD_RULES = [
    (r"rain|monsoon|storm|drizzle", "rain-soaked, intimate", 0.45),
    (r"night|neon|noir|moody|dark", "moody, nocturnal", 0.4),
    (r"golden|sunset|sunrise|warm", "warm golden-hour glow", 0.55),
    (r"fast|energetic|hype|punchy|epic|intense", "high-energy, kinetic", 0.9),
    (r"calm|slow|gentle|dreamy|soft", "calm, dreamy", 0.3),
    (r"happy|joy|playful|fun|festive", "joyful, festive", 0.75),
]


def _mood_for(instruction: str) -> tuple[str, float] | None:
    text = (instruction or "").lower()
    for pattern, mood, energy in _MOOD_RULES:
        if re.search(pattern, text):
            return mood, energy
    return None


def fake_direction(*, plan: dict | None = None, instruction: str = "", scenes_state: list | None = None,
                   **_: Any) -> dict:
    """One sentence -> an Omni edit for every scene + a music re-score (+ mood updates)."""
    plan = plan or {}
    scenes = scenes_state or plan.get("scenes") or []
    instr = (instruction or "").strip().rstrip(".") or "make it more cinematic"
    mood = _mood_for(instr)
    edits, moods = [], []
    for sc in scenes:
        sid = sc.get("id")
        if not sid:
            continue
        edits.append({"scene_id": sid, "omni_instruction":
                      f"{instr[0].upper() + instr[1:]} in this shot ('{sc.get('title', sid)}'); keep the hero, "
                      f"product and camera move identical."})
        if mood:
            energy = mood[1] + (0.15 if sc.get("beat") == "reveal" else 0.0)
            moods.append({"scene_id": sid, "mood": mood[0], "energy": round(min(1.0, energy), 2)})
    return {
        "summary": f"Applying “{instr}” across {len(edits)} shots and re-scoring the soundtrack to match.",
        "scene_edits": edits,
        "restyle_keyframes": bool(re.search(r"restyle|palette|colou?r grade|look", instr.lower())),
        "music": {"rescore": True, "instruction": f"Re-score to feel {mood[0] if mood else instr}."},
        "mood_updates": moods,
    }


def fake_clip_edit(*, scene: dict | None = None, instruction: str = "", **_: Any) -> dict:
    """Interpret a per-clip chat instruction (mood change detection + Omni instruction)."""
    scene = scene or {}
    mood = _mood_for(instruction)
    instr = (instruction or "").strip().rstrip(".")
    return {
        "mood": mood[0] if mood else scene.get("mood", ""),
        "energy": mood[1] if mood else float(scene.get("energy", 0.5) or 0.5),
        "mood_changed": bool(mood),
        "omni_instruction": f"{instr}. Keep the hero, product, framing and timing identical.",
    }


_LANGS: dict[str, dict[str, str]] = {
    "telugu": {"cta": "ఇప్పుడే సందర్శించండి", "prefix": "ప్రతి క్షణం", "music": "Telugu folk percussion with "
               "nadaswaram accents"},
    "hindi": {"cta": "आज ही आइए", "prefix": "हर पल", "music": "Bollywood-lounge strings with dholak groove"},
    "tamil": {"cta": "இன்றே வாருங்கள்", "prefix": "ஒவ்வொரு கணமும்", "music": "Carnatic violin over a kuthu beat"},
    "japanese": {"cta": "今すぐチェック", "prefix": "毎日に", "music": "city-pop with koto flourishes"},
    "english": {"cta": "", "prefix": "", "music": "polished indie-pop"},
    "spanish": {"cta": "Descúbrelo hoy", "prefix": "Cada momento", "music": "Latin pop with nylon guitar"},
}


def fake_localize(*, plan: dict | None = None, market: str = "", **_: Any) -> dict:
    """A LocalizePlan: translated tagline/CTA + per-scene NB2 edit instructions."""
    plan = plan or {}
    city, _, lang = (market or "").partition("·")
    city, lang = city.strip() or market, (lang.strip() or "English")
    table = _LANGS.get(lang.lower(), {"cta": plan.get("cta", ""), "prefix": "", "music": f"{lang} pop"})
    tagline = plan.get("tagline", "")
    loc_tagline = f"{table['prefix']} — {tagline}" if table["prefix"] else tagline
    edits = []
    for sc in plan.get("scenes", []):
        text = sc.get("on_screen_text") or ""
        edits.append({"scene_id": sc.get("id"), "nb2_instruction":
                      (f"Re-render the on-image text in {lang} script" + (f" (was '{text}')" if text else "") +
                       f"; adapt the setting, wardrobe and props to {city}; keep composition, lighting, hero pose "
                       f"and the product exactly as they are.")})
    return {
        "market": market,
        "language": lang,
        "tagline": loc_tagline,
        "cta": table["cta"] or plan.get("cta", ""),
        "scene_edits": edits,
        "music_style": table["music"],
    }


def detect_kind(schema: dict | None) -> str:
    """Infer which fake payload a JSON schema wants (used when no mock_context is given)."""
    req = set((schema or {}).get("required") or []) | set(((schema or {}).get("properties") or {}).keys())
    if {"campaign_name", "scenes"} <= req:
        return "plan"
    if "winner_index" in req:
        return "judge"
    if "restyle_keyframes" in req or ("scene_edits" in req and "summary" in req):
        return "direction"
    if "mood_changed" in req:
        return "clip_edit"
    if "language" in req:
        return "localize"
    return "unknown"


def build_json(kind: str, ctx: dict, parts: list | None = None) -> dict:
    """Dispatch to the right fake builder; unknown kinds get an empty object."""
    ctx = dict(ctx or {})
    if kind == "judge" and "n_variants" not in ctx:
        ctx["n_variants"] = sum(1 for p in parts or [] if isinstance(p, str) and p.startswith("Variant ")) or 4
    if kind == "plan" and "brief" not in ctx:
        ctx["brief"] = " ".join(p for p in parts or [] if isinstance(p, str))[:400]
    builders = {"plan": fake_plan, "judge": fake_judgement, "direction": fake_direction,
                "clip_edit": fake_clip_edit, "localize": fake_localize}
    ctx.pop("kind", None)
    return builders[kind](**ctx) if kind in builders else {}


_TRANSCRIPTS = [
    "Launch a warm, cinematic ad for an Irani chai café opening in Hyderabad's Old City — steaming glasses, "
    "Osmania biscuits and golden morning light.",
    "An EV scooter for Gen-Z commuters: slipping through gridlock, silent and fast, with a bold neon look.",
    "A monsoon sneaker drop — rain-soaked streets, puddle splashes in slow motion, confident street-dance energy.",
]


def fake_transcript(audio: bytes) -> str:
    """Pick one of three sample briefs deterministically from the audio bytes."""
    return _TRANSCRIPTS[_h(len(audio or b""), (audio or b"")[:64]) % len(_TRANSCRIPTS)]


# ─────────────────────────────────────────────────────────────────────────────
# Image synthesis
# ─────────────────────────────────────────────────────────────────────────────

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def frame_size(aspect: str) -> tuple[int, int]:
    """Mock keyframe size (even dimensions, matches the ffmpeg normalization target)."""
    return (720, 1280) if aspect == "9:16" else (1280, 720)


def _palette_from(prompt: str) -> list[tuple[int, int, int]]:
    found = re.findall(r"#[0-9A-Fa-f]{6}\b", prompt or "")
    uniq = list(dict.fromkeys(c.upper() for c in found))
    if len(uniq) >= 2:
        return [_hex(c) for c in uniq[:5]]
    rng = random.Random(_h(prompt))
    base = rng.random()
    out = []
    for i in range(4):
        hue = (base + i * 0.18) % 1.0
        out.append(tuple(int(255 * v) for v in _hsv(hue, 0.55, 0.35 + 0.15 * i)))
    return out


def _hsv(h: float, s: float, v: float) -> tuple[float, float, float]:
    import colorsys
    return colorsys.hsv_to_rgb(h, s, v)


def _title_from(prompt: str) -> str:
    if re.search(r"continuity reference frame", prompt or "", re.I):
        return "Continuity anchor"
    m = re.search(r'scene \d+ of \d+:\s*"([^"]+)"', prompt or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r"^([^:.\n]{3,48})[:.]", (prompt or "").strip())
    return m.group(1).strip() if m else "Keyframe"


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def render_image(prompt: str, *, aspect: str = "16:9", variant: int = 0, has_refs: bool = False) -> bytes:
    """A distinct, palette-true placeholder keyframe: gradient + shapes + title + ``V<k>`` + grain."""
    w, h = frame_size(aspect)
    pal = _palette_from(prompt)
    rng = random.Random(_h(prompt, variant))
    c1, c2 = pal[variant % len(pal)], pal[(variant + 1 + variant // len(pal)) % len(pal)]
    # Diagonal gradient whose direction rotates with the variant.
    angle = (variant * 67 + rng.randint(0, 40)) % 360
    side = int(math.hypot(w, h)) + 4  # rotate an oversized square so the frame is always fully covered
    grad = Image.linear_gradient("L").resize((side, side), Image.BICUBIC).rotate(angle, Image.BICUBIC)
    left, top = (side - w) // 2, (side - h) // 2
    grad = grad.crop((left, top, left + w, top + h))
    img = Image.composite(Image.new("RGB", (w, h), c2), Image.new("RGB", (w, h), c1), grad)
    # Soft "subject" shapes so variants have different compositions.
    shapes = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shapes)
    accent = pal[(variant + 2) % len(pal)]
    for i in range(3):
        r = int(min(w, h) * rng.uniform(0.12, 0.32))
        cx, cy = int(w * rng.uniform(0.2, 0.8)), int(h * rng.uniform(0.25, 0.75))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*accent, 90 - i * 20))
    shapes = shapes.filter(ImageFilter.GaussianBlur(radius=min(w, h) // 30))
    img = Image.alpha_composite(img.convert("RGBA"), shapes)
    # Typography: scene title + big variant number.
    draw = ImageDraw.Draw(img)
    title = _title_from(prompt)
    tfont = _font(max(28, w // 18))
    vfont = _font(max(60, w // 7))
    sfont = _font(max(16, w // 60))
    ink = (255, 255, 255, 235)
    draw.text((int(w * 0.06) + 3, int(h * 0.08) + 3), title, font=tfont, fill=(0, 0, 0, 120))
    draw.text((int(w * 0.06), int(h * 0.08)), title, font=tfont, fill=ink)
    vlabel = f"V{variant + 1}"
    draw.text((int(w * 0.94), int(h * 0.92)), vlabel, font=vfont, fill=(255, 255, 255, 200), anchor="rs")
    tag = "MOCK · NANO BANANA 2 LITE" + (" · ref-guided" if has_refs else "")
    if re.search(r"\bfix(es)?\b|repair", prompt or "", re.I):
        tag += " · repair"
    draw.text((int(w * 0.06), int(h * 0.92)), tag, font=sfont, fill=(255, 255, 255, 170), anchor="ls")
    # Palette swatches along the top-right.
    for i, col in enumerate(pal[:5]):
        x0 = w - int(w * 0.06) - (5 - i) * (w // 40 + 6)
        draw.rectangle([x0, int(h * 0.08), x0 + w // 40, int(h * 0.08) + w // 40], fill=(*col, 255),
                       outline=(255, 255, 255, 160))
    # Subtle film grain.
    noise = Image.effect_noise((w, h), 22).convert("RGBA")
    noise.putalpha(18)
    img = Image.alpha_composite(img, noise).convert("RGB")
    return _png(img)


_TINTS = [
    (r"rain|monsoon|storm|blue", (40, 90, 170)),
    (r"golden|sunset|warm|sunrise", (230, 150, 40)),
    (r"night|moody|dark|noir|neon", (60, 20, 90)),
    (r"green|forest|fresh", (40, 140, 90)),
]


def tint_image(image: bytes, instruction: str) -> bytes:
    """A visibly "edited" frame: colour tint keyed off the instruction + an edit caption."""
    try:
        img = Image.open(io.BytesIO(image)).convert("RGB")
    except Exception:  # noqa: BLE001 - corrupt input: fall back to a fresh render
        return render_image(instruction, variant=1)
    color = next((c for p, c in _TINTS if re.search(p, (instruction or "").lower())), None)
    if color is None:
        rng = random.Random(_h(instruction))
        color = tuple(rng.randint(60, 220) for _ in range(3))
    img = Image.blend(img, Image.new("RGB", img.size, color), 0.28)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    font = _font(max(18, w // 45))
    caption = "EDIT · " + ((instruction or "").strip()[:60] or "director's note")
    draw.rectangle([0, int(h * 0.84), w, int(h * 0.84) + max(30, h // 14)], fill=(0, 0, 0))
    draw.text((int(w * 0.04), int(h * 0.84) + 6), caption, font=font, fill=(255, 255, 255))
    return _png(img)


# ─────────────────────────────────────────────────────────────────────────────
# Music synthesis (pure python `wave`)
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 44100
_NOTE = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "F": 5, "F#": 6, "GB": 6, "G": 7,
         "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11}
_MAJOR = [0, 2, 4, 5, 7, 9, 11]
_MINOR = [0, 2, 3, 5, 7, 8, 10]


def _parse_music(prompt: str) -> tuple[int, int, bool]:
    """(bpm, root midi note, is_minor) parsed from a music prompt, with pleasant defaults."""
    bpm_m = re.search(r"(\d{2,3})\s*bpm", prompt or "", re.I)
    bpm = min(160, max(60, int(bpm_m.group(1)))) if bpm_m else 96
    key_m = re.search(r"\bkey(?: of)?:?\s*([A-G](?:#|b)?)\s*(major|minor|maj|min|m)?\b", prompt or "", re.I)
    root, minor = 57, True  # A3 minor default
    if key_m:
        root = 48 + _NOTE.get(key_m.group(1).upper(), 9)
        minor = (key_m.group(2) or "major").lower() in ("minor", "min", "m")
    return bpm, root, minor


def _freq(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12.0)


def _render_bar(chord: list[int], bass: int, bar_len: int, beat_len: int, arp_gain: float) -> list[float]:
    """One bar: sustained pad triad + plucked bass on each beat + 8th-note arpeggio."""
    out = [0.0] * bar_len
    two_pi = 2 * math.pi
    # Pad: soft triad with slow attack/release.
    attack, release = int(0.08 * SAMPLE_RATE), int(0.25 * SAMPLE_RATE)
    for note in chord:
        inc = two_pi * _freq(note) / SAMPLE_RATE
        inc2 = inc * 2.0
        for i in range(bar_len):
            env = min(1.0, i / attack, (bar_len - i) / release)
            out[i] += 0.11 * env * (math.sin(inc * i) + 0.25 * math.sin(inc2 * i))
    # Bass pluck on every beat.
    inc = two_pi * _freq(bass) / SAMPLE_RATE
    decay = 4.0 / beat_len
    for b in range(0, bar_len, beat_len):
        for j in range(min(beat_len, bar_len - b)):
            out[b + j] += 0.22 * math.exp(-decay * j) * math.sin(inc * j)
    # Arpeggio of chord tones an octave up, eighth notes.
    eighth = beat_len // 2
    arp = [chord[0] + 12, chord[1] + 12, chord[2] + 12, chord[1] + 12]
    for k, start in enumerate(range(0, bar_len, eighth)):
        inc = two_pi * _freq(arp[k % len(arp)]) / SAMPLE_RATE
        d = 7.0 / eighth
        for j in range(min(eighth, bar_len - start)):
            out[start + j] += arp_gain * 0.09 * math.exp(-d * j) * math.sin(inc * j)
    return out


def synth_music(prompt: str, seconds: int) -> bytes:
    """A pleasant I–V–vi–IV (or i–VI–III–VII) progression that ends on a tonic sting. 44.1 kHz mono WAV."""
    seconds = max(2, int(seconds))
    bpm, root, minor = _parse_music(prompt)
    scale = _MINOR if minor else _MAJOR
    degrees = [0, 5, 2, 6] if minor else [0, 4, 5, 3]
    beat_len = int(SAMPLE_RATE * 60 / bpm)
    bar_len = beat_len * 4

    def triad(deg: int) -> list[int]:
        notes = []
        for step in (0, 2, 4):
            idx = deg + step
            notes.append(root + scale[idx % 7] + 12 * (idx // 7))
        return notes

    # Pre-render each distinct bar once (quiet + full arpeggio versions) and tile them — fast in pure python.
    bars_soft = [_render_bar(triad(d), triad(d)[0] - 12, bar_len, beat_len, 0.35) for d in degrees]
    bars_full = [_render_bar(triad(d), triad(d)[0] - 12, bar_len, beat_len, 1.0) for d in degrees]
    total = seconds * SAMPLE_RATE
    sting_len = min(total // 3, int(1.8 * SAMPLE_RATE))
    body_len = total - sting_len
    mix: list[float] = []
    bar_i = 0
    while len(mix) < body_len:
        progress = len(mix) / max(1, body_len)
        src = bars_full if progress > 0.35 else bars_soft
        mix.extend(src[bar_i % len(degrees)])
        bar_i += 1
    del mix[body_len:]
    # Resolved sting on the tonic: bright chord with a long exponential decay.
    tonic = triad(0) + [root + 12]
    two_pi = 2 * math.pi
    sting = [0.0] * sting_len
    for note in tonic + [root - 12]:
        inc = two_pi * _freq(note) / SAMPLE_RATE
        for j in range(sting_len):
            sting[j] += 0.16 * math.exp(-2.2 * j / SAMPLE_RATE) * math.sin(inc * j)
    mix.extend(sting)
    # Fade in/out, normalize, write 16-bit PCM.
    fade_in = int(0.4 * SAMPLE_RATE)
    for i in range(min(fade_in, len(mix))):
        mix[i] *= i / fade_in
    tail = int(0.3 * SAMPLE_RATE)
    for i in range(tail):
        mix[-1 - i] *= i / tail
    peak = max(1e-6, max(abs(v) for v in mix))
    scale_to = 0.85 * 32767 / peak
    pcm = array("h", (int(v * scale_to) for v in mix))
    if sys.byteorder == "big":  # WAV is little-endian
        pcm.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()
