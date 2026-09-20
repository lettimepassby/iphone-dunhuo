#!/usr/bin/env python3
"""No-login pickup stock via /shop/retail/pickup-message.

Each proxy is a resident worker with keep-alive. Results land in a queue
as soon as they return; the main loop never waits on the slowest node.
HTTP 541/503/429 cools that node only. Hard fails kick after 8.

Checkout stays on the logged-in Safari session (no proxy).
"""
from __future__ import annotations

import json
import os
import queue
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = Path(__file__).resolve().parents[1]
STORES_CFG = json.loads((ROOT / "config" / "stores.json").read_text())
SKU_DEFAULT = "MJYE4CH/A"
TARGET = tuple(s["storeId"] for s in sorted(STORES_CFG["targetStores"], key=lambda s: s["priority"]))
NAMES = {s["storeId"]: s["name"] for s in STORES_CFG["targetStores"]}
SHORT = {
    "R471": "西湖",
    "R532": "万象城",
    "R531": "天一",
    "R678": "静安",
    "R401": "环贸",
    "R359": "南京东路",
    "R389": "浦东",
    "R390": "香港广场",
    "R683": "环球港",
    "R705": "七宝",
    "R581": "五角场",
}
def _build_anchors() -> list[str]:
    """Minimal set of searchNearby anchors that together cover every target.

    A single anchor's searchNearby only returns nearby stores, so one Hangzhou
    anchor (R471) drops the farthest Shanghai store (R581 五角场 was missing from
    the R471 capture). Anchor once per city — highest-priority store in each — so
    every target's own-city cluster is reachable. Primary city first, then other
    cities by how many targets they hold (biggest cluster first) so the early-exit
    in _fetch_anchors usually stops after 2 requests.
    """
    stores = STORES_CFG["targetStores"]
    by_city: dict[str, list[dict]] = {}
    for s in stores:
        by_city.setdefault(s.get("city") or "", []).append(s)
    prio = lambda s: s.get("priority", 1 << 30)
    city_anchor = {c: min(lst, key=prio)["storeId"] for c, lst in by_city.items()}
    primary_city = min(stores, key=prio).get("city") or ""
    anchors = [city_anchor[primary_city]]
    others = [c for c in by_city if c != primary_city]
    others.sort(key=lambda c: (-len(by_city[c]), min(prio(s) for s in by_city[c])))
    anchors += [city_anchor[c] for c in others]
    return anchors


PRIMARY = "R471"
ANCHORS = _build_anchors()
URL = (
    "https://www.apple.com.cn/shop/retail/pickup-message"
    "?pl=true&mts.0=regular&parts.0={sku}&searchNearby=true&store={store}"
)
_BASE_URL = "https://www.apple.com.cn/shop/retail/pickup-message?pl=true&mts.0=regular"


def build_url(skus: list[str], store: str) -> str:
    """pickup-message URL for one or more parts (parts.0, parts.1, …)."""
    parts = "&".join(
        f"parts.{i}={urllib.request.quote(s, safe='/')}" for i, s in enumerate(skus)
    )
    return f"{_BASE_URL}&{parts}&searchNearby=true&store={store}"


def iphone18_all_skus() -> list[str]:
    """Every iPhone 18 Pro Max + 18 Pro part number (for --test broad trigger)."""
    try:
        cfg = json.loads((ROOT / "config" / "skus.json").read_text())
    except Exception:
        return [SKU_DEFAULT]
    picked: list[str] = [SKU_DEFAULT]
    for fam in ("iphone18ProMax", "iphone18Pro"):
        picked.extend(((cfg.get(fam) or {}).get("parts") or {}).keys())
    seen: set[str] = set()
    out: list[str] = []
    for s in picked:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/mjye4ch/a",
    "Connection": "keep-alive",
}

KICK_AFTER = 8
PROXY_COOLDOWN = 2.2
DIRECT_COOLDOWN = 8.0
# 541/429/503 = Apple throttling THIS IP. Its throttle window is ~40s, so back off
# to ~40s: retrying sooner just hits inside the window (still 541, and can extend
# the block). The low poll rate is the unavoidable cost of respecting it — the fix
# for more throughput is more/better IPs, not a shorter cooldown.
SOFT_COOLDOWN = 12.0
SOFT_FAIL = {429, 503, 541}
# Hard-fail kick is a penalty box, not a permanent ban: after KICK_AFTER hard
# fails the proxy sits out KICK_PENALTY (×round, capped) then retries. Only after
# MAX_KICKS rounds is it removed for good.
KICK_PENALTY = 120.0
KICK_MAX_PENALTY = 600.0
MAX_KICKS = 4
# Rotating tunnel (e.g. 青果 隧道, per-request auto-rotating exit IP). Every request
# is a fresh IP, so per-IP cooldown/kick is meaningless — it runs at a steady rate
# with its own (wider) timeout. Used alongside the free proxies: free ones are fast
# but mostly 541; the tunnel is slower but reliable, so both signals arrive and the
# first OPEN through either wins.
TUNNEL_COOLDOWN = 2.0
TUNNEL_TIMEOUT = 6.0
PROXY_TIMEOUT = 2.4
DIRECT_TIMEOUT = 3.0
CONNECT_TIMEOUT = 1.4
QG_TIMEOUT = 3.2
WORKERS_PER_NODE = 1

# 青果短效共享代理：默认关闭（订阅已到期）。要重新启用，设环境变量
#   APPLE_QG_URL=https://share.proxy.qg.net/get?key=<你的key>&num=2&format=json
# 只扫公开库存，结账仍走本机 Safari；不写入 proxies.json 静态池。
QG_DEFAULT = (
    "https://share.proxy.qg.net/get?num=2"
    "&area=&isp=0&format=json&distinct=false"
)
QG_REFRESH = 50.0
QG_RETRY = 10.0
QG_LEAD = 8.0


def qg_extract_url() -> str:
    raw = (os.environ.get("APPLE_QG_URL") or QG_DEFAULT).strip()
    parts = urllib.parse.urlsplit(raw)
    q = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    q["format"] = ["json"]
    q.pop("seq", None)
    if not (q.get("num") or [""])[-1]:
        q["num"] = ["2"]
    query = urllib.parse.urlencode({k: v[-1] for k, v in q.items()})
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def qg_slot_count(url: str | None = None) -> int:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url or qg_extract_url()).query)
    try:
        n = int((q.get("num") or ["2"])[-1] or "2")
    except ValueError:
        n = 2
    return max(1, min(8, n))


def _qg_deadline(raw: str | None) -> float:
    try:
        return time.mktime(time.strptime(str(raw or "").strip(), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, OverflowError, ValueError):
        return time.time() + 60.0


def _qg_get(target: str, timeout: float) -> bytes:
    """Fetch the extract URL, falling back to curl when stdlib SSL can't verify.

    Some machines (python.org Python without wired certs, or an AV / corporate
    proxy injecting a self-signed root) fail stdlib TLS with
    CERTIFICATE_VERIFY_FAILED even though curl (system keychain) works — the same
    reason the stock fetch already falls back to curl. extract_qg is the only
    stdlib-urllib caller, so mirror that here.
    """
    req = urllib.request.Request(
        target,
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        cmd = [
            "curl", "-s", "--compressed",
            "--connect-timeout", "3",
            "-m", str(max(2, int(timeout))),
            "-A", HEADERS["User-Agent"],
            "-H", "Accept: application/json, text/plain, */*",
            target,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 2)
        if r.returncode != 0 or not (r.stdout or b"").strip():
            err = (r.stderr or b"").decode("utf-8", "replace").strip() or f"curl exit {r.returncode}"
            raise RuntimeError(err)
        return r.stdout


def extract_qg(url: str | None = None, timeout: float = 8.0) -> list[dict[str, Any]]:
    target = url or qg_extract_url()
    blob = _qg_get(target, timeout)
    data = json.loads(blob.decode("utf-8", "replace") or "{}")
    code = str(data.get("code") or data.get("Code") or "")
    rows = data.get("data") or data.get("Data") or []
    if code.upper() not in ("SUCCESS", "0", "") and not rows:
        raise RuntimeError(f"qg {code}: {str(data)[:160]}")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            server = row.strip()
            area = isp = ""
            deadline = time.time() + 60.0
        elif isinstance(row, dict):
            server = str(row.get("server") or row.get("proxy_ip") or "").strip()
            if server and ":" not in server:
                port = row.get("port") or row.get("proxy_port")
                if port:
                    server = f"{server}:{port}"
            area = str(row.get("area") or "")
            isp = str(row.get("isp") or "")
            deadline = _qg_deadline(row.get("deadline") or row.get("overdue") or row.get("expire"))
        else:
            continue
        if not server or server in seen:
            continue
        seen.add(server)
        out.append(
            {
                "server": server,
                "proxy": f"http://{server}",
                "area": area,
                "isp": isp,
                "deadline": deadline,
            }
        )
    return out


def _is_qg_node(n: dict[str, Any] | None) -> bool:
    """Ephemeral rotating node (青果 slots AND the tunnel) — never persisted, never
    per-IP cooled/kicked, since every request is a fresh exit IP."""
    if not n:
        return False
    if n.get("ephemeral") or n.get("src") in ("qg", "tunnel"):
        return True
    name = str(n.get("name") or "")
    return name == "qg" or name.startswith("qg-") or name == "tunnel"


def tunnel_url() -> str | None:
    raw = (os.environ.get("APPLE_TUNNEL_URL") or "").strip()
    return raw or None


def is_open(sku_obj: dict) -> bool:
    display = str(sku_obj.get("pickupDisplay") or "").lower()
    if display == "available":
        return True
    if display in ("unavailable", "ineligible"):
        return False
    quote = str(sku_obj.get("pickupSearchQuote") or sku_obj.get("storePickupQuote") or "")
    if any(tok in quote for tok in ("不可取货", "暂无供应", "无货可取", "目前不可")):
        return False
    return "可取货" in quote


def backoff_seconds(streak: int, base: float = 1.0) -> float:
    if streak <= 0:
        return base
    return min(12.0, max(base, 1.2 * (2 ** (streak - 1))))


def _no_direct() -> bool:
    """Keep the local IP out of stock polling so it stays clean for checkout.

    Default ON: the local IP is the only one that can place the order (Safari
    session), so hammering pickup-message from it risks getting it throttled
    (HTTP 541) right when the checkout burst needs it. Detection rides proxies +
    青果. Set APPLE_USE_DIRECT=1 to poll from the local IP too.
    """
    return (os.environ.get("APPLE_USE_DIRECT") or "").strip().lower() not in ("1", "on", "true", "yes")


def load_nodes() -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    path = ROOT / "config" / "proxies.json"
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except json.JSONDecodeError:
            cfg = {}
        for n in cfg.get("nodes") or []:
            if isinstance(n, str):
                proxy = None if n in ("direct", "", "none") else n
                nodes.append({"name": "direct" if proxy is None else n, "proxy": proxy})
            elif isinstance(n, dict):
                proxy = n.get("proxy") or None
                if proxy in ("direct", "", "none"):
                    proxy = None
                name = n.get("name") or ("direct" if proxy is None else proxy)
                if _is_qg_node({"name": name, "src": n.get("src"), "ephemeral": n.get("ephemeral")}):
                    continue
                nodes.append({"name": str(name), "proxy": proxy})
    extra = os.environ.get("APPLE_PROXIES") or ""
    for i, raw in enumerate(p.strip() for p in extra.split(",")):
        if not raw:
            continue
        nodes.append({"name": f"env{i + 1}", "proxy": raw})
    no_direct = _no_direct()
    if not nodes and not no_direct:
        nodes = [{"name": "direct", "proxy": None}]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for n in nodes:
        if no_direct and not n["proxy"]:
            continue  # drop the local IP; detection rides proxies + 青果
        key = n["proxy"] or "direct"
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": n["name"], "proxy": n["proxy"]})
    return out


def _parse_stores(raw: bytes, skus) -> list[dict]:
    if isinstance(skus, str):
        skus = [skus]
    data = json.loads(raw)
    out: list[dict] = []
    for s in (data.get("body") or {}).get("stores") or []:
        sid = s.get("storeNumber")
        pa = s.get("partsAvailability") or {}
        # store counts as open if ANY watched sku is available; pick that sku for
        # display, else fall back to the first sku present so we still show a quote.
        open_hit = None
        fallback = None
        for sk in skus:
            obj = pa.get(sk)
            if not obj:
                continue
            if fallback is None:
                fallback = (sk, obj)
            if is_open(obj):
                open_hit = (sk, obj)
                break
        sk, sku_obj = open_hit or fallback or ((skus[0] if skus else ""), {})
        quote = sku_obj.get("pickupSearchQuote") or sku_obj.get("storePickupQuote") or ""
        out.append(
            {
                "storeId": sid,
                "storeName": s.get("storeName") or NAMES.get(sid, sid),
                "city": s.get("city") or "",
                "state": s.get("state") or "",
                "pickupDisplay": sku_obj.get("pickupDisplay") or "",
                "quote": quote,
                "storeDisabled": open_hit is None,
                "sku": sk,
            }
        )
    return out


def _watched_from(merged: dict[str, dict], *, stale: bool = False) -> list[dict]:
    watched: list[dict] = []
    for sid in TARGET:
        if sid in merged:
            row = dict(merged[sid])
            if stale:
                row["stale"] = True
            watched.append(row)
        else:
            watched.append(
                {
                    "storeId": sid,
                    "storeName": NAMES.get(sid, sid),
                    "city": "",
                    "pickupDisplay": "",
                    "quote": "—",
                    "storeDisabled": True,
                    "missing": True,
                }
            )
    return watched


def _pool(proxy: str | None):
    kw = dict(
        timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT, read=PROXY_TIMEOUT),
        retries=False,
        maxsize=2,
        block=False,
        headers=HEADERS,
    )
    if proxy:
        return urllib3.ProxyManager(proxy, cert_reqs="CERT_REQUIRED", **kw)
    return urllib3.PoolManager(cert_reqs="CERT_REQUIRED", **kw)


def _fetch_pool(http, url: str, timeout: float) -> tuple[int, bytes, int]:
    t0 = time.time()
    r = http.request(
        "GET",
        url,
        headers=HEADERS,
        timeout=urllib3.Timeout(connect=min(CONNECT_TIMEOUT, timeout), read=timeout),
        retries=False,
        redirect=True,
        preload_content=True,
    )
    try:
        return int(r.status), r.data or b"", int((time.time() - t0) * 1000)
    finally:
        r.release_conn()


def _fetch_curl(url: str, proxy: str | None, timeout: float) -> tuple[int, bytes, int]:
    # Adaptive timeouts: free proxies (small timeout) still fail fast on a dead IP;
    # the overseas tunnel (large timeout) gets enough to connect + respond.
    connect_to = max(1.0, min(4.0, timeout * 0.7))
    cmd = [
        "curl",
        "-s",
        "--compressed",
        "--connect-timeout",
        f"{connect_to:.1f}",
        "-m",
        f"{max(1.0, timeout):.1f}",
        "-A",
        HEADERS["User-Agent"],
        "-H",
        f"Accept: {HEADERS['Accept']}",
        "-H",
        f"Accept-Language: {HEADERS['Accept-Language']}",
        "-H",
        f"Referer: {HEADERS['Referer']}",
        "-w",
        "\n__STATUS__%{http_code}",
        url,
    ]
    if proxy:
        cmd[1:1] = ["-x", proxy]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 1.2)
    ms = int((time.time() - t0) * 1000)
    blob = r.stdout or b""
    marker = b"\n__STATUS__"
    idx = blob.rfind(marker)
    if idx < 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip() or f"curl exit {r.returncode}"
        raise RuntimeError(err)
    body, status_raw = blob[:idx], blob[idx + len(marker) :]
    try:
        status = int(status_raw.strip() or b"0")
    except ValueError:
        status = 0
    if r.returncode != 0 and status == 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip() or f"curl exit {r.returncode}"
        raise RuntimeError(err)
    return status, body, ms


def fetch_one(sku: str, anchor: str, timeout: float = PROXY_TIMEOUT, proxy: str | None = None) -> dict[str, Any]:
    url = URL.format(sku=urllib.request.quote(sku, safe="/"), store=anchor)
    t0 = time.time()
    http = None
    try:
        http = _pool(proxy)
        status, raw, ms = _fetch_pool(http, url, timeout)
    except Exception:
        try:
            status, raw, ms = _fetch_curl(url, proxy, timeout)
        except Exception as e:
            return {
                "ok": False,
                "status": 0,
                "reason": str(e)[:120],
                "ms": int((time.time() - t0) * 1000),
                "stores": [],
            }
    finally:
        if http is not None:
            try:
                http.clear()
            except Exception:
                pass
    if status != 200:
        return {"ok": False, "status": status, "reason": f"http {status}", "ms": ms, "stores": []}
    try:
        stores = _parse_stores(raw, sku)
    except json.JSONDecodeError:
        return {"ok": False, "status": status, "reason": "non-json", "ms": ms, "stores": []}
    return {"ok": True, "status": status, "ms": ms, "stores": stores, "n": len(stores), "anchor": anchor}


class StockPoller:
    def __init__(self, base_interval: float = 0.12, nodes: list[dict[str, Any]] | None = None):
        self.base_interval = max(0.05, float(base_interval))
        self.proxy_cd = PROXY_COOLDOWN
        self.direct_cd = DIRECT_COOLDOWN
        raw = nodes if nodes is not None else load_nodes()
        self.nodes: list[dict[str, Any]] = []
        for src in raw:
            self.nodes.append(
                {
                    "name": src["name"],
                    "proxy": src.get("proxy"),
                    "fail_streak": 0,
                    "soft_streak": 0,
                    "poll_n": 0,
                    "kicked": False,
                    "next_ok": 0.0,
                }
            )
        self.last_by_id: dict[str, dict] = {}
        self.kicked: list[str] = []
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._ok_times: list[float] = []
        self.skus: list[str] = [SKU_DEFAULT]
        self._threads: list[threading.Thread] = []
        self._qg_url = qg_extract_url()
        self._qg_slots: list[dict[str, Any]] = []
        if self._qg_enabled():
            n_slots = qg_slot_count(self._qg_url)
            for i in range(n_slots):
                slot = {
                    "name": f"qg-{i}",
                    "proxy": None,
                    "fail_streak": 0,
                    "soft_streak": 0,
                    "poll_n": 0,
                    "kicked": False,
                    "next_ok": time.time() + 86400,
                    "proxy_gen": 0,
                    "src": "qg",
                    "ephemeral": True,
                    "deadline": 0.0,
                    "area": "",
                    "isp": "",
                }
                self.nodes.append(slot)
                self._qg_slots.append(slot)
        self._tunnel = tunnel_url()
        if self._tunnel:
            # One steady worker; every request exits via a different IP.
            self.nodes.append({
                "name": "tunnel",
                "proxy": self._tunnel,
                "fail_streak": 0,
                "soft_streak": 0,
                "poll_n": 0,
                "kicked": False,
                "next_ok": time.time(),
                "proxy_gen": 0,
                "src": "tunnel",
                "ephemeral": True,
                "deadline": time.time() + 86400 * 365,  # never expires (unlike 青果 slots)
                "area": "",
                "isp": "",
            })
        if not self.nodes:
            # APPLE_NO_DIRECT left nothing to poll (no proxies, 青果 off/empty).
            # Fall back to direct rather than run blind, and say so.
            print("no proxies and 青果 unavailable; falling back to direct despite APPLE_NO_DIRECT", flush=True)
            self.nodes.append(
                {"name": "direct", "proxy": None, "fail_streak": 0, "soft_streak": 0,
                 "poll_n": 0, "kicked": False, "next_ok": 0.0}
            )
        n = max(1, len(self.nodes))
        stagger = min(0.35, PROXY_COOLDOWN / n)
        for i, node in enumerate(self.nodes):
            if not _is_qg_node(node):
                node["next_ok"] = time.time() + i * stagger
            th = threading.Thread(
                target=self._worker,
                args=(node, 0.0),
                daemon=True,
                name=f"stock-{node['name']}",
            )
            th.start()
            self._threads.append(th)
        if self._qg_slots:
            th = threading.Thread(target=self._qg_rotator, daemon=True, name="qg-rotator")
            th.start()
            self._threads.append(th)

    def _qg_enabled(self) -> bool:
        # 默认关闭：青果订阅已到期。要重新启用，设 APPLE_QG_URL=<提取链接>。
        raw = os.environ.get("APPLE_QG_URL")
        if raw is None:
            return False
        return raw.strip().lower() not in ("0", "off", "false", "none", "")

    def stop(self) -> None:
        self._stop.set()

    def alive(self) -> list[dict[str, Any]]:
        return [n for n in self.nodes if not n.get("kicked")]

    def names(self) -> list[str]:
        return [n["name"] for n in self.alive()]

    def hz(self) -> float:
        now = time.time()
        self._ok_times = [t for t in self._ok_times if now - t <= 2.0]
        return round(len(self._ok_times) / 2.0, 1)

    def persist(self) -> None:
        path = ROOT / "config" / "proxies.json"
        try:
            cfg = json.loads(path.read_text()) if path.exists() else {}
        except json.JSONDecodeError:
            cfg = {}
        dead = {
            n.get("proxy")
            for n in self.nodes
            if n.get("kicked") and n.get("proxy") and not _is_qg_node(n)
        }
        if _no_direct():
            kept: list[dict[str, Any]] = []
            seen: set[str] = set()
        else:
            kept = [{"name": "direct"}]
            seen = {"direct"}
        for n in cfg.get("nodes") or []:
            if not isinstance(n, dict):
                continue
            if _is_qg_node(n):
                continue
            proxy = n.get("proxy")
            if not proxy or proxy in dead or proxy in seen:
                continue
            seen.add(proxy)
            kept.append({"name": n.get("name") or proxy, "proxy": proxy})
        for n in self.alive():
            if _is_qg_node(n):
                continue
            proxy = n.get("proxy")
            if not proxy or proxy in seen:
                continue
            seen.add(proxy)
            kept.append({"name": n["name"], "proxy": proxy})
        proxy_n = sum(1 for k in kept if k.get("proxy"))
        # never persist an empty pool — overnight kicks used to wipe proxies.json
        if proxy_n == 0:
            try:
                fresh = json.loads(path.read_text()) if path.exists() else cfg
            except json.JSONDecodeError:
                fresh = cfg
            if any(isinstance(n, dict) and n.get("proxy") and not _is_qg_node(n) for n in (fresh.get("nodes") or [])):
                cfg = fresh
            cfg["kicked"] = self.kicked[-20:]
            cfg["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
            return
        cfg["nodes"] = kept
        cfg["live"] = proxy_n
        cfg["kicked"] = self.kicked[-20:]
        cfg["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        global _proxies_mtime
        try:
            _proxies_mtime = path.stat().st_mtime
        except OSError:
            pass

    def _cooldown(self, node: dict[str, Any]) -> float:
        if node.get("src") == "tunnel":
            return TUNNEL_COOLDOWN
        return self.direct_cd if not node.get("proxy") else self.proxy_cd

    def _make_http(self, node: dict[str, Any]):
        # Proxies with embedded auth (user:pass@host, e.g. the tunnel) — urllib3's
        # ProxyManager is unreliable doing an authenticated HTTPS CONNECT, so use
        # curl (proven to work with -x user:pass@host).
        proxy = node.get("proxy") or ""
        if "@" in proxy:
            return None, "curl"
        try:
            return _pool(node.get("proxy")), "pool"
        except Exception:
            return None, "curl"

    def _fetch(self, http, mode: str, node: dict[str, Any], url: str, timeout: float) -> dict[str, Any]:
        t0 = time.time()
        try:
            if http is not None and mode == "pool":
                status, raw, ms = _fetch_pool(http, url, timeout)
            else:
                status, raw, ms = _fetch_curl(url, node.get("proxy"), timeout)
        except Exception as e:
            return {
                "ok": False,
                "status": 0,
                "reason": str(e)[:120],
                "ms": int((time.time() - t0) * 1000),
                "stores": [],
            }
        if status != 200:
            return {"ok": False, "status": status, "reason": f"http {status}", "ms": ms, "stores": []}
        try:
            stores = _parse_stores(raw, self.skus)
        except json.JSONDecodeError:
            return {"ok": False, "status": status, "reason": "non-json", "ms": ms, "stores": []}
        return {"ok": True, "status": 200, "ms": ms, "stores": stores, "n": len(stores)}

    def _fetch_anchors(self, http, mode: str, node: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Query anchors in order, merge stores, stop once all targets are seen.

        R471 alone covers ~10/11 targets; a Shanghai anchor picks up the rest
        (R581). Early-exit keeps it to ~1-2 requests per shot in the common case.
        A single merged chunk is still a full snapshot, so _apply/poll downstream
        are unchanged.
        """
        merged: dict[str, dict] = {}
        statuses: list[int] = []
        reasons: list[str] = []
        used: list[str] = []
        ms_total = 0
        ok_any = False
        target_set = set(TARGET)
        for anchor in ANCHORS:
            url = build_url(self.skus, anchor)
            chunk = self._fetch(http, mode, node, url, timeout)
            used.append(anchor)
            statuses.append(int(chunk.get("status") or 0))
            ms_total += int(chunk.get("ms") or 0)
            if chunk.get("ok"):
                ok_any = True
                for s in chunk.get("stores") or []:
                    sid = s.get("storeId")
                    if sid:
                        merged[sid] = s
                if target_set.issubset(merged.keys()):
                    break
            else:
                reasons.append(str(chunk.get("reason") or f"http {chunk.get('status')}"))
                # A throttle (541/503/429) means Apple is rate-limiting THIS IP right
                # now — firing the remaining anchors just piles on more 541s and
                # deepens the throttle. Stop and let the node cool. Missing coverage
                # (e.g. R581) refreshes on a later shot when this IP isn't throttled.
                if int(chunk.get("status") or 0) in SOFT_FAIL:
                    break
        anchor_label = "+".join(used) or (ANCHORS[0] if ANCHORS else PRIMARY)
        if ok_any:
            return {
                "ok": True,
                "status": 200,
                "ms": ms_total,
                "stores": list(merged.values()),
                "n": len(merged),
                "anchor": anchor_label,
            }
        # nothing succeeded: surface the last failure so node-health/backoff still fires
        return {
            "ok": False,
            "status": statuses[-1] if statuses else 0,
            "reason": reasons[-1] if reasons else "fetch failed",
            "ms": ms_total,
            "stores": [],
            "anchor": anchor_label,
        }

    def _qg_rotator(self) -> None:
        url = self._qg_url
        while not self._stop.is_set():
            try:
                rows = extract_qg(url)
            except Exception as e:
                print(f"qg extract fail: {e}", flush=True)
                self._stop.wait(QG_RETRY)
                continue
            if not rows:
                print("qg extract empty", flush=True)
                self._stop.wait(QG_RETRY)
                continue
            bits: list[str] = []
            now = time.time()
            with self._lock:
                for i, slot in enumerate(self._qg_slots):
                    row = rows[i] if i < len(rows) else None
                    slot["fail_streak"] = 0
                    slot["soft_streak"] = 0
                    slot["kicked"] = False
                    if row is None:
                        slot["proxy"] = None
                        slot["deadline"] = 0.0
                        slot["area"] = ""
                        slot["isp"] = ""
                        slot["proxy_gen"] = int(slot.get("proxy_gen") or 0) + 1
                        slot["next_ok"] = now + 86400
                        bits.append(f"{slot['name']}=idle")
                        continue
                    slot["proxy"] = row["proxy"]
                    slot["deadline"] = float(row["deadline"])
                    slot["area"] = row.get("area") or ""
                    slot["isp"] = row.get("isp") or ""
                    slot["proxy_gen"] = int(slot.get("proxy_gen") or 0) + 1
                    slot["next_ok"] = now + i * 0.15
                    left = max(0, int(slot["deadline"] - now))
                    bits.append(
                        f"{slot['name']}={row['server']} {slot['area']} {slot['isp']} {left}s"
                    )
                soonest = min(
                    (float(s.get("deadline") or 0) for s in self._qg_slots if s.get("proxy")),
                    default=now + QG_REFRESH,
                )
            print("qg " + " | ".join(bits), flush=True)
            wait = min(QG_REFRESH, max(5.0, soonest - time.time() - QG_LEAD))
            self._stop.wait(wait)

    def _worker(self, node: dict[str, Any], delay: float) -> None:
        if delay:
            self._stop.wait(delay)
        http, mode = self._make_http(node)
        gen = node.get("proxy_gen") or 0
        ssl_fails = 0
        while not self._stop.is_set():
            if node.get("kicked") and not _is_qg_node(node):
                return
            now = time.time()
            if _is_qg_node(node):
                with self._lock:
                    cur_gen = node.get("proxy_gen") or 0
                    proxy = node.get("proxy")
                    deadline = float(node.get("deadline") or 0)
                if cur_gen != gen:
                    try:
                        if http is not None:
                            http.clear()
                    except Exception:
                        pass
                    http, mode = self._make_http(node)
                    gen = cur_gen
                    ssl_fails = 0
                if not proxy or now >= deadline:
                    self._stop.wait(0.2)
                    continue
            with self._lock:
                nxt = float(node.get("next_ok") or 0)
            if nxt > now:
                self._stop.wait(min(0.25, nxt - now))
                continue
            if node.get("src") == "tunnel":
                timeout = TUNNEL_TIMEOUT
            elif _is_qg_node(node):
                timeout = QG_TIMEOUT
            elif not node.get("proxy"):
                timeout = DIRECT_TIMEOUT
            else:
                timeout = PROXY_TIMEOUT
            with self._lock:
                # claim this slot so a second worker on the same IP cannot pile on
                node["next_ok"] = time.time() + self._cooldown(node)
                node["poll_n"] += 1
            chunk = self._fetch_anchors(http, mode, node, timeout)
            anchor = chunk.get("anchor") or (ANCHORS[0] if ANCHORS else PRIMARY)
            if not chunk.get("ok") and mode == "pool" and "SSL" in str(chunk.get("reason") or ""):
                ssl_fails += 1
                if ssl_fails >= 2:
                    mode = "curl"
                    try:
                        if http is not None:
                            http.clear()
                    except Exception:
                        pass
                    http = None
            try:
                self._q.put_nowait((node, chunk, anchor))
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._q.put_nowait((node, chunk, anchor))
                except queue.Full:
                    pass
            status = int(chunk.get("status") or 0)
            penalty_note = None
            is_tunnel = node.get("src") == "tunnel"
            with self._lock:
                if chunk.get("ok"):
                    node["fail_streak"] = 0
                    node["soft_streak"] = 0
                    node["kick_count"] = 0  # a working shot clears prior strikes
                    node["next_ok"] = time.time() + self._cooldown(node)
                elif is_tunnel:
                    # Rotating exit IP: a 541/error is just one bad exit, the NEXT
                    # request is a fresh IP. Never escalate cooldown, never kick.
                    node["fail_streak"] = 0
                    node["next_ok"] = time.time() + TUNNEL_COOLDOWN
                elif status in SOFT_FAIL:
                    node["fail_streak"] = 0
                    node["soft_streak"] = int(node.get("soft_streak") or 0) + 1
                    cool = min(40.0, SOFT_COOLDOWN * (2 ** min(3, node["soft_streak"] - 1)))
                    node["next_ok"] = time.time() + cool
                else:
                    node["fail_streak"] = int(node.get("fail_streak") or 0) + 1
                    if node.get("proxy") and node["fail_streak"] >= KICK_AFTER:
                        if _is_qg_node(node):
                            # 短效 IP，等 rotator 换，不当静态节点踢出
                            node["fail_streak"] = 0
                            node["next_ok"] = time.time() + 4.0
                        else:
                            node["kick_count"] = int(node.get("kick_count") or 0) + 1
                            node["fail_streak"] = 0
                            if node["kick_count"] >= MAX_KICKS:
                                node["kicked"] = True
                                self.kicked.append(f"{node['name']} ({node.get('proxy')})")
                                self.persist()
                                return
                            penalty = min(KICK_MAX_PENALTY, KICK_PENALTY * node["kick_count"])
                            node["next_ok"] = time.time() + penalty
                            penalty_note = f"penalty {node['name']} kick#{node['kick_count']} {penalty:.0f}s"
                    else:
                        node["next_ok"] = time.time() + backoff_seconds(node["fail_streak"], 1.0)
            if penalty_note:
                print("  " + penalty_note, flush=True)

    def _apply(self, node: dict[str, Any], chunk: dict[str, Any], anchor: str) -> dict[str, Any]:
        kicked = bool(node.get("kicked") and node.get("proxy"))
        if chunk.get("ok"):
            self._ok_times.append(time.time())
            for s in chunk.get("stores") or []:
                sid = s.get("storeId")
                if sid:
                    self.last_by_id[sid] = s
            current = {s["storeId"]: s for s in (chunk.get("stores") or []) if s.get("storeId")}
            watched = _watched_from(current)
            opened = [r for r in watched if not r.get("storeDisabled") and not r.get("missing")]
            return {
                "ok": True,
                "status": 200,
                "ms": chunk.get("ms"),
                "n": chunk.get("n"),
                "anchor": anchor,
                "node": node["name"],
                "proxy": node.get("proxy"),
                "kicked": False,
                "fail_streak": 0,
                "hangzhou": watched,
                "watched": watched,
                "open": opened,
            }
        watched = _watched_from(self.last_by_id, stale=True) if self.last_by_id else []
        return {
            "ok": False,
            "status": chunk.get("status") or 0,
            "reason": chunk.get("reason") or "fetch failed",
            "ms": chunk.get("ms"),
            "anchor": anchor,
            "node": node["name"],
            "proxy": node.get("proxy"),
            "fail_streak": node.get("fail_streak") or 0,
            "kicked": kicked,
            "hangzhou": watched,
            "watched": watched,
            "open": [],
        }

    def drain(self) -> int:
        n = 0
        while True:
            try:
                self._q.get_nowait()
                n += 1
            except queue.Empty:
                return n

    def _idle(self, wait: float) -> dict[str, Any]:
        watched = _watched_from(self.last_by_id, stale=True) if self.last_by_id else []
        return {
            "ok": False,
            "status": 0,
            "reason": "idle",
            "ms": 0,
            "backoff": max(0.0, wait),
            "hangzhou": watched,
            "watched": watched,
            "open": [],
            "pool": len(self.alive()),
            "shots": [],
            "hz": self.hz(),
        }

    def poll(self, sku=SKU_DEFAULT, timeout: float | None = None) -> dict[str, Any]:
        self.skus = [sku] if isinstance(sku, str) else list(sku)
        wait_s = 0.05 if timeout is None else min(1.0, max(0.02, float(timeout)))
        try:
            first = self._q.get(timeout=wait_s)
        except queue.Empty:
            return self._idle(0.02)
        items = [first]
        while True:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        shots: list[dict[str, Any]] = []
        best: dict[str, Any] | None = None
        any_ok = False
        with self._lock:
            for node, chunk, anchor in items:
                shot = self._apply(node, chunk, anchor)
                shots.append(shot)
                if shot.get("ok"):
                    any_ok = True
                    best = shot
        if not shots:
            return self._idle(0.02)
        watched = (best or shots[-1]).get("watched") or []
        # only the newest 200 counts — queued in-flight shots still say OPEN after stock is gone
        opened = [
            r for r in watched
            if not r.get("storeDisabled") and not r.get("missing") and not r.get("stale")
        ]
        return {
            "ok": any_ok,
            "status": (best or shots[-1]).get("status") or 0,
            "reason": None if any_ok else (shots[-1].get("reason") or "fetch failed"),
            "ms": (best or shots[-1]).get("ms"),
            "n": (best or {}).get("n"),
            "anchor": (best or shots[-1]).get("anchor"),
            "node": ",".join(s.get("node") or "?" for s in shots),
            "backoff": 0.0,
            "pool": len(self.alive()),
            "hangzhou": watched,
            "watched": watched,
            "open": opened,
            "shots": shots,
            "kicked": any(s.get("kicked") for s in shots),
            "hz": self.hz(),
        }


_poller: StockPoller | None = None
_proxies_mtime: float = 0.0


def _proxies_stamp() -> float:
    path = ROOT / "config" / "proxies.json"
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def fetch_pickup(sku=SKU_DEFAULT, timeout: float | None = None, base_interval: float = 0.12) -> dict[str, Any]:
    global _poller, _proxies_mtime
    stamp = _proxies_stamp()
    if (
        _poller is None
        or abs(_poller.base_interval - base_interval) > 0.05
        or stamp != _proxies_mtime
    ):
        if _poller is not None:
            _poller.stop()
        _poller = StockPoller(base_interval=base_interval)
        _proxies_mtime = stamp
    return _poller.poll(sku, timeout=timeout)


fetch_hangzhou = fetch_pickup


def fmt(s: dict) -> str:
    if s.get("missing"):
        flag = "—"
    elif s.get("stale"):
        flag = "OOS?" if s.get("storeDisabled") else "OPEN?"
    else:
        flag = "OOS" if s.get("storeDisabled") else "OPEN"
    name = SHORT.get(s.get("storeId") or "", s.get("storeName") or NAMES.get(s.get("storeId") or "", ""))
    quote = s.get("quote") or s.get("pickupDisplay") or ""
    return f"{s.get('storeId')} {flag} {name} {quote}".strip()
