#!/usr/bin/env python3
"""Protocol sniper: POST checkoutx search/select for R471/R532.

Safari is only the cookie + x-aos-stk carrier. No radio clicks, no payment.
Requires the checkout tab to stay open (same origin as /shop/checkoutx).

    python3 scripts/snipe_hangzhou.py           # poll + lock store
    python3 scripts/snipe_hangzhou.py 3         # interval seconds
    python3 scripts/snipe_hangzhou.py --once    # one search, print, exit
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORES = json.loads((ROOT / "config" / "stores.json").read_text())
PRODUCT = json.loads((ROOT / "config" / "product.json").read_text())
ENDPOINTS = json.loads((ROOT / "config" / "endpoints.json").read_text())

TARGET = [s["storeId"] for s in sorted(STORES["targetStores"], key=lambda s: s["priority"])]
TARGET_NAMES = {s["storeId"]: s["name"] for s in STORES["targetStores"]}
REJECT = set(STORES["rejectStores"])
SEARCH = STORES["search"]

JS = r"""
(function(){
  const cmd = __CMD__;
  const storeId = __STORE__;

  function readInit(){
    const el = document.getElementById('init_data');
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch(e) { return null; }
  }

  function currentStk(){
    const d = readInit();
    return (d && d.meta && d.meta.h && d.meta.h['x-aos-stk']) || '';
  }

  function hangzhouBody(selectStore){
    const p = 'checkout.fulfillment.pickupTab.pickup.storeLocator.';
    const a = p + 'address.stateCitySelectorForCheckout.';
    return [
      p + 'showAllStores=false',
      p + 'selectStore=' + encodeURIComponent(selectStore),
      p + 'searchInput=' + encodeURIComponent('浙江 杭州 上城区'),
      a + 'city=' + encodeURIComponent('杭州'),
      a + 'state=' + encodeURIComponent('浙江'),
      a + 'provinceCityDistrict=' + encodeURIComponent('浙江 杭州 上城区'),
      a + 'countryCode=CN',
      a + 'district=' + encodeURIComponent('上城区')
    ].join('&');
  }

  function xhr(path, body, stk){
    const x = new XMLHttpRequest();
    x.open('POST', path, false);
    x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
    x.setRequestHeader('Accept', '*/*');
    x.setRequestHeader('syntax', 'graviton');
    x.setRequestHeader('x-aos-model-page', 'checkoutPage');
    x.setRequestHeader('modelVersion', 'v2');
    x.setRequestHeader('X-Requested-With', 'Fetch');
    if (stk) x.setRequestHeader('x-aos-stk', stk);
    x.send(body || '');
    let parsed = null;
    try { parsed = JSON.parse(x.responseText); } catch(e) {}
    const newStk = parsed && parsed.body && parsed.body.meta && parsed.body.meta.h && parsed.body.meta.h['x-aos-stk'];
    return {status: x.status, stk: newStk || stk, parsed: parsed, err: x.status >= 400 ? (x.responseText||'').slice(0,200) : null};
  }

  function findDude(o, depth){
    if (!o || depth > 8) return null;
    if (typeof o !== 'object') return null;
    if (o.dudeAttributeStore) return o.dudeAttributeStore;
    for (const k of Object.keys(o)) {
      const v = findDude(o[k], depth + 1);
      if (v) return v;
    }
    return null;
  }

  function slim(parsed){
    const checkout = ((parsed || {}).body || {}).checkout || {};
    const pickup = (((checkout.fulfillment || {}).pickupTab || {}).pickup) || {};
    const sl = (pickup.storeLocator || {}).d || {};
    const stores = ((((pickup.storeLocator || {}).searchResults || {}).d) || {}).retailStores || [];
    const ts = (pickup.timeSlot || {}).d || {};
    const fo = ((checkout.fulfillment || {}).fulfillmentOptions || {}).d || {};
    const slimStores = stores.map(function(s){
      return {
        storeId: s.storeId,
        storeName: s.storeName,
        storeDisabled: !!s.storeDisabled,
        quote: (s.availability || {}).storeAvailability || '',
        city: (s.retailAddress || {}).city || ''
      };
    });
    return {
      selectStore: sl.selectStore || null,
      searchInput: sl.searchInput || null,
      dude: findDude(pickup, 0),
      timeSlotStoreId: ts.storeId || null,
      enableTimeSlotWindows: ts.enableTimeSlotWindows || false,
      fulfillment: fo.selectFulfillmentLocation || null,
      stores: slimStores
    };
  }

  if (location.href.indexOf('/shop/checkout') < 0) {
    return JSON.stringify({ok:false, reason:'not on checkout', url: location.href});
  }

  let stk = currentStk();
  if (!stk) {
    return JSON.stringify({ok:false, reason:'no x-aos-stk in init_data', url: location.href});
  }

  if (cmd === 'retail') {
    const r = xhr(
      '/shop/checkoutx/fulfillment?_a=selectFulfillmentLocationAction&_m=checkout.fulfillment.fulfillmentOptions',
      'checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation=RETAIL',
      stk
    );
    if (r.status !== 200 || !r.parsed) {
      return JSON.stringify({ok:false, reason:'retail xhr failed', status:r.status, err:r.err});
    }
    stk = r.stk;
    const s = slim(r.parsed);
    return JSON.stringify({ok:true, cmd:'retail', stk:stk, state:s});
  }

  if (cmd === 'session') {
    const r = xhr('/shop/checkoutx/session?_a=extendSession&_m=checkout.session', '', stk);
    return JSON.stringify({ok: r.status === 200, cmd:'session', status:r.status, stk: r.stk});
  }

  const pathA = (cmd === 'select') ? 'select' : 'search';
  const useStore = storeId || 'R471';
  if (['R401','R678','R390','R683','R359','R705','R389','R581'].indexOf(useStore) >= 0) {
    return JSON.stringify({ok:false, reason:'refusing shanghai store', storeId: useStore});
  }
  const r = xhr(
    '/shop/checkoutx/fulfillment?_a=' + pathA + '&_m=checkout.fulfillment.pickupTab.pickup.storeLocator',
    hangzhouBody(useStore),
    stk
  );
  if (r.status !== 200 || !r.parsed) {
    return JSON.stringify({ok:false, reason: pathA + ' xhr failed', status:r.status, err:r.err, head:(r.parsed&&r.parsed.head)||null});
  }
  const state = slim(r.parsed);
  const shanghai = (state.selectStore && ['R401','R678','R390','R683','R359','R705','R389','R581'].indexOf(state.selectStore) >= 0)
    || (state.dude && String(state.dude).indexOf('上海') >= 0)
    || (state.searchInput && String(state.searchInput).indexOf('上海') >= 0);
  const hangzhou = (state.stores || []).filter(function(s){ return s.storeId === 'R471' || s.storeId === 'R532'; });
  const openHz = hangzhou.filter(function(s){ return !s.storeDisabled; });
  const locked = (state.selectStore === 'R471' || state.selectStore === 'R532')
    && state.dude
    && String(state.dude).indexOf('上海') < 0
    && !shanghai;
  return JSON.stringify({
    ok: true,
    cmd: pathA,
    requestedStore: useStore,
    stk: r.stk,
    shanghai: !!shanghai,
    locked: !!locked,
    open: openHz,
    hangzhou: hangzhou,
    state: {
      selectStore: state.selectStore,
      searchInput: state.searchInput,
      dude: state.dude,
      timeSlotStoreId: state.timeSlotStoreId,
      enableTimeSlotWindows: state.enableTimeSlotWindows,
      fulfillment: state.fulfillment
    }
  });
})();
"""


def osascript_js(js: str) -> str:
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


def run_cmd(cmd: str, store: str | None = None) -> dict:
    js = JS.replace("__CMD__", json.dumps(cmd)).replace("__STORE__", json.dumps(store))
    raw = osascript_js(js)
    if raw in ("", "missing value", "NO_WINDOWS"):
        raise RuntimeError(f"safari empty: {raw!r}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"safari non-json: {raw[:300]!r}") from e


def beep() -> None:
    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)


def pick_open(open_stores: list[dict]) -> dict | None:
    by_id = {s["storeId"]: s for s in open_stores}
    for sid in TARGET:
        if sid in by_id:
            return by_id[sid]
    return None


def fmt(s: dict) -> str:
    flag = "OOS" if s.get("storeDisabled") else "OPEN"
    return f"{s.get('storeId')} {flag} {s.get('storeName')} {s.get('quote')}"


def main() -> int:
    once = "--once" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--once"]
    interval = 3
    if args:
        interval = max(1, int(args[0]))

    print(f"SKU {PRODUCT['phone']['partNumber']} + {PRODUCT['appleCarePlus']['partNumber']}")
    print(f"protocol POST {ENDPOINTS['origin']}{ENDPOINTS['xhr']['fulfillment']}")
    print(f"target {TARGET}  reject {sorted(REJECT)}")
    print(f"geo {SEARCH['searchInput']!r}")
    print("Safari = cookie/stk only. will NOT pay, will NOT fill contact")
    print("=" * 56)

    try:
        retail = run_cmd("retail")
        print("retail", retail.get("ok"), (retail.get("state") or {}).get("fulfillment"), retail.get("reason"))
    except Exception as e:
        print(f"! retail: {e}")

    ticks = 0
    locked_id = None
    last_sig = None
    while True:
        ticks += 1
        try:
            result = run_cmd("search", TARGET[0])
        except Exception as e:
            print(f"! search: {e}")
            if once:
                return 1
            time.sleep(interval)
            continue

        if not result.get("ok"):
            print(f"! search fail {result}")
            if once:
                return 1
            time.sleep(interval)
            continue

        hz = result.get("hangzhou") or []
        opened = result.get("open") or []
        st = result.get("state") or {}
        sig = (
            st.get("selectStore"),
            st.get("dude"),
            tuple((s.get("storeId"), s.get("storeDisabled"), s.get("quote")) for s in hz),
        )
        if sig != last_sig or once:
            last_sig = sig
            print(f"\n[{time.strftime('%H:%M:%S')}] search selectStore={st.get('selectStore')} dude={st.get('dude')}")
            print(f"  searchInput={st.get('searchInput')} shanghaiBounce={result.get('shanghai')}")
            for s in hz:
                print("   ", fmt(s))
            if not hz:
                print("   (no Hangzhou stores in this response)")

        if result.get("shanghai"):
            print("  DROP shanghai bounce in search response — ignoring selectStore")

        chosen = pick_open(opened)
        if chosen:
            if locked_id != chosen["storeId"] or not result.get("locked"):
                print(f"  LOCK via _a=select {chosen['storeId']} {chosen.get('storeName')}")
                beep()
                try:
                    sel = run_cmd("select", chosen["storeId"])
                except Exception as e:
                    sel = {"ok": False, "reason": str(e)}
                sst = sel.get("state") or {}
                print(
                    f"   select ok={sel.get('ok')} locked={sel.get('locked')} "
                    f"selectStore={sst.get('selectStore')} dude={sst.get('dude')} "
                    f"shanghai={sel.get('shanghai')} reason={sel.get('reason')}"
                )
                if sel.get("locked") and sst.get("selectStore") in TARGET:
                    locked_id = sst["selectStore"]
                    print(f"   locked {TARGET_NAMES.get(locked_id, locked_id)}. not continuing to contact/pay")
                elif sel.get("shanghai"):
                    print("   DROP: select bounced to Shanghai, will retry")
            if once:
                return 0 if (result.get("locked") or (chosen and locked_id)) else 1
        else:
            locked_id = None

        if ticks % 20 == 0:
            try:
                ses = run_cmd("session")
                print(f"  extendSession ok={ses.get('ok')} status={ses.get('status')}")
            except Exception as e:
                print(f"  extendSession: {e}")

        if once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        raise SystemExit(0)
