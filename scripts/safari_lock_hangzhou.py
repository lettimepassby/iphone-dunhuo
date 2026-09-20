#!/usr/bin/env python3
"""Watch the live Safari checkout page and lock Apple 西湖 / 杭州万象城 only.

Does not place an order. When a Hangzhou radio enables, it clicks that store
and refuses Shanghai radios (R401 and friends).

Requires Safari → Develop → Allow JavaScript from Apple Events.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORES = json.loads((ROOT / "config" / "stores.json").read_text())
PRODUCT = json.loads((ROOT / "config" / "product.json").read_text())

TARGET = [s["storeId"] for s in sorted(STORES["targetStores"], key=lambda s: s["priority"])]
REJECT = set(STORES["rejectStores"])
SEARCH = STORES["search"]["searchInput"]
OOS_TOKENS = ("目前不可取货", "无货可取")


JS_SNAPSHOT = r"""
(function(){
  const radios = Array.from(document.querySelectorAll('input[name=store-locator-result]'));
  function labelFor(r){
    const lab = document.querySelector('label[for="'+r.id+'"]');
    return ((lab&&lab.innerText)||r.parentElement.innerText||'').replace(/\s+/g,' ').trim().slice(0,160);
  }
  const stores = radios.map(r => ({
    id: r.value,
    disabled: !!r.disabled,
    checked: !!r.checked,
    label: labelFor(r)
  }));
  const searchBtn = document.querySelector('[data-autom=fulfillment-pickup-store-search-button]');
  const cont = document.querySelector('[data-autom=fulfillment-continue-button]');
  const pickup = document.querySelector('.rc-segmented-control-selected');
  const body = document.body.innerText;
  return JSON.stringify({
    url: location.href,
    title: document.title,
    searchBtn: searchBtn ? searchBtn.innerText.trim() : null,
    continueText: cont ? cont.innerText.trim() : null,
    continueDisabled: cont ? !!cont.disabled : null,
    pickupSelected: pickup ? pickup.innerText.trim() : null,
    locOk: body.indexOf('浙江 杭州 上城区') >= 0,
    shanghaiVisible: /上海 徐汇区|上海 杨浦区|上海 上海/.test(body),
    regionOos: body.indexOf('你所在的地区无货可取') >= 0,
    stores: stores
  });
})();
"""

JS_CLICK_STORE = r"""
(function(storeId){
  if (['R401','R678','R390','R683','R359','R705','R389','R581'].indexOf(storeId) >= 0) {
    return JSON.stringify({ok:false, reason:'rejected shanghai store', storeId:storeId});
  }
  const r = Array.from(document.querySelectorAll('input[name=store-locator-result]'))
    .find(x => x.value === storeId);
  if (!r) return JSON.stringify({ok:false, reason:'radio not found', storeId:storeId});
  if (r.disabled) return JSON.stringify({ok:false, reason:'still disabled', storeId:storeId});
  r.click();
  return JSON.stringify({ok:true, storeId:storeId, checked:r.checked, disabled:r.disabled});
})(%s);
"""

JS_REFRESH = r"""
(function(){
  const btn = document.querySelector('[data-autom=fulfillment-pickup-store-search-button]');
  if (!btn) return JSON.stringify({ok:false, reason:'search button missing'});
  const label = (btn.innerText||'').trim();
  if (label.indexOf('上海') >= 0) {
    return JSON.stringify({ok:false, reason:'search location is Shanghai', label:label});
  }
  if (label.indexOf('杭州') < 0 && label.indexOf('上城') < 0) {
    return JSON.stringify({ok:false, reason:'search location not Hangzhou', label:label});
  }
  btn.click();
  return JSON.stringify({ok:true, clicked:label});
})();
"""


def osascript_js(js: str) -> str:
    # AppleScript -e cannot swallow a JSON-quoted JS blob (quotes / backslashes).
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
    script = f'''tell application "Safari"
  if (count of windows) = 0 then return "NO_WINDOWS"
  set theTab to current tab of front window
  set js to (do shell script "cat " & quoted form of "{path}")
  return do JavaScript js in theTab
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(err or f"osascript exit {r.returncode}")
    return (r.stdout or "").strip()


def snapshot() -> dict:
    raw = osascript_js(JS_SNAPSHOT)
    if raw in ("", "missing value", "NO_WINDOWS"):
        raise RuntimeError(f"safari snapshot empty: {raw!r}")
    return json.loads(raw)


def beep() -> None:
    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)


def pick_target(stores: list[dict]) -> dict | None:
    by_id = {s["id"]: s for s in stores}
    for sid in TARGET:
        s = by_id.get(sid)
        if s and not s["disabled"]:
            return s
    return None


def format_store(s: dict) -> str:
    flag = "OOS" if s["disabled"] else "OPEN"
    chk = "*" if s["checked"] else " "
    return f"[{chk}] {s['id']} {flag} {s['label']}"


def main() -> int:
    interval = 8
    refresh_every = 4
    if len(sys.argv) > 1:
        interval = max(3, int(sys.argv[1]))
    print(f"SKU {PRODUCT['phone']['partNumber']} + {PRODUCT['appleCarePlus']['partNumber']}")
    print(f"target {TARGET}  reject {sorted(REJECT)}")
    print(f"search must stay {SEARCH!r}")
    print(f"poll {interval}s, refresh every {refresh_every} ticks")
    print("will NOT click 继续 / will NOT pay")
    print("=" * 56)

    ticks = 0
    locked = None
    last_sig = None
    while True:
        ticks += 1
        try:
            state = snapshot()
        except Exception as e:
            print(f"! safari: {e}")
            time.sleep(interval)
            continue

        if "checkout" not in (state.get("url") or ""):
            print(f"! not on checkout: {state.get('url')}")
            time.sleep(interval)
            continue

        stores = state.get("stores") or []
        hangzhou = [s for s in stores if s["id"] in TARGET]
        shanghai_checked = [s for s in stores if s["id"] in REJECT and s["checked"]]
        sig = (
            state.get("searchBtn"),
            state.get("continueText"),
            tuple((s["id"], s["disabled"], s["checked"]) for s in hangzhou),
        )
        if sig != last_sig:
            last_sig = sig
            print(f"\n[{time.strftime('%H:%M:%S')}] {state.get('url')}")
            print(f"  loc={state.get('searchBtn')} locOk={state.get('locOk')} shanghaiGeo={state.get('shanghaiVisible')}")
            print(f"  continue={state.get('continueText')!r} regionOos={state.get('regionOos')}")
            for s in hangzhou or stores[:4]:
                print("   ", format_store(s))

        if not state.get("locOk") or state.get("shanghaiVisible"):
            print("  DROP: page location is not 浙江 杭州 上城区 — not clicking anything")
            time.sleep(interval)
            continue

        if shanghai_checked:
            print(f"  DROP: shanghai radio checked {[s['id'] for s in shanghai_checked]}")

        chosen = pick_target(stores)
        if chosen:
            if locked != chosen["id"] or not chosen["checked"]:
                print(f"  LOCK {chosen['id']} {chosen['label']}")
                beep()
                try:
                    result = json.loads(osascript_js(JS_CLICK_STORE % json.dumps(chosen["id"])))
                except Exception as e:
                    result = {"ok": False, "reason": str(e)}
                print("   click", result)
                if result.get("ok"):
                    locked = chosen["id"]
                    print("   locked. waiting for timeSlot / 继续 to switch off 送货地址")
            time.sleep(interval)
            continue

        if ticks % refresh_every == 0:
            try:
                ref = json.loads(osascript_js(JS_REFRESH))
            except Exception as e:
                ref = {"ok": False, "reason": str(e)}
            if not ref.get("ok"):
                print(f"  refresh skipped: {ref}")
            else:
                print(f"  refreshed {ref.get('clicked')}")

        time.sleep(interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        raise SystemExit(0)
