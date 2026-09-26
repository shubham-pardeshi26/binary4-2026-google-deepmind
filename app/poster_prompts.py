"""Campaign Kit prompt layer: poster formats, copywriting, NB2 poster prompts, the poster judge and localization.

Pure functions + schemas only (no I/O, no model calls) so the orchestration in :mod:`app.posters` stays small and
every prompt can be unit-tested or eyeballed in isolation.

Design principles behind the prompts (Nano Banana 2 Lite renders typography, so the prompt *is* the layout brief):

* **Exact copy in double quotes** -- every string the model must render is quoted verbatim, labelled with its role
  (headline / subline / CTA / wordmark) and followed by "no other text". Anything unquoted must never appear.
* **Format-native layout** -- each format carries its own composition recipe, typographic scale and safe zones
  (Story UI overlays, billboard 3-second read, web-banner responsive crop, print margins).
* **Brand bible** -- palette hexes are assigned *roles* (field / accent / ink, chosen by WCAG contrast) instead of
  being dumped as a list, and the plan's typography is named explicitly.
* **Continuity** -- the reference images are enumerated in order (hero keyframe, anchor, product photo) with what
  to take from each, so the poster is unmistakably the same campaign as the film.
* **Two distinct takes per format** (type-led vs image-led) so the judge has a real creative choice.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Formats
# ─────────────────────────────────────────────────────────────────────────────

#: Aspect ratios Nano Banana 2 Lite accepts (Gemini image aspect_ratio values).
SUPPORTED_ASPECTS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")

#: Ordered poster formats. ``fallbacks`` is the nearest-supported chain used if the model rejects ``aspect``.
FORMATS: list[dict[str, Any]] = [
    {
        "id": "ig_square", "label": "Instagram post", "aspect": "1:1", "fallbacks": ["1:1"],
        "canvas": "1080×1080 px social feed post",
        "use": "Seen in a fast-scrolling phone feed at thumbnail size; it must stop the thumb in half a second.",
        "layout": ("strong central focal point: hero and product fill the middle 60% of the square; headline as a "
                   "compact 1–2 line block in the top or bottom third on clean negative space; CTA as a pill button "
                   "under the headline; wordmark small in a bottom corner"),
        "safe": "keep all text at least 7% inside every edge; nothing important in the outer 4%",
        "scale": "headline cap-height ≈ 9% of the canvas height; subline ≈ 40% of the headline size",
        "subline": True,
    },
    {
        "id": "ig_story", "label": "Story / Reel cover", "aspect": "9:16", "fallbacks": ["9:16", "2:3"],
        "canvas": "1080×1920 px vertical full-screen Story / Reel cover",
        "use": "Full-screen vertical on a phone with the app's UI overlaid on top and bottom.",
        "layout": ("vertical stack: headline in the upper third (below the top safe zone), hero and product "
                   "dominating the middle, subline and CTA pill in the lower-middle above the bottom safe zone; "
                   "full-bleed image, strong vertical depth layers"),
        "safe": ("leave the top 14% and bottom 20% completely free of text and key subject detail (profile bar and "
                 "reply field overlay those areas); side margins 6%"),
        "scale": "headline cap-height ≈ 5.5% of the canvas height, set in 2–3 short stacked lines",
        "subline": True,
    },
    {
        "id": "print_poster", "label": "Print poster", "aspect": "4:5", "fallbacks": ["4:5", "3:4", "2:3"],
        "canvas": "4:5 portrait print poster (A-series / 18×24 in class), viewed at arm's length to 3 metres",
        "use": "A premium printed poster: gallery-grade image quality and classic typographic hierarchy.",
        "layout": ("classic poster grid: dominant hero image occupying the top ~65%; headline set large across the "
                   "width at the image/field boundary; subline beneath; a clean bottom signature band with the CTA "
                   "on one side and the wordmark on the other; generous negative space, everything grid-aligned"),
        "safe": "5% margin on every side (print bleed and trim); no text within the outer 5%",
        "scale": "headline cap-height ≈ 7% of the canvas height; subline ≈ 35% of the headline size",
        "subline": True,
    },
    {
        "id": "web_banner", "label": "Web hero banner", "aspect": "16:9", "fallbacks": ["16:9", "3:2"],
        "canvas": "1920×1080 px website hero banner",
        "use": "Top of a landing page on desktop; it is also centre-cropped on smaller screens.",
        "layout": ("split composition: a left-aligned text column in the left 40% (headline, subline beneath, CTA "
                   "button beneath that) on calm negative space; hero and product on the right 60%, looking or "
                   "angled toward the text; wordmark small top-left"),
        "safe": ("keep all text inside the central 90% of the width and 80% of the height so responsive crops "
                 "never cut it"),
        "scale": "headline cap-height ≈ 8% of the canvas height, max 2 lines; subline ≈ 40% of headline size",
        "subline": True,
    },
    {
        "id": "billboard", "label": "Billboard", "aspect": "21:9", "fallbacks": ["21:9", "16:9"],
        "canvas": "ultra-wide roadside billboard (48-sheet class), read from 50–150 metres",
        "use": "Read in three seconds by a passing driver: one idea, one image, huge type.",
        "layout": ("radically simple: ONE hero/product image anchored on one side, the headline set huge on the "
                   "other side on a flat, uncluttered field; wordmark next to the CTA; no subline, no small print"),
        "safe": "6% margins; no text in the outer 6%; zero clutter",
        "scale": "headline cap-height ≈ 18% of the canvas height, one line if possible",
        "subline": False,
    },
]

FORMAT_BY_ID: dict[str, dict[str, Any]] = {f["id"]: f for f in FORMATS}
FORMAT_IDS: list[str] = [f["id"] for f in FORMATS]

#: Two genuinely different creative takes, one per variant slot (idx % 2), so the judge compares real options.
TAKES = [
    ("type-led", "TYPE-LED TAKE: bold graphic poster. A solid or softly graded field in the dominant brand colour "
                 "carries the typography; the hero/product photograph sits inside the layout as a large, clean "
                 "cut-out or crisply framed image block. Swiss-grid discipline, confident negative space."),
    ("image-led", "IMAGE-LED TAKE: full-bleed cinematic photograph of the hero moment edge to edge; the typography "
                  "sits on a naturally clean area of the image (sky, wall, shadow, or a subtle brand-colour gradient "
                  "scrim) so it stays perfectly legible without a box."),
]

# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

_HEX_RE = re.compile(r"#?([0-9A-Fa-f]{6})\b")
_DEFAULT_PALETTE = ["#111827", "#6366F1", "#F59E0B", "#F9FAFB", "#10B981"]
_QUOTES = "\"'“”‘’«»「」"


def _s(value: Any, default: str = "") -> str:
    """Clean single-spaced string."""
    if value is None:
        return default
    text = " ".join((value if isinstance(value, str) else str(value)).split())
    return text or default


def clean_copy(text: Any, max_words: int, default: str = "") -> str:
    """Strip wrapping quotes, collapse whitespace and trim to ``max_words`` words (keeps the copy renderable)."""
    t = _s(text).strip(_QUOTES).strip()
    t = t.replace('"', "'")  # a double quote inside quoted copy would break the prompt's quoting
    words = t.split()
    if not words:
        return default
    if len(words) > max_words:
        t = " ".join(words[:max_words]).rstrip(",;:—–- ")
    return t


def palette(plan: dict | None) -> list[str]:
    """The plan's palette as upper-case ``#RRGGBB`` strings (defaults if missing/invalid)."""
    out: list[str] = []
    for c in ((plan or {}).get("brand") or {}).get("palette") or []:
        m = _HEX_RE.search(str(c))
        if m:
            hx = "#" + m.group(1).upper()
            if hx not in out:
                out.append(hx)
    return out or list(_DEFAULT_PALETTE)


def _lum(hx: str) -> float:
    """WCAG relative luminance of a ``#RRGGBB`` colour."""
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def ch(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _saturation(hx: str) -> float:
    """HSV saturation x value of a ``#RRGGBB`` colour (how "brand-vivid" it reads)."""
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx * mx


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two hex colours."""
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def palette_roles(plan: dict | None) -> dict[str, str]:
    """Assign brand colours to poster roles: ``field`` (dominant), ``ink`` (text on field), ``accent`` (CTA),
    ``accent_ink`` (text on the CTA). Ink/accent are picked by contrast so the typography is always legible."""
    pal = palette(plan)
    field = pal[0]
    candidates = pal[1:] + ["#FFFFFF", "#0B0B0F"]
    ink = max(candidates, key=lambda c: contrast(c, field))
    accents = [c for c in pal[1:] if c != ink] or [ink]
    # Prefer the most saturated brand colour that still separates from the field (>= 2.5:1); else max contrast.
    vivid = [c for c in accents if contrast(c, field) >= 2.5]
    accent = max(vivid, key=_saturation) if vivid else max(accents, key=lambda c: contrast(c, field))
    accent_ink = max(["#FFFFFF", "#0B0B0F", field, ink], key=lambda c: contrast(c, accent))
    return {"field": field, "ink": ink, "accent": accent, "accent_ink": accent_ink,
            "others": ", ".join(c for c in pal if c not in (field, ink, accent)) or "none"}


def brand_name(plan: dict | None) -> str:
    """Brand wordmark text."""
    plan = plan or {}
    return clean_copy((plan.get("brand") or {}).get("name") or plan.get("campaign_name", "").split("—")[0], 4)


def hero_scene(plan: dict | None, scenes: list[dict]) -> dict | None:
    """The scene whose winning keyframe anchors the posters: the ``reveal`` beat, else the highest energy."""
    with_winner = [sc for sc in scenes if sc.get("winner") is not None]
    if not with_winner:
        return None
    reveal = [sc for sc in with_winner if (sc.get("beat") or "") == "reveal"]
    if reveal:
        return reveal[0]
    return max(with_winner, key=lambda sc: float(sc.get("energy") or 0.0))


# ─────────────────────────────────────────────────────────────────────────────
# Copy (one quick Flash call)
# ─────────────────────────────────────────────────────────────────────────────

COPY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "subline": {"type": "string"},
        "cta": {"type": "string"},
        "art_direction": {"type": "object",
                          "properties": {fid: {"type": "string"} for fid in FORMAT_IDS},
                          "required": FORMAT_IDS},
    },
    "required": ["headline", "subline", "cta", "art_direction"],
}

COPY_SYSTEM = """You are the lead copywriter and art director of an award-winning agency, writing the print/social \
poster line-up for a campaign whose film is already shot. Output JSON only.

COPY RULES
- headline: at most 6 words. One sharp idea: a concrete benefit, a tension, or a vivid sensory image tied to THIS \
product and place. It may reuse or tighten the campaign tagline when that is the strongest line. No clichés \
("elevate", "unleash", "experience the", "like never before", "game-changer", "redefine"), no hashtags, no emoji, \
no exclamation marks unless the brand voice truly demands one.
- subline: at most 12 words. Supports the headline with one specific, believable reason to believe (ingredient, \
feature, place, ritual, number). Plain and confident.
- cta: at most 4 words, verb first, specific to the offer (e.g. "Pour your first cup", "Book a test ride").
- Write in the language of the brief (English unless the brief clearly uses another language). Simple words that \
render cleanly as typography: avoid rare punctuation, ampersands only if they are part of the brand name.

ART DIRECTION
- art_direction: for EVERY format id given, one line (≤ 30 words) describing how to stage the hero keyframe for \
that format: where the hero/product sit, where the type goes, what the light and background do. Be concrete and \
format-native (a billboard is not a shrunk Instagram post)."""


def copy_parts(plan: dict, scenes: list[dict] | None = None) -> list[str]:
    """Prompt parts for :data:`COPY_SCHEMA`."""
    brand = plan.get("brand") or {}
    beats = [{"beat": sc.get("beat"), "title": sc.get("title"), "on_screen_text": sc.get("on_screen_text"),
              "voiceover": sc.get("voiceover")} for sc in plan.get("scenes") or []]
    ctx = {"campaign_name": plan.get("campaign_name"), "tagline": plan.get("tagline"), "cta": plan.get("cta"),
           "brand": {k: brand.get(k) for k in ("name", "product", "hero", "visual_style", "mood", "typography")},
           "story_beats": beats}
    fmts = [{"id": f["id"], "label": f["label"], "aspect": f["aspect"], "use": f["use"]} for f in FORMATS]
    return [f"CAMPAIGN (the film is already made — the posters must feel like its key art):\n"
            f"{json.dumps(ctx, ensure_ascii=False)}",
            f"FORMATS:\n{json.dumps(fmts, ensure_ascii=False)}",
            "Write the poster copy and per-format art direction. Return the JSON."]


def fallback_copy(plan: dict) -> dict:
    """Deterministic copy derived from the plan (mock mode, or when the model returns junk)."""
    plan = plan or {}
    brand = plan.get("brand") or {}
    headline = clean_copy(plan.get("tagline"), 6) or clean_copy(plan.get("campaign_name"), 6) or brand_name(plan)
    reveal = next((sc for sc in plan.get("scenes") or [] if sc.get("beat") == "reveal"), {})
    sub_src = _s(reveal.get("voiceover")) or _s(brand.get("product"))
    sub_src = re.split(r"(?<=[.!?])\s", sub_src)[0].rstrip(".!?") if sub_src else ""
    kept: list[str] = []
    for clause in sub_src.split(","):  # whole clauses only, so the line never ends mid-thought
        if len(" ".join(kept + [clause]).split()) > 10:
            break
        kept.append(clause.strip())
    subline = clean_copy(", ".join(k for k in kept if k), 12) or clean_copy(sub_src, 12) or \
        clean_copy(f"{brand_name(plan)}, made for you", 12)
    cta = clean_copy(plan.get("cta"), 4) or "Discover more"
    moment = _s(reveal.get("title"), "the product reveal")
    art = {f["id"]: (f"Stage the film's \"{moment}\" moment as {f['label'].lower()} key art, lit and graded "
                     f"exactly like the film.") for f in FORMATS}
    return {"headline": headline, "subline": subline, "cta": cta, "art_direction": art}


def normalize_copy(raw: Any, plan: dict) -> dict:
    """Coerce the copywriter's JSON: word limits enforced, every format gets an art-direction line."""
    raw = raw if isinstance(raw, dict) else {}
    fb = fallback_copy(plan)
    art_raw = raw.get("art_direction") if isinstance(raw.get("art_direction"), dict) else {}
    art = {fid: (_s(art_raw.get(fid))[:260] or fb["art_direction"][fid]) for fid in FORMAT_IDS}
    return {"headline": clean_copy(raw.get("headline"), 6, fb["headline"]),
            "subline": clean_copy(raw.get("subline"), 12, fb["subline"]),
            "cta": clean_copy(raw.get("cta"), 4, fb["cta"]),
            "art_direction": art}


# ─────────────────────────────────────────────────────────────────────────────
# NB2 poster prompt
# ─────────────────────────────────────────────────────────────────────────────


def poster_prompt(plan: dict, copy: dict, fmt: dict, *, take: int, aspect: str, has_hero: bool,
                  has_anchor: bool, has_product: bool, instruction: str | None = None) -> str:
    """Full, self-contained Nano Banana 2 Lite prompt for one poster variant.

    The first line starts with ``"<label> campaign poster:"`` (the mock renderer uses the text before the first
    colon as the tile title).
    """
    brand = plan.get("brand") or {}
    roles = palette_roles(plan)
    name = brand_name(plan)
    typography = _s(brand.get("typography"), "a clean, bold geometric sans-serif")
    take_name, take_text = TAKES[take % len(TAKES)]
    art = _s((copy.get("art_direction") or {}).get(fmt["id"]))
    lines: list[str] = [
        f"{fmt['label']} campaign poster: finished, print-ready key art for \"{_s(plan.get('campaign_name'))}\" by "
        f"{name or 'the brand'} — {fmt['canvas']}, aspect ratio {aspect}. {fmt['use']}",
        take_text,
    ]
    refs: list[str] = []
    if has_hero:
        refs.append(f"image {len(refs) + 1} = the hero keyframe from the campaign film — keep this exact hero "
                    "(face, hair, skin tone, wardrobe), the setting's light and colour grade; re-compose it for this "
                    "format rather than cropping it")
    if has_anchor:
        refs.append(f"image {len(refs) + 1} = the continuity anchor — the canonical hero and product design")
    if has_product:
        refs.append(f"image {len(refs) + 1} = the real product photo — reproduce the product EXACTLY: same shape, "
                    "colours, label, logo placement and proportions; never redesign it")
    if refs:
        lines.append("REFERENCE IMAGES (in order): " + "; ".join(refs) + ". Use them for identity only — do not "
                     "copy any text that may appear in them.")
    hero = _s(brand.get("hero"))
    lines.append(f"SUBJECT: {hero + ' with ' if hero and hero.lower() not in ('none', 'n/a') else ''}"
                 f"{_s(brand.get('product'), 'the product')}, as the hero moment of the film. "
                 f"LOOK: {_s(brand.get('visual_style'))}; mood {_s(brand.get('mood'))}.")
    lines.append(f"LAYOUT ({fmt['label']}): {fmt['layout']}.")
    if art:
        lines.append(f"ART DIRECTION: {art}")
    lines.append(f"SAFE ZONES: {fmt['safe']}.")
    copy_lines = [f"  • HEADLINE: \"{copy['headline']}\" — the largest element; {fmt['scale']}; set in {typography}, "
                  f"heavy weight, tight but even letter-spacing, colour {roles['ink']}."]
    if fmt.get("subline", True) and copy.get("subline"):
        copy_lines.append(f"  • SUBLINE: \"{copy['subline']}\" — regular weight of the same family, clearly "
                          f"secondary, one or two lines, colour {roles['ink']} at slightly reduced emphasis.")
    copy_lines.append(f"  • CTA: \"{copy['cta']}\" — inside a solid rounded pill button filled {roles['accent']} "
                      f"with {roles['accent_ink']} text, medium-bold, generous padding.")
    if name:
        copy_lines.append(f"  • WORDMARK: \"{name}\" — small, confident brand signature in {typography}.")
    lines.append("TYPOGRAPHY — render EXACTLY these strings, each exactly once, letter-for-letter with the same "
                 "spelling, capitalisation and punctuation:\n" + "\n".join(copy_lines))
    lines.append("Text must be razor-sharp, correctly kerned, perfectly legible at thumbnail size, with contrast of "
                 "at least 4.5:1 against what sits behind it. A clear three-level hierarchy: headline → subline → "
                 "CTA. Maximum two type weights.")
    lines.append(f"BRAND PALETTE (roles): dominant field/background {roles['field']}; typography ink {roles['ink']}; "
                 f"CTA accent {roles['accent']}; supporting colours {roles['others']}. Keep the whole image inside "
                 "this palette and the film's colour grade — no off-brand colour casts.")
    lines.append("FORBIDDEN: any words, letters or numbers other than the quoted strings above; fake logos, badges, "
                 "prices, dates, URLs, hashtags, lorem ipsum, watermarks, signatures, captions, UI chrome; "
                 "printing colour codes or these instructions; borders, frames, collages, split screens, or a "
                 "mock-up of a poster on a wall / device.")
    lines.append("QUALITY: professional advertising photography and layout, photoreal hero with natural anatomy "
                 "(correct hands), undistorted product geometry, crisp focus on product and type, cinematic "
                 "lighting consistent with the film.")
    if instruction:
        lines.append(f"DIRECTOR'S NOTE (overrides the layout/look above where they conflict; never changes the "
                     f"quoted copy): {instruction.strip()}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Poster judge
# ─────────────────────────────────────────────────────────────────────────────

_NUM = {"type": "number"}
JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "scores": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "legibility": _NUM, "brand": _NUM, "composition": _NUM,
                           "impact": _NUM, "overall": _NUM, "notes": {"type": "string"}},
            "required": ["index", "legibility", "brand", "composition", "impact", "overall", "notes"]}},
        "winner_index": {"type": "integer"},
        "rationale": {"type": "string"},
    },
    "required": ["scores", "winner_index", "rationale"],
}

#: Weights used to recompute ``overall`` when the model omits it. Legibility dominates: a poster with garbled copy
#: is unusable no matter how beautiful it is.
JUDGE_WEIGHTS = {"legibility": 0.35, "brand": 0.20, "composition": 0.20, "impact": 0.25}

JUDGE_SYSTEM = """You are the executive creative director and a master typographer reviewing campaign posters \
before they go to print and paid social. You compare candidate posters for ONE format and pick the one to ship. \
Output JSON only.

Score every candidate 0–10 (decimals allowed) on:
- legibility: EVERY required string is present exactly once, spelled letter-for-letter as quoted (same words, \
capitalisation, punctuation), crisp and readable at thumbnail size. Any misspelled, garbled, duplicated, \
truncated or extra text (gibberish glyphs, fake words, stray letters, colour codes) caps legibility at 3. Missing \
the headline caps it at 2.
- brand: palette discipline, the named typography style, product rendered faithfully to the references (shape, \
label, colours), hero identity consistent with the hero keyframe.
- composition: format-native layout, safe zones respected (nothing important in the UI overlay areas of a Story, \
margins for print), a clear headline → subline → CTA hierarchy, balance and negative space.
- impact: stopping power and emotional pull; does it make you want the product in one glance?
- overall: your holistic verdict ≈ 0.35·legibility + 0.20·brand + 0.20·composition + 0.25·impact. A poster with \
broken text must never win over one with correct text.

winner_index = the candidate to ship. rationale = one crisp sentence a client would understand, naming the decisive \
difference. notes per candidate = the single most important observation (quote any misspelling you see)."""


def judge_parts(plan: dict, copy: dict, fmt: dict, aspect: str, hero: bytes | None, variants: list[bytes],
                img_part: Any) -> list:
    """Prompt parts for the poster judge: brief, hero reference, then ``Variant 0``, ``Variant 1`` ..."""
    required = [f"HEADLINE \"{copy['headline']}\""]
    if fmt.get("subline", True) and copy.get("subline"):
        required.append(f"SUBLINE \"{copy['subline']}\"")
    required.append(f"CTA \"{copy['cta']}\"")
    if brand_name(plan):
        required.append(f"WORDMARK \"{brand_name(plan)}\"")
    brand = plan.get("brand") or {}
    brief = (f"CAMPAIGN: {_s(plan.get('campaign_name'))} — brand {brand_name(plan)}.\n"
             f"FORMAT: {fmt['label']} ({aspect}) — {fmt['canvas']}. {fmt['use']}\n"
             f"LAYOUT BRIEF: {fmt['layout']}. SAFE ZONES: {fmt['safe']}.\n"
             f"REQUIRED TEXT (exact, nothing else): {'; '.join(required)}.\n"
             f"PALETTE: {', '.join(palette(plan))} · TYPOGRAPHY: {_s(brand.get('typography'))} · "
             f"PRODUCT: {_s(brand.get('product'))}")
    parts: list = [brief]
    if hero:
        parts += ["HERO KEYFRAME from the film — continuity reference (NOT a candidate):", img_part(hero)]
    for i, v in enumerate(variants):
        parts += [f"Variant {i}:", img_part(v)]
    parts.append(f"Score all {len(variants)} candidates (index 0..{len(variants) - 1}) and return the JSON.")
    return parts


def _num(v: Any, default: float) -> float:
    try:
        x = float(v)
        if x != x:
            raise ValueError
    except (TypeError, ValueError):
        x = default
    return max(0.0, min(10.0, x))


def normalize_judgement(raw: Any, n: int) -> dict:
    """Coerce the judge's JSON: one score per candidate, overall recomputed if missing and capped by legibility
    (``overall ≤ legibility + 2``) so garbled copy can never win; winner = best overall."""
    raw = raw if isinstance(raw, dict) else {}
    by_idx: dict[int, dict] = {}
    for i, s in enumerate(raw.get("scores") or []):
        if not isinstance(s, dict):
            continue
        try:
            idx = int(s.get("index", i))
        except (TypeError, ValueError):
            idx = i
        if 0 <= idx < n and idx not in by_idx:
            by_idx[idx] = s
    scores = []
    for i in range(n):
        s = by_idx.get(i, {})
        base = _num(s.get("overall"), 6.0)
        dims = {"legibility": _num(s.get("legibility", s.get("artifact_free")), base),
                "brand": _num(s.get("brand", s.get("brand_consistency")), base),
                "composition": _num(s.get("composition"), base),
                "impact": _num(s.get("impact", s.get("brief_fit")), base)}
        overall = _num(s.get("overall"), sum(dims[k] * w for k, w in JUDGE_WEIGHTS.items()))
        overall = round(min(overall, dims["legibility"] + 2.0), 1)
        scores.append({"index": i, **{k: round(v, 1) for k, v in dims.items()}, "overall": overall,
                       "notes": _s(s.get("notes"))[:240]})
    best = max(range(n), key=lambda i: (scores[i]["overall"], scores[i]["legibility"])) if n else 0
    try:
        proposed = int(raw.get("winner_index"))
    except (TypeError, ValueError):
        proposed = best
    # Trust the judge's pick unless it contradicts its own scores by a clear margin.
    winner = proposed if 0 <= proposed < n and scores[proposed]["overall"] >= scores[best]["overall"] - 0.3 \
        else best
    rationale = _s(raw.get("rationale"))[:400] or \
        f"Variant {winner} renders the copy cleanly and reads fastest in this format."
    return {"scores": scores, "winner_index": winner, "rationale": rationale}


# ─────────────────────────────────────────────────────────────────────────────
# Localization
# ─────────────────────────────────────────────────────────────────────────────

LOCALIZE_COPY_SCHEMA: dict = {
    "type": "object",
    "properties": {"headline": {"type": "string"}, "subline": {"type": "string"}, "cta": {"type": "string"},
                   "script": {"type": "string"}},
    "required": ["headline", "subline", "cta", "script"],
}

LOCALIZE_COPY_SYSTEM = """You are a senior transcreation copywriter. Adapt poster copy for a target market: \
native-sounding, culturally resonant, same intent and brand voice — not a literal translation. Write in the \
market's language using its NATIVE SCRIPT (e.g. Telugu → తెలుగు, Hindi → देवनागरी, Japanese → 日本語), keeping brand \
and product names in their original form. Keep lengths tight so the type fits the same text boxes: headline ≤ 6 \
words (or ≤ 14 characters for CJK), subline ≤ 12 words, cta ≤ 4 words. If the film's localized tagline/CTA are \
given, stay consistent with them. script = the script's name in English (e.g. "Telugu script"). Output JSON only."""


def localize_copy_parts(copy: dict, market: str, lplan: dict) -> list[str]:
    """Prompt parts for :data:`LOCALIZE_COPY_SCHEMA`."""
    ctx = {"headline": copy.get("headline"), "subline": copy.get("subline"), "cta": copy.get("cta")}
    film = {"language": lplan.get("language"), "tagline": lplan.get("tagline"), "cta": lplan.get("cta")}
    return [f"ORIGINAL POSTER COPY: {json.dumps(ctx, ensure_ascii=False)}",
            f"TARGET MARKET: {market}",
            f"THE FILM'S LOCALIZATION FOR THIS MARKET: {json.dumps(film, ensure_ascii=False)}",
            "Return the localized poster copy JSON."]


def normalize_localized_copy(raw: Any, copy: dict, lplan: dict) -> dict:
    """Coerce localized copy; falls back to the film's localized tagline/CTA, then to the original copy."""
    raw = raw if isinstance(raw, dict) else {}
    lang = _s(lplan.get("language"), "the local language")
    return {"headline": clean_copy(raw.get("headline"), 8, clean_copy(lplan.get("tagline"), 8, copy["headline"])),
            "subline": clean_copy(raw.get("subline"), 14, copy.get("subline", "")),
            "cta": clean_copy(raw.get("cta"), 5, clean_copy(lplan.get("cta"), 5, copy["cta"])),
            "script": _s(raw.get("script"), f"{lang} script")[:60],
            "language": lang}


def localize_poster_prompt(plan: dict, copy: dict, loc: dict, fmt: dict, market: str, *, has_product: bool) -> str:
    """NB2 edit prompt: re-typeset the approved poster (first reference) in the market's language and script."""
    name = brand_name(plan)
    typography = _s((plan.get("brand") or {}).get("typography"), "the same typeface style")
    swaps = [f"  • headline \"{copy['headline']}\" → \"{loc['headline']}\""]
    if fmt.get("subline", True) and copy.get("subline") and loc.get("subline"):
        swaps.append(f"  • subline \"{copy['subline']}\" → \"{loc['subline']}\"")
    swaps.append(f"  • CTA \"{copy['cta']}\" → \"{loc['cta']}\"")
    lines = [
        f"{fmt['label']} localized poster: EDIT TASK for the {market} market. The first reference image is the "
        f"approved campaign poster. Re-typeset its text in {loc['language']} using {loc['script']}; change NOTHING "
        "else.",
        "REPLACE the text exactly as follows (render the new strings letter-for-letter, correctly shaped with proper "
        "conjuncts / ligatures for the script):\n" + "\n".join(swaps),
        f"KEEP: the wordmark \"{name}\" unchanged; the same text positions, alignment, sizes, weights, colours and "
        f"hierarchy (use a high-quality native-script face that matches {typography}); if a translated line is "
        "longer, reflow it inside the same text box without shrinking the headline below the subline.",
        "KEEP PIXEL-IDENTICAL: the photograph, hero, product" + (" (matches the product photo reference)"
                                                                  if has_product else "")
        + ", layout, CTA button shape and colour, palette and aspect ratio.",
        "FORBIDDEN: leftover original-language text, mixed scripts inside one line, extra words, watermarks, "
        "transliteration in Latin letters.",
    ]
    return "\n".join(lines)
