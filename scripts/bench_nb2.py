#!/usr/bin/env python3
"""Burst benchmark for Nano Banana 2 Lite (the AdLoop storyboard model).

Fires N image generations through ``app.genai_client.GenMedia`` with at most C
in flight -- the same shape as AdLoop's per-scene storyboard fan-out -- and
reports the numbers we quote in the README / writeup:

    p50 / p95 / max latency, images per minute, time-to-first-image, errors

Each request uses a distinct prompt variation (and seed) so we measure real
generations, not a cache. ``--ref`` adds a continuity reference frame to every
call (first generates one anchor image, then threads it through the burst),
which matches the production request shape.

Per-request rows go to ``data/bench/nb2_<timestamp>.csv`` and a summary JSON
sits next to it.

Examples::

    .venv/bin/python scripts/bench_nb2.py                     # 16 images, 8 in flight
    .venv/bin/python scripts/bench_nb2.py -n 32 -c 16 --ref   # production-shaped burst
    .venv/bin/python scripts/bench_nb2.py --mock              # offline harness check
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_PROMPT = (
    "Cinematic ad keyframe for an Irani chai cafe in Hyderabad, warm morning light, "
    "teal and saffron palette, shallow depth of field, no text. Shot variation: {v}."
)
VARIATIONS = (
    "wide establishing shot of the cafe exterior", "close-up of chai being poured",
    "Osmania biscuit dipped in chai", "friends laughing at a marble table",
    "overhead flat-lay of cups and biscuits", "steam rising against a window",
    "barista pulling a kettle high", "street-side view at golden hour",
)
ANCHOR_PROMPT = (
    "Clean hero reference frame: a single glass of Irani chai on a marble table, "
    "neutral backdrop, even studio light, no text."
)


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (no numpy dependency); 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(pct / 100 * len(ordered))))
    return ordered[rank - 1]


async def bench(args: argparse.Namespace) -> dict:
    """Run the burst and return the summary dict (also writes CSV + JSON)."""
    from app.config import settings  # late import: env overrides are set in main()
    from app.genai_client import GenMedia

    gm = GenMedia(settings)
    mode = "mock" if gm.mock else "live"
    print(f"NB2 bench  model={settings.model_image}  mode={mode}  n={args.n}  concurrency={args.concurrency}"
          f"  aspect={args.aspect}  size={args.size}  ref={args.ref}")

    refs: list[bytes] = []
    if args.ref:
        anchor = await gm.generate_image(ANCHOR_PROMPT, aspect=args.aspect, size=args.size)
        refs = [anchor.data] if anchor.data else []
        print(f"anchor ready in {anchor.latency_ms} ms via {anchor.api_path}")

    sem = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []
    t_start = time.perf_counter()

    async def one(i: int) -> None:
        """One generation; never raises -- errors are recorded as rows."""
        prompt = BASE_PROMPT.format(v=VARIATIONS[i % len(VARIATIONS)]) + f" Take {i + 1}."
        async with sem:
            t_submit = time.perf_counter()
            row = {"i": i, "submit_s": round(t_submit - t_start, 3)}
            try:
                res = await gm.generate_image(prompt, refs=refs, aspect=args.aspect, size=args.size, seed=1000 + i)
                done = time.perf_counter()
                row.update(ok=True, latency_ms=int((done - t_submit) * 1000), model_latency_ms=res.latency_ms,
                           done_s=round(done - t_start, 3), bytes=len(res.data or b""), api_path=res.api_path,
                           error="")
            except Exception as exc:  # noqa: BLE001 - a failed request is a data point, not a crash
                done = time.perf_counter()
                row.update(ok=False, latency_ms=int((done - t_submit) * 1000), model_latency_ms=None,
                           done_s=round(done - t_start, 3), bytes=0, api_path=getattr(exc, "api_path", "") or "",
                           error=f"{type(exc).__name__}: {exc}"[:300])
        rows.append(row)
        mark = "ok " if row["ok"] else "ERR"
        print(f"  [{len(rows):>3}/{args.n}] {mark} #{i:<3} {row['latency_ms']:>6} ms  {row['error'][:80]}", flush=True)

    await asyncio.gather(*(one(i) for i in range(args.n)))
    wall = time.perf_counter() - t_start

    ok = [r for r in rows if r["ok"]]
    lat = [float(r["latency_ms"]) for r in ok]
    summary = {
        "model": settings.model_image, "mode": mode, "n": args.n, "concurrency": args.concurrency,
        "aspect": args.aspect, "size": args.size, "ref": args.ref,
        "ok": len(ok), "errors": len(rows) - len(ok),
        "wall_s": round(wall, 2),
        "p50_ms": round(percentile(lat, 50)), "p95_ms": round(percentile(lat, 95)),
        "max_ms": round(max(lat)) if lat else 0,
        "mean_ms": round(statistics.fmean(lat)) if lat else 0,
        "images_per_min": round(len(ok) / wall * 60, 1) if wall > 0 else 0.0,
        "time_to_first_image_ms": round(min(r["done_s"] for r in ok) * 1000) if ok else None,
        "api_paths": sorted({r["api_path"] for r in ok}),
    }

    out_dir = ROOT / "data" / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out_dir / f"nb2_{stamp}.csv"
    fields = ["i", "ok", "submit_s", "done_s", "latency_ms", "model_latency_ms", "bytes", "api_path", "error"]
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: r["i"]))
    (out_dir / f"nb2_{stamp}.summary.json").write_text(json.dumps(summary, indent=2))
    summary["csv"] = str(csv_path)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Burst benchmark for Nano Banana 2 Lite via GenMedia.")
    p.add_argument("-n", "--n", type=int, default=16, help="total generations (default 16)")
    p.add_argument("-c", "--concurrency", type=int, default=8, help="max in flight (default 8)")
    p.add_argument("--aspect", default="16:9", choices=["16:9", "9:16"])
    p.add_argument("--size", default="1K", help="512 | 1K | 2K | 4K (default 1K)")
    p.add_argument("--ref", action="store_true", help="thread a continuity anchor through every call")
    p.add_argument("--mock", action="store_true", help="force ADLOOP_MOCK=1 (offline harness check)")
    args = p.parse_args(argv)
    if args.n < 1 or args.concurrency < 1:
        p.error("-n and -c must be >= 1")
    if args.mock:
        os.environ["ADLOOP_MOCK"] = "1"
    # GenMedia's internal image semaphore would otherwise cap the burst below -c.
    os.environ["ADLOOP_IMAGE_CONCURRENCY"] = str(max(args.concurrency, int(os.getenv("ADLOOP_IMAGE_CONCURRENCY", "0") or 0)))

    try:
        s = asyncio.run(bench(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    print("\n--- NB2 burst summary " + "-" * 40)
    print(f"  ok / errors          {s['ok']} / {s['errors']}")
    print(f"  p50 / p95 / max      {s['p50_ms']} / {s['p95_ms']} / {s['max_ms']} ms")
    print(f"  throughput           {s['images_per_min']} images/min  (wall {s['wall_s']} s)")
    print(f"  time to first image  {s['time_to_first_image_ms']} ms")
    print(f"  api path(s)          {', '.join(s['api_paths']) or '-'}")
    print(f"  csv                  {s['csv']}")
    print("\nREADME/WRITEUP fill-ins: "
          f"[[NB2 p50]]={s['p50_ms'] / 1000:.2f}s [[NB2 p95]]={s['p95_ms'] / 1000:.2f}s "
          f"[[images/min]]={s['images_per_min']}")
    return 0 if s["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
