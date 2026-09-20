#!/usr/bin/env python3
"""Pull free HTTP proxies (CN + overseas) that can hit Apple pickup-message.

    python3 scripts/fetch_proxies.py              # 快代理 + proxyhub + scdn + 89ip
    python3 scripts/fetch_proxies.py --kuaidaili 15
    python3 scripts/fetch_proxies.py --hub 66
    python3 scripts/fetch_proxies.py --scdn 0     # 跳过 proxy.scdn.io
    python3 scripts/fetch_proxies.py --89ip 0     # 跳过 89ip.cn
    python3 scripts/fetch_proxies.py --keep 80    # 最多留 80 条 live

Checkout never uses these. Public stock GET only, no cookies.
Kuaidaili is reachable direct from this IP (was HTTP 567, now 200). Prefer direct, fall back to live tunnels.
scdn 不限国家：能打通 www.apple.com.cn/shop/retail/pickup-message 的国外节点也收。
"""
from __future__ import annotations

import json
import gc
import os
import random
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pickup_stock import SKU_DEFAULT, fetch_one  # noqa: E402

HUB = "https://proxyhub.me/zh/cn-http-proxy-list.html"
KDL_INTR = "https://www.kuaidaili.com/free/intr/{page}"
KDL_INHA = "https://www.kuaidaili.com/free/inha/{page}"
SCDN = "https://proxy.scdn.io/"
SCDN_TEXT = "https://proxy.scdn.io/text.php"
SCDN_API = "https://proxy.scdn.io/api/get_proxy.php"
IP89 = "https://www.89ip.cn/"
IP89_API = "http://api.89ip.cn/tqdl.html?api=1&num=300"
LUMI = "https://www.lumiproxy.com/zh-hans/free-proxy/"
LUMI_API = "https://www.lumiproxy.com/get-proxy/free"
# 541 是 Apple 按出口 IP 拦。这些源还在更新，探测时亚洲优先。
PROXYSCRAPE = (
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=displayproxies&protocol=http&timeout=10000"
    "&country=all&ssl=all&anonymity=all&format=text"
)
PROXYSCRAPE_ASIA = (
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=displayproxies&protocol=http&timeout=10000"
    "&country=CN,HK,JP,SG,KR,TW&ssl=all&anonymity=all&format=text"
)
GEONODE = "https://proxylist.geonode.com/api/proxy-list"
FREE_PROXY_LIST = "https://free-proxy-list.net/"
SSL_PROXIES = "https://www.sslproxies.org/"
SPYS_ME = "https://spys.me/proxy.txt"
SPEEDX = "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"
MONOSANS = "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"
CLARKETM = "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"
JETKAI = "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"
ROOSTERKID = "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"
RDAVYDOV = "https://cdn.jsdelivr.net/gh/rdavydov/proxy-list@main/proxies/http.txt"
ALIILAPRO = "https://cdn.jsdelivr.net/gh/ALIILAPRO/Proxy@main/http.txt"
HIDEIP = "https://cdn.jsdelivr.net/gh/zloi-user/hideip.me@master/http.txt"
PROXIFLY_HTTP = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt"
PROXIFLY_COUNTRY = (
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/countries/{cc}/data.txt"
)
HPROXY_LIVE = "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/live.txt"
HPROXY_HTTP = "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/http.txt"
HPROXY_CC = "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/by-country/{cc}.txt"
DATABAY = "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt"
SUNNY9577 = "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"
OPENPROXYLIST = "https://api.openproxylist.xyz/http.txt"
PROXY4FTP = "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt"
VAKHOV = "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt"
ELLIOTT = "https://cdn.jsdelivr.net/gh/elliottophellia/yakumo@master/results/http/global/http_checked.txt"
PROXYSCRAPE_CC = (
    "https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/countries/{cc}/data.txt"
)
ASIA_CC = ("CN", "HK", "JP", "SG", "KR", "TW")
SCDN_PROTOCOLS = ("HTTP", "HTTPS")
# apple.com.cn 对欧美免费代理几乎全超时；优先这些区，全球 text.php 只作补充抽样。
SCDN_COUNTRIES = (
    "中国",
    "香港",
    "日本",
    "新加坡",
    "South Korea",
    "美国",
)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15"
)
KEEP = 200
PROBE_WORKERS = 2
SCRAPE_WORKERS = 2
PROBE_TIMEOUT = 4.0
PROBE_CAP = 20000
IPPORT = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})")
PAGE_INFO = re.compile(r"第\s*\d+\s*/\s*(\d+)\s*页")
DATA_PROXY = re.compile(r'data-proxy="(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})"')


def get(url: str, timeout: float = 15.0, referer: str | None = None) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,text/plain,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer or url,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def existing_proxies() -> list[tuple[str, str]]:
    path = ROOT / "config" / "proxies.json"
    if not path.exists():
        return []
    try:
        cfg = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(proxy: str) -> None:
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", proxy or "")
        if not m:
            return
        key = f"{m.group(1)}:{m.group(2)}"
        if key in seen:
            return
        seen.add(key)
        out.append((m.group(1), m.group(2)))

    for n in cfg.get("nodes") or []:
        if isinstance(n, dict) and (
            n.get("ephemeral") or n.get("src") == "qg" or str(n.get("name") or "").startswith("qg-")
        ):
            continue
        proxy = (n.get("proxy") if isinstance(n, dict) else None) or ""
        add(proxy)
    for kicked in cfg.get("kicked") or []:
        add(str(kicked))
    return out


def proxy_urls() -> list[str]:
    return [f"http://{ip}:{port}" for ip, port in existing_proxies()]


def get_curl(
    url: str,
    referer: str,
    timeout: float = 18.0,
    proxy: str | None = None,
    cookie: str | None = None,
) -> str:
    import subprocess

    cmd = [
        "curl",
        "-sS",
        "-L",
        "--compressed",
        "-A",
        UA,
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H",
        "Accept-Language: zh-CN,zh;q=0.9",
        "-H",
        f"Referer: {referer}",
        "-m",
        str(int(timeout)),
        "-w",
        "\n__HTTP__%{http_code}",
    ]
    if proxy:
        cmd.extend(["-x", proxy])
    if cookie:
        cmd.extend(["-c", cookie, "-b", cookie])
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 4)
    body = (r.stdout or b"").decode("utf-8", "replace")
    http = ""
    if "\n__HTTP__" in body:
        body, http = body.rsplit("\n__HTTP__", 1)
        http = http.strip()
    if r.returncode != 0 and not body:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace") or f"curl {r.returncode}")
    if http in ("403", "567", "429", "503") or (
        "567" in body[:500] and "kdl-table" not in body
    ):
        raise RuntimeError(f"http {http or 567}")
    if "kdl-table" not in body and "ip-text" not in body:
        raise RuntimeError(f"http {http or r.returncode} no table ({len(body)}b)")
    return body


def scrape_hub_page(page: int) -> tuple[str, list[tuple[str, str]]]:
    url = HUB if page <= 1 else f"{HUB}?page={page}"
    html = get(url)
    ips = re.findall(r'class="ip-text"[^>]*>\s*(\d{1,3}(?:\.\d{1,3}){3})\s*<', html)
    ports = re.findall(r'class="port-text">\s*(\d{2,5})\s*<', html)
    n = min(len(ips), len(ports))
    return f"hub/{page}", list(zip(ips[:n], ports[:n]))


def scrape_89_api() -> tuple[str, list[tuple[str, str]]]:
    html = get(IP89_API, timeout=20.0, referer="https://www.89ip.cn/api.html")
    return "89ip/api", IPPORT.findall(html)


def scrape_89_page(page: int) -> tuple[str, list[tuple[str, str]]]:
    url = IP89 if page <= 1 else f"https://www.89ip.cn/index_{page}.html"
    html = get(url, timeout=18.0, referer=IP89)
    rows = re.findall(
        r"(\d{1,3}(?:\.\d{1,3}){3})\s*</td>\s*<td[^>]*>\s*(\d{2,5})",
        html,
    )
    return f"89ip/{page}", rows


def scrape_89ip(max_pages: int) -> list[tuple[str, list[tuple[str, str]]]]:
    if max_pages <= 0:
        return []
    chunks: list[tuple[str, list[tuple[str, str]]]] = []
    try:
        chunks.append(scrape_89_api())
        print(f"  89ip/api: {len(chunks[-1][1])} listed")
    except Exception as e:
        print(f"  89ip/api failed: {e}")
    with ThreadPoolExecutor(max_workers=min(SCRAPE_WORKERS, 6)) as pool:
        futs = {pool.submit(scrape_89_page, p): p for p in range(1, max_pages + 1)}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                chunks.append(fut.result())
            except Exception as e:
                print(f"  89ip/{p} failed: {e}")
    return chunks


def scrape_kdl_page(kind: str, page: int, proxies: list[str]) -> tuple[str, list[tuple[str, str]]]:
    tmpl = KDL_INTR if kind == "intr" else KDL_INHA
    url = tmpl.format(page=page)
    referer = tmpl.format(page=max(1, page - 1))
    last_err: object = "no proxy"
    # 本机现在能直接打开（不再 567），优先直连，失败再借池里的隧道。
    start = (page + (0 if kind == "intr" else 7)) % max(1, len(proxies))
    rotated = (proxies[start:] + proxies[:start]) if proxies else []
    ordered: list[str | None] = [None]
    for p in rotated:
        if p and p not in ordered:
            ordered.append(p)
    for proxy in ordered[: min(2, len(ordered))]:
        try:
            html = get_curl(url, referer, proxy=proxy, timeout=8.0)
            rows = re.findall(
                r'<td class="kdl-table-cell">(\d{1,3}(?:\.\d{1,3}){3})</td>\s*'
                r'<td class="kdl-table-cell">(\d{2,5})</td>',
                html,
            )
            if not rows:
                rows = IPPORT.findall(html)
            if rows:
                via = (proxy or "direct").replace("http://", "")
                return f"kdl/{kind}/{page} via {via}", rows
            last_err = "empty table"
        except Exception as e:
            last_err = e
        time.sleep(0.2)
    raise RuntimeError(last_err or "kuaidaili scrape failed")


def scdn_url(page: int, protocol: str, country: str = "") -> str:
    q = {"page": page, "per_page": 100}
    if protocol:
        q["protocol"] = protocol
    if country:
        q["country"] = country
    return f"{SCDN}?{urllib.parse.urlencode(q)}"


def scrape_scdn_page(page: int, protocol: str, country: str = "") -> tuple[str, list[tuple[str, str]], int]:
    html = get(scdn_url(page, protocol, country), timeout=20.0, referer=SCDN)
    rows = DATA_PROXY.findall(html)
    last = 1
    m = PAGE_INFO.search(html)
    if m:
        last = max(1, int(m.group(1)))
    where = country or "all"
    label = f"scdn/{protocol or 'any'}/{where}/{page}"
    return label, rows, last


def scrape_scdn_text() -> tuple[str, list[tuple[str, str]]]:
    raw = get(SCDN_TEXT, timeout=30.0, referer=SCDN)
    rows = IPPORT.findall(raw)
    return "scdn/text.php", rows


def scrape_scdn_api(protocol: str, count: int = 200) -> tuple[str, list[tuple[str, str]]]:
    url = f"{SCDN_API}?{urllib.parse.urlencode({'protocol': protocol, 'count': count})}"
    raw = get(url, timeout=20.0, referer=SCDN)
    data = json.loads(raw)
    proxies = ((data.get("data") or {}).get("proxies")) or []
    rows: list[tuple[str, str]] = []
    for item in proxies:
        m = IPPORT.search(str(item))
        if m:
            rows.append((m.group(1), m.group(2)))
    return f"scdn/api/{protocol}/{count}", rows


def scrape_text_url(url: str, label: str, timeout: float = 20.0) -> tuple[str, list[tuple[str, str]]]:
    raw = get(url, timeout=timeout, referer=url)
    return label, IPPORT.findall(raw)


def scrape_geonode(country: str = "", limit: int = 500, page: int = 1) -> tuple[str, list[tuple[str, str]]]:
    q = {
        "limit": limit,
        "page": page,
        "sort_by": "lastChecked",
        "sort_type": "desc",
        "protocols": "http,https",
    }
    if country:
        q["country"] = country
    url = f"{GEONODE}?{urllib.parse.urlencode(q)}"
    raw = get(url, timeout=20.0, referer="https://proxylist.geonode.com/")
    data = json.loads(raw)
    rows: list[tuple[str, str]] = []
    for item in data.get("data") or []:
        ip = str(item.get("ip") or "").strip()
        port = str(item.get("port") or "").strip()
        if ip and port.isdigit():
            rows.append((ip, port))
    label = f"geonode/{country or 'all'}" + (f"/p{page}" if page > 1 else "")
    return label, rows


def scrape_extra() -> list[tuple[str, list[tuple[str, str]]]]:
    """Lists that still refresh. Asia-tagged chunks go first at probe time."""
    jobs: list[tuple[str, str, float]] = [
        ("proxyscrape/asia", PROXYSCRAPE_ASIA, 20.0),
        ("proxyscrape/http", PROXYSCRAPE, 20.0),
        ("proxifly/http", PROXIFLY_HTTP, 20.0),
        ("github/speedx", SPEEDX, 25.0),
        ("github/monosans", MONOSANS, 20.0),
        ("github/clarketm", CLARKETM, 20.0),
        ("github/jetkai", JETKAI, 25.0),
        ("github/roosterkid", ROOSTERKID, 15.0),
        ("github/rdavydov", RDAVYDOV, 15.0),
        ("github/aliilapro", ALIILAPRO, 15.0),
        ("github/hideip", HIDEIP, 15.0),
        ("spys.me", SPYS_ME, 15.0),
        ("free-proxy-list.net", FREE_PROXY_LIST, 15.0),
        ("sslproxies.org", SSL_PROXIES, 15.0),
        ("hproxy/live", HPROXY_LIVE, 25.0),
        ("hproxy/http", HPROXY_HTTP, 20.0),
        ("github/databay", DATABAY, 20.0),
        ("github/sunny9577", SUNNY9577, 20.0),
        ("openproxylist", OPENPROXYLIST, 20.0),
        ("github/proxy4ftp", PROXY4FTP, 25.0),
        ("github/vakhov", VAKHOV, 15.0),
        ("github/elliott", ELLIOTT, 15.0),
    ]
    for cc in ASIA_CC:
        jobs.append((f"proxifly/{cc}", PROXIFLY_COUNTRY.format(cc=cc), 15.0))
        jobs.append((f"hproxy/{cc}", HPROXY_CC.format(cc=cc), 15.0))
        jobs.append((f"proxyscrape/{cc.lower()}", PROXYSCRAPE_CC.format(cc=cc.lower()), 15.0))
    chunks: list[tuple[str, list[tuple[str, str]]]] = []
    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as pool:
        futs = {pool.submit(scrape_text_url, url, label, timeout): label for label, url, timeout in jobs}
        futs.update(
            {
                pool.submit(scrape_geonode, cc, 500 if cc == "CN" else 200): f"geonode/{cc}"
                for cc in ASIA_CC
            }
        )
        futs[pool.submit(scrape_geonode, "", 500, 1)] = "geonode/all"
        futs[pool.submit(scrape_geonode, "", 500, 2)] = "geonode/all/p2"
        futs[pool.submit(scrape_geonode, "", 500, 3)] = "geonode/all/p3"
        for fut in as_completed(futs):
            label = futs[fut]
            try:
                chunks.append(fut.result())
                print(f"  {chunks[-1][0]}: {len(chunks[-1][1])} listed")
            except Exception as e:
                print(f"  {label} failed: {e}")
    return chunks


def scrape_scdn(max_pages: int) -> list[tuple[str, list[tuple[str, str]]]]:
    if max_pages <= 0:
        return []
    chunks: list[tuple[str, list[tuple[str, str]]]] = []
    try:
        chunks.append(scrape_scdn_text())
        print(f"  scdn/text.php: {len(chunks[-1][1])} listed")
    except Exception as e:
        print(f"  scdn/text.php failed: {e}")
    for proto in ("http", "https"):
        try:
            chunks.append(scrape_scdn_api(proto, 200))
            print(f"  scdn/api/{proto}: {len(chunks[-1][1])} listed")
        except Exception as e:
            print(f"  scdn/api/{proto} failed: {e}")
    jobs: list[tuple[int, str, str]] = []
    for country in SCDN_COUNTRIES:
        for protocol in SCDN_PROTOCOLS:
            try:
                label, rows, last = scrape_scdn_page(1, protocol, country)
            except Exception as e:
                print(f"  scdn/{protocol}/{country}/1 failed: {e}")
                continue
            chunks.append((label, rows))
            last = min(last, max_pages)
            print(f"  scdn/{protocol}/{country}: {last} pages ({len(rows)} on p1)")
            for p in range(2, last + 1):
                jobs.append((p, protocol, country))
    if not jobs:
        return chunks
    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as pool:
        futs = {
            pool.submit(scrape_scdn_page, p, proto, country): (p, proto, country)
            for p, proto, country in jobs
        }
        for fut in as_completed(futs):
            p, proto, country = futs[fut]
            try:
                label, rows, _ = fut.result()
                chunks.append((label, rows))
            except Exception as e:
                print(f"  scdn/{proto}/{country}/{p} failed: {e}")
    return chunks


def merge(chunks: list[tuple[str, list[tuple[str, str]]]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for label, rows in chunks:
        fresh = 0
        for ip, port in rows:
            key = f"{ip}:{port}"
            if key in seen:
                continue
            seen.add(key)
            out.append((ip, port))
            fresh += 1
        print(f"  {label}: {len(rows)} listed, +{fresh} unique")
    return out


def probe(ip: str, port: str) -> dict:
    proxy = f"http://{ip}:{port}"
    r = fetch_one(SKU_DEFAULT, "R471", timeout=PROBE_TIMEOUT, proxy=proxy)
    r["proxy"] = proxy
    r["ip"] = ip
    r["port"] = port
    return r


def node_name(ip: str, port: str) -> str:
    parts = ip.split(".")
    return f"{parts[-2]}.{parts[-1]}:{port}"


def parse_args(argv: list[str]) -> tuple[int, int, int, int, int, int, bool]:
    hub, kdl, kdl_from, scdn, ip89, keep, extra = 3, 6, 1, 8, 12, KEEP, True
    args = list(argv)
    i = 0
    positional: list[int] = []
    while i < len(args):
        a = args[i]
        if a in ("--hub",) and i + 1 < len(args):
            hub = max(0, min(66, int(args[i + 1])))
            i += 2
            continue
        if a in ("--kdl-from", "--from") and i + 1 < len(args):
            kdl_from = max(1, min(8309, int(args[i + 1])))
            i += 2
            continue
        if a in ("--kuaidaili", "--kdl") and i + 1 < len(args):
            kdl = max(0, min(8309, int(args[i + 1])))
            i += 2
            continue
        if a in ("--scdn",) and i + 1 < len(args):
            scdn = max(0, min(50, int(args[i + 1])))
            i += 2
            continue
        if a in ("--no-scdn",):
            scdn = 0
            i += 1
            continue
        if a in ("--keep",) and i + 1 < len(args):
            keep = max(1, min(2000, int(args[i + 1])))
            i += 2
            continue
        if a in ("--89ip", "--ip89") and i + 1 < len(args):
            ip89 = max(0, min(40, int(args[i + 1])))
            i += 2
            continue
        if a in ("--no-extra",):
            extra = False
            i += 1
            continue
        if a.startswith("--"):
            i += 1
            continue
        positional.append(int(a))
        i += 1
    if positional:
        kdl = max(0, min(8309, positional[0]))
    if len(positional) >= 2:
        keep = max(1, min(2000, positional[1]))
    if kdl and kdl_from > kdl:
        kdl_from, kdl = kdl, kdl_from
    return hub, kdl, kdl_from, scdn, ip89, keep, extra


def _asia_label(label: str) -> bool:
    low = label.lower()
    return any(
        tok in low
        for tok in (
            "/asia",
            "/cn",
            "/hk",
            "/jp",
            "/sg",
            "/kr",
            "/tw",
            "/中国",
            "/香港",
            "/日本",
            "/新加坡",
            "/south korea",
            "/美国",
        )
    )


def main() -> int:
    hub_pages, kdl_pages, kdl_from, scdn_pages, ip89_pages, keep, extra = parse_args(sys.argv[1:])
    tunnels = proxy_urls()
    print(
        f"scrape kuaidaili pages={kdl_from}..{kdl_pages} via {len(tunnels)} live proxies  "
        f"proxyhub pages=1..{hub_pages}  scdn global (text.php + api + html<={scdn_pages})  "
        f"89ip pages=1..{ip89_pages}  extra={'on' if extra else 'off'}  keep={keep}"
    )
    if tunnels:
        print("  tunnels: " + ", ".join(t.replace("http://", "") for t in tunnels[:12])
              + (" ..." if len(tunnels) > 12 else ""))
    else:
        print("  no live proxies yet — kuaidaili will try direct first")

    chunks: list[tuple[str, list[tuple[str, str]]]] = []
    if kdl_pages > 0:
        # 单线程翻页：双列表并行 + curl 会把本机内存打爆。
        for kind in ("intr", "inha"):
            for p in range(kdl_from, kdl_pages + 1):
                try:
                    item = scrape_kdl_page(kind, p, tunnels)
                    print(f"  {item[0]}: {len(item[1])} listed", flush=True)
                    chunks.append(item)
                except Exception as e:
                    print(f"  kdl/{kind}/{p} failed: {e}", flush=True)
                time.sleep(0.12)
                if p % 5 == 0:
                    gc.collect()
    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as pool:
        jobs = [pool.submit(scrape_hub_page, p) for p in range(1, hub_pages + 1)]
        for fut in as_completed(jobs):
            try:
                chunks.append(fut.result())
            except Exception as e:
                print(f"  scrape failed: {e}")
    chunks.extend(scrape_scdn(scdn_pages))
    chunks.extend(scrape_89ip(ip89_pages))
    extra_chunks: list[tuple[str, list[tuple[str, str]]]] = scrape_extra() if extra else []
    chunks.extend(extra_chunks)
    asia_set = {
        f"{ip}:{port}"
        for label, listed in chunks
        if _asia_label(label)
        for ip, port in listed
    }
    rows = merge(chunks)
    already = existing_proxies()
    already_set = {f"{a}:{b}" for a, b in already}
    # continuation crawls only probe new IPs; existing stay in the file at write time
    if kdl_from > 1:
        before = len(rows)
        rows = [r for r in rows if f"{r[0]}:{r[1]}" not in already_set]
        print(f"unique {before} ({len(already)} already in pool, probe {len(rows)} new)")
    else:
        have = {f"{a}:{b}" for a, b in rows}
        for ip, port in reversed(already):
            if f"{ip}:{port}" not in have:
                rows.insert(0, (ip, port))
                have.add(f"{ip}:{port}")
        print(f"unique {len(rows)} (incl. {len(already)} existing), probe against apple pickup-message")
    if not rows:
        print("no proxies parsed")
        return 1

    random.shuffle(rows)
    rows.sort(
        key=lambda r: (
            0 if f"{r[0]}:{r[1]}" in already_set else 1,
            0 if f"{r[0]}:{r[1]}" in asia_set else 1,
        )
    )
    if len(rows) > PROBE_CAP:
        head = [r for r in rows if f"{r[0]}:{r[1]}" in already_set]
        rest = [r for r in rows if f"{r[0]}:{r[1]}" not in already_set]
        rows = head + rest[: max(0, PROBE_CAP - len(head))]
        asia_n = sum(1 for r in rows if f"{r[0]}:{r[1]}" in asia_set)
        print(
            f"probe {len(rows)} (existing {len(head)} + sample {len(rows) - len(head)} of {len(rest)}; "
            f"asia-tagged {asia_n})"
        )

    live: list[dict] = []
    t0 = time.time()
    done = 0
    gc.collect()
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        futs = {pool.submit(probe, ip, port): (ip, port) for ip, port in rows}
        for fut in as_completed(futs):
            ip, port = futs[fut]
            done += 1
            try:
                r = fut.result()
            except Exception as e:
                print(f"  {ip}:{port} ERR {e}")
                continue
            ok = r.get("ok")
            status = r.get("status")
            n = r.get("n")
            ms = r.get("ms")
            flag = "LIVE" if ok else f"fail {r.get('reason')}"
            if ok or done % 40 == 0 or status == 541:
                print(
                    f"  [{done}/{len(rows)}] {ip}:{port} {flag} "
                    f"status={status} n={n} {ms}ms  live={len(live)+int(bool(ok))}"
                )
            if ok:
                live.append(r)
    live.sort(key=lambda r: int(r.get("ms") or 99999))
    print(f"live {len(live)} in {int(time.time() - t0)}s")

    nodes = [{"name": "direct"}]
    seen_name: set[str] = {"direct"}
    seen_proxy: set[str] = set()
    for r in live:
        name = node_name(r["ip"], r["port"])
        if name in seen_name:
            name = f"{r['ip']}:{r['port']}"
        seen_name.add(name)
        seen_proxy.add(r["proxy"])
        nodes.append({"name": name, "proxy": r["proxy"], "ms": int(r.get("ms") or 0)})
    for ip, port in already:
        proxy = f"http://{ip}:{port}"
        if proxy in seen_proxy:
            continue
        name = node_name(ip, port)
        if name in seen_name:
            name = f"{ip}:{port}"
        seen_name.add(name)
        seen_proxy.add(proxy)
        nodes.append({"name": name, "proxy": proxy})
        print(f"  keep existing {proxy} (541/cooldown, not dead)")
    kicked: list[str] = []
    old_path = ROOT / "config" / "proxies.json"
    if old_path.exists():
        try:
            old = json.loads(old_path.read_text())
            kicked = [str(x) for x in (old.get("kicked") or [])]
        except json.JSONDecodeError:
            kicked = []
    out = {
        "_comment": "direct=本机。只扫公开库存，结账仍走 Safari。静态节点连续失败踢出。541 只冷却。青果短效每分钟提取 2 条，不写进本文件。",
        "source": [
            "https://share.proxy.qg.net/get?key=<YOUR_QG_KEY>&num=2&format=json",
            KDL_INTR.format(page=1),
            HUB,
            SCDN,
            SCDN_TEXT,
            SCDN_API,
            IP89,
            IP89_API,
            PROXYSCRAPE_ASIA,
            GEONODE,
            PROXIFLY_HTTP,
            SPEEDX,
            MONOSANS,
            HPROXY_LIVE,
            HPROXY_CC.format(cc="CN"),
            DATABAY,
            OPENPROXYLIST,
        ],
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kuaidaili_pages": kdl_pages,
        "kuaidaili_from": kdl_from,
        "hub_pages": hub_pages,
        "scdn_pages": scdn_pages,
        "ip89_pages": ip89_pages,
        "probed": len(rows),
        "live": len(nodes) - 1,
        "nodes": nodes,
        "kicked": kicked,
    }
    path = ROOT / "config" / "proxies.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {path}  {len(nodes)} nodes (direct + {len(nodes)-1} proxies)")
    return 0 if live else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        raise SystemExit(130)
