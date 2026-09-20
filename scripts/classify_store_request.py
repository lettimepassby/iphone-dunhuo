#!/usr/bin/env python3
"""Classify a checkout storeLocator form body as hangzhou-intent vs shanghai-bounce."""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORES = json.loads((ROOT / "config" / "stores.json").read_text())

TARGET_IDS = {s["storeId"] for s in STORES["targetStores"]}
REJECT_IDS = set(STORES["rejectStores"])
REJECT_TOKENS = tuple(STORES["rejectSearchTokens"])
SEARCH = STORES["search"]


def parse(body: str) -> dict[str, str]:
    qs = urllib.parse.parse_qs(body, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in qs.items()}


def field(d: dict[str, str], suffix: str) -> str:
    for k, v in d.items():
        if k.endswith(suffix) or k.split(".")[-1] == suffix.split(".")[-1] and suffix in k:
            if k.endswith(suffix):
                return v
    for k, v in d.items():
        if k.endswith(suffix):
            return v
    return ""


def classify(body: str) -> dict:
    d = parse(body)
    store = field(d, "storeLocator.selectStore")
    search = field(d, "storeLocator.searchInput")
    state = field(d, "stateCitySelectorForCheckout.state")
    city = field(d, "stateCitySelectorForCheckout.city")
    district = field(d, "stateCitySelectorForCheckout.district")
    pcd = field(d, "stateCitySelectorForCheckout.provinceCityDistrict")
    fulfill = field(d, "selectFulfillmentLocation")

    reasons = []
    verdict = "unknown"

    if store in TARGET_IDS and city == SEARCH["city"] and state == SEARCH["state"]:
        verdict = "ALLOW_SELECT_HANGZHOU"
        reasons.append(f"selectStore={store} with 浙江/杭州")
    elif store in TARGET_IDS:
        verdict = "ALLOW_BUT_CITY_MISMATCH"
        reasons.append(f"selectStore={store} but city={city!r} state={state!r}")
    elif store in REJECT_IDS:
        verdict = "DROP_SHANGHAI_DEFAULT"
        reasons.append(f"selectStore={store} is Shanghai fallback")
    elif any(t in (search + state + city + district + pcd) for t in REJECT_TOKENS):
        verdict = "DROP_SHANGHAI_GEO"
        reasons.append("search/city contains 上海")
    elif city == SEARCH["city"] and state == SEARCH["state"]:
        verdict = "ALLOW_CITY_STEP"
        reasons.append("province/city step for Hangzhou, no store lock yet")
    elif fulfill == "RETAIL":
        verdict = "ALLOW_RETAIL_TAB"
        reasons.append("switched fulfillment to RETAIL")

    return {
        "verdict": verdict,
        "reasons": reasons,
        "selectStore": store,
        "searchInput": search,
        "state": state,
        "city": city,
        "district": district,
        "fulfillment": fulfill,
        "hangzhouLocked": store in TARGET_IDS
        and city == SEARCH["city"]
        and state == SEARCH["state"]
        and district == SEARCH["district"],
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: classify_store_request.py <urlencoded-body-file-or-string>")
        sys.exit(2)
    arg = sys.argv[1]
    body = Path(arg).read_text() if Path(arg).exists() else arg
    print(json.dumps(classify(body), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
