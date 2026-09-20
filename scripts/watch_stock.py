#!/usr/bin/env python3
"""No-login pickup stock watcher (Hangzhou / Ningbo / Shanghai).

Uses GET /shop/retail/pickup-message (JSON 200, no cookies). Does not touch
Safari / checkout. /shop/fulfillment-messages is HTTP 541 — do not use it.

    python3 scripts/watch_stock.py          # every 2s, print every tick
    python3 scripts/watch_stock.py 5        # slower, less likely to 541
    python3 scripts/watch_stock.py --once
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pickup_stock import ANCHORS, SKU_DEFAULT, TARGET, fetch_pickup, fmt  # noqa: E402


def beep() -> None:
    import subprocess

    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)


def main() -> int:
    once = "--once" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    interval = 0.12
    if args:
        interval = max(0.08, float(args[0]))

    from pickup_stock import load_nodes

    nodes = [n["name"] for n in load_nodes()]
    print(f"public GET /shop/retail/pickup-message  sku={SKU_DEFAULT}")
    print(f"target {list(TARGET)}  keep-alive workers  (no login, no Safari, no place-order)")
    print(f"anchors {ANCHORS}  (每枪合并锚点覆盖全 11 家)")
    print(f"nodes {nodes}")
    print("=" * 56)

    ticks = 0
    last_open = None
    while True:
        ticks += 1
        t0 = time.time()
        result = fetch_pickup(SKU_DEFAULT, base_interval=interval)
        watched = result.get("watched") or result.get("hangzhou") or []
        opened = result.get("open") or []
        wait = float(result.get("backoff") or 0.0)
        if result.get("reason") == "idle":
            if once:
                return 0 if opened else 1
            time.sleep(max(0.0, wait or 0.02))
            continue
        shots = result.get("shots") or []
        shot_txt = " ".join(
            f"{s.get('node')}={'OK' if s.get('ok') else (s.get('reason') or s.get('status'))}/{s.get('ms')}ms"
            for s in shots
        ) or (result.get("node") or "direct")
        hzrate = result.get("hz")
        hzbit = f" {hzrate}/s" if hzrate is not None else ""
        if not result.get("ok"):
            last = " | ".join(fmt(s) for s in watched)
            extra = f"  last: {last}" if last else ""
            kick = " KICK" if result.get("kicked") else ""
            print(
                f"[{time.strftime('%H:%M:%S')}] #{ticks} FAIL{kick} "
                f"n={len(shots)} pool={result.get('pool')}{hzbit} {shot_txt}{extra}"
            )
        else:
            line = " | ".join(fmt(s) for s in watched) or "(no target stores)"
            print(f"[{time.strftime('%H:%M:%S')}] #{ticks} n={len(shots)} {result.get('ms')}ms{hzbit}  {line}")
            if ticks == 1 or ticks % 10 == 0:
                print(f"         {shot_txt}")

        sig = tuple(sorted(s.get("storeId") for s in opened if s.get("storeId") in TARGET))
        if sig and sig != last_open:
            last_open = sig
            beep()
            print(f"*** STOCK {', '.join(sig)} — 跑 python3 scripts/auto_order.py 下单")
        elif not sig:
            last_open = None

        if once:
            return 0 if opened else 1
        time.sleep(max(0.0, wait))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        raise SystemExit(0)
