#!/usr/bin/env python3
"""
scan.py - Shopify 스토어 진단 (재입고 알림 앱 리드 스캐너 W1)

하는 일:
  1) 도메인 목록(텍스트, 한 줄에 하나)을 읽는다
  2) /products.json 으로 품절 상품 수를 센다. 막히면 sitemap_products_1.xml ->
     개별 상품 .json 조회로 폴백한다
  3) 홈페이지 + 품절 상품 페이지 1개의 <script> 태그에서 재입고 알림 앱 흔적을 찾는다
  4) 통화, Shopify Plus 여부(추정), 리뷰 앱 유무로 스토어 규모를 추정한다
  5) robots.txt를 존중하고, 차단 시 지수 백오프로 재시도하며, 요청 간격을 무작위화한다
  6) 진행 상황을 CSV에 즉시 기록해 --resume 으로 중단 지점부터 이어갈 수 있다

사용법:
  python scan.py stores.txt --out scanned.csv --resume
  python scan.py stores.txt --out scanned.csv --verify --workers 6
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

TIMEOUT = 12
MAX_JSON_PAGES = 8            # products.json 페이지 상한 (250 * 8 = 2000개)
FALLBACK_MAX_PRODUCTS = 150   # sitemap 폴백 시 개별 .json 조회 상한 (시간 예산 보호)
VERIFY_TOP_N = 5
RETRY_MAX = 3
RETRY_BASE_DELAY = 1.5
REQUEST_JITTER = (0.4, 1.1)   # 같은 스토어 내 요청 간 대기(초)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": UA}

# script 태그(src + inline) 안에서만 찾는다 - 본문 텍스트("품절되면 알려드릴게요" 같은
# 테마 기본 문구)에 매칭되어 오탐하는 것을 막기 위함
APP_SIGNATURES = {
    "swym":           ["swymrelay.com", "swymstore", "swym-plugin", "swym.js"],
    "klaviyo":        ["klaviyo.com", "klaviyojs", "_learnq"],
    "back_in_stock":  ["backinstock.org", "back-in-stock-widget", "backinstockapp"],
    "restock_rocket": ["restockrocket.io"],
    "stoq":           ["stoqapp.com", "cdn.stoqapp"],
    "appikon":        ["appikon.com", "cdn.appikon"],
    "notify_me":      ["notifyme.io", "notify-me-app"],
    "alert_me":       ["alertme.io"],
}

REVIEW_APP_SIGNATURES = {
    "judge_me":  ["judge.me", "judgeme"],
    "loox":      ["loox.io", "loox.app"],
    "yotpo":     ["yotpo.com"],
    "stamped":   ["stamped.io"],
    "okendo":    ["okendo.io"],
    "reviews_io": ["reviews.io"],
}

ROW_FIELDS = [
    "domain", "discovery_method", "products_total", "products_oos", "oos_ratio",
    "products_json_ok", "shopify_plus_guess", "currency", "review_apps",
    "existing_apps", "existing_apps_source", "verify_checked", "verify_mismatches",
    "sample_oos_urls", "lead_score", "note",
]

_robots_cache = {}
_robots_lock = threading.Lock()


def normalize(domain: str) -> str:
    d = domain.strip()
    if not d or d.startswith("#"):
        return ""
    d = d.replace("https://", "").replace("http://", "").strip("/")
    d = d.split("/")[0]
    return d.lower()


def get_robots(domain):
    with _robots_lock:
        if domain in _robots_cache:
            return _robots_cache[domain]
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"https://{domain}/robots.txt")
    try:
        r = requests.get(f"https://{domain}/robots.txt", timeout=TIMEOUT, headers=HEADERS)
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
        else:
            rp = None  # robots.txt 없음/접근불가 -> 기본 허용
    except Exception:
        rp = None
    with _robots_lock:
        _robots_cache[domain] = rp
    return rp


def robots_allowed(domain, url):
    rp = get_robots(domain)
    if rp is None:
        return True
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def fetch(session, url, domain=None):
    """지수 백오프 재시도 + robots.txt 확인 포함된 GET"""
    if domain and not robots_allowed(domain, url):
        return None, "robots_disallow"

    delay = RETRY_BASE_DELAY
    for attempt in range(1, RETRY_MAX + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, headers=HEADERS)
        except Exception:
            r = None

        if r is not None and r.status_code == 200:
            return r, None
        if r is not None and r.status_code == 404:
            return None, "not_found"
        if r is not None and r.headers.get("cf-mitigated") == "challenge":
            # 봇 탐지 챌린지 페이지 - 재시도해도 안 풀린다. 우회 시도하지 않고 바로 포기.
            return None, "cloudflare_challenge"
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            return None, f"http_{r.status_code}"

        if attempt < RETRY_MAX:
            time.sleep(delay + random.uniform(0, 0.5))
            delay *= 2
    return None, "unreachable_after_retry"


def jitter_sleep():
    time.sleep(random.uniform(*REQUEST_JITTER))


def trim_product(p):
    variants = p.get("variants", [])
    return {
        "handle": p.get("handle", ""),
        "title": p.get("title", ""),
        "product_type": p.get("product_type", ""),
        "image_count": len(p.get("images", [])),
        "variant_count": len(variants),
        "available": bool(variants) and not all(not v.get("available", False) for v in variants),
    }


def scan_via_products_json(session, domain):
    """1차 시도: /products.json 페이지네이션. 반환: (total, oos, oos_urls, ok, err, products_cache)"""
    total, oos = 0, 0
    oos_urls = []
    reachable = False
    last_err = None
    cache = []

    for page in range(1, MAX_JSON_PAGES + 1):
        url = f"https://{domain}/products.json?limit=250&page={page}"
        r, err = fetch(session, url, domain)
        if r is None:
            return total, oos, oos_urls, reachable, err, cache
        try:
            data = r.json()
        except Exception:
            return total, oos, oos_urls, reachable, "invalid_json", cache
        products = data.get("products", [])
        if not products:
            break
        reachable = True

        for p in products:
            total += 1
            cache.append(trim_product(p))
            variants = p.get("variants", [])
            if variants and all(not v.get("available", False) for v in variants):
                oos += 1
                if len(oos_urls) < VERIFY_TOP_N:
                    oos_urls.append(f"https://{domain}/products/{p.get('handle', '')}")

        if len(products) < 250:
            break
        jitter_sleep()

    return total, oos, oos_urls, reachable, last_err, cache


def scan_via_sitemap_fallback(session, domain):
    """2차 폴백: sitemap_products_1.xml 목록 -> 상품별 .json 개별 조회"""
    url = f"https://{domain}/sitemap_products_1.xml"
    r, err = fetch(session, url, domain)
    if r is None:
        return 0, 0, [], False, err, []

    try:
        root = ElementTree.fromstring(r.content)
    except Exception:
        return 0, 0, [], False, "invalid_xml", []

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [el.text for el in root.findall(".//sm:loc", ns) if el.text]
    if not locs:
        locs = [el.text for el in root.findall(".//loc") if el.text]
    locs = locs[:FALLBACK_MAX_PRODUCTS]
    if not locs:
        return 0, 0, [], False, "empty_sitemap", []

    total, oos = 0, 0
    oos_urls = []
    reachable = False
    cache = []

    def fetch_one(product_url):
        json_url = product_url.rstrip("/") + ".json"
        r, err = fetch(session, json_url, domain)
        if r is None:
            return None
        try:
            return r.json().get("product")
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(fetch_one, loc) for loc in locs]
        for fut in as_completed(futures):
            p = fut.result()
            if p is None:
                continue
            reachable = True
            total += 1
            cache.append(trim_product(p))
            variants = p.get("variants", [])
            if variants and all(not v.get("available", False) for v in variants):
                oos += 1
                if len(oos_urls) < VERIFY_TOP_N:
                    oos_urls.append(f"https://{domain}/products/{p.get('handle', '')}")

    return total, oos, oos_urls, reachable, None, cache


def scripts_blob(html):
    """script 태그(src attr + inline content)만 뽑아 합친 소문자 문자열"""
    soup = BeautifulSoup(html, "html.parser")
    parts = []
    for tag in soup.find_all("script"):
        if tag.get("src"):
            parts.append(tag["src"])
        if tag.string:
            parts.append(tag.string)
    return " ".join(parts).lower()


def extract_home_meta(html):
    """홈페이지 응답 하나에서 스토어명/이메일을 기회적으로 뽑아둔다.
    (enrich.py가 나중에 같은 도메인을 또 요청하면 Cloudflare에 다시 막힐 수 있어,
    scan.py가 이미 뚫었을 때 얻은 응답을 캐시에 같이 남겨 재요청을 줄인다)"""
    soup = BeautifulSoup(html, "html.parser")
    store_name = ""
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        store_name = og["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
        for sep in (" | ", " - ", " – "):
            if sep in title:
                title = title.split(sep)[0].strip()
                break
        store_name = title

    email = ""
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            addr = a["href"][7:].split("?")[0].strip()
            if addr:
                email = addr
                break

    return store_name, email


def detect_apps(session, domain, oos_product_url):
    """홈페이지 + 품절 상품 페이지 1개의 script에서 재입고 앱 / 리뷰 앱 흔적 탐색.
    부가적으로 홈페이지에서 스토어명/이메일도 같이 뽑아 반환한다."""
    found_apps = set()
    found_reviews = set()
    sources = []
    store_name = ""
    home_email = ""

    pages = [("home", f"https://{domain}/")]
    if oos_product_url:
        pages.append(("oos_product", oos_product_url))

    for label, url in pages:
        r, err = fetch(session, url, domain)
        if r is None:
            continue
        blob = scripts_blob(r.text)

        page_hit = False
        for app, sigs in APP_SIGNATURES.items():
            if any(s in blob for s in sigs):
                found_apps.add(app)
                page_hit = True
        for app, sigs in REVIEW_APP_SIGNATURES.items():
            if any(s in blob for s in sigs):
                found_reviews.add(app)

        if label == "home":
            store_name, home_email = extract_home_meta(r.text)

        if page_hit:
            sources.append(label)
        jitter_sleep()

    return sorted(found_apps), sorted(found_reviews), ",".join(sources), store_name, home_email


def guess_currency(session, domain):
    r, err = fetch(session, f"https://{domain}/cart.json", domain)
    if r is None:
        return ""
    try:
        return r.json().get("currency", "")
    except Exception:
        return ""


def guess_shopify_plus(session, domain):
    """best-effort: 브랜드 체크아웃 서브도메인(checkout.example.com)이 응답하면 Plus 가능성 높음.
    확인 불가 시 'unknown' - 없다고 단정하지 않는다 (오탐보다 unknown이 안전)"""
    root_parts = domain.split(".")
    if len(root_parts) < 2:
        return "unknown"
    root = ".".join(root_parts[-2:])
    url = f"https://checkout.{root}/"
    try:
        r = session.get(url, timeout=6, headers=HEADERS, allow_redirects=True)
        if r.status_code == 200 and "shopify" in r.text.lower():
            return "likely"
    except Exception:
        pass
    return "unknown"


def verify_oos_sample(session, domain, oos_urls):
    """상위 N개 품절 상품 페이지의 JSON-LD schema.org availability로 교차검증"""
    checked = 0
    mismatches = 0
    for url in oos_urls[:VERIFY_TOP_N]:
        r, err = fetch(session, url, domain)
        if r is None:
            continue
        checked += 1
        soup = BeautifulSoup(r.text, "html.parser")
        availability = None
        for script in soup.find_all("script", type="application/ld+json"):
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                offers = cand.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if isinstance(offers, dict) and offers.get("availability"):
                    availability = offers["availability"].lower()
        if availability and "outofstock" not in availability and "out_of_stock" not in availability:
            mismatches += 1
        jitter_sleep()
    return checked, mismatches


def compute_lead_score(oos, oos_ratio, products_total, existing_apps):
    score = 0
    if oos >= 5:
        score += 25
    if oos >= 20:
        score += 25
    if oos_ratio >= 0.10:
        score += 20
    if products_total >= 50:
        score += 10
    if not existing_apps:
        score += 20
    elif existing_apps == ["klaviyo"]:
        score += 10
    return score


def save_cache(cache_dir, domain, products, store_name, email):
    if not cache_dir:
        return
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{domain}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"store_name": store_name, "email": email, "products": products}, f)
    except Exception:
        pass


def scan_one(domain, verify=False, cache_dir=None):
    domain = normalize(domain)
    if not domain:
        return None

    session = requests.Session()
    row = {f: "" for f in ROW_FIELDS}
    row.update({
        "domain": domain, "discovery_method": "none", "products_total": 0,
        "products_oos": 0, "oos_ratio": 0.0, "products_json_ok": False,
        "shopify_plus_guess": "unknown", "verify_checked": 0, "verify_mismatches": 0,
        "lead_score": 0,
    })

    try:
        total, oos, oos_urls, ok, err1, cache = scan_via_products_json(session, domain)
        method = "products_json"
        err2 = None

        if not ok:
            total, oos, oos_urls, ok, err2, cache = scan_via_sitemap_fallback(session, domain)
            method = "sitemap_fallback"

        row["products_total"] = total
        row["products_oos"] = oos
        row["oos_ratio"] = round(oos / total, 3) if total else 0.0
        row["products_json_ok"] = ok
        row["discovery_method"] = method if ok else "none"
        row["sample_oos_urls"] = ",".join(oos_urls[:3])

        if not ok:
            row["note"] = f"products.json({err1}) / sitemap({err2}) 모두 접근 불가"
            return row

        apps, reviews, sources, store_name, home_email = detect_apps(
            session, domain, oos_urls[0] if oos_urls else None)
        row["existing_apps"] = ",".join(apps)
        row["existing_apps_source"] = sources
        row["review_apps"] = ",".join(reviews)
        row["currency"] = guess_currency(session, domain)
        row["shopify_plus_guess"] = guess_shopify_plus(session, domain)

        if verify and oos_urls:
            checked, mismatches = verify_oos_sample(session, domain, oos_urls)
            row["verify_checked"] = checked
            row["verify_mismatches"] = mismatches

        row["lead_score"] = compute_lead_score(oos, row["oos_ratio"], total, apps)

        save_cache(cache_dir, domain, cache, store_name, home_email)

    except Exception as e:
        row["note"] = f"error: {type(e).__name__}: {e}"

    return row


class ResultWriter:
    def __init__(self, outfile, resume):
        self.outfile = outfile
        self.lock = threading.Lock()
        self.done_domains = set()

        if resume:
            try:
                with open(outfile, encoding="utf-8-sig") as f:
                    for r in csv.DictReader(f):
                        self.done_domains.add(r["domain"])
            except FileNotFoundError:
                pass

        mode = "a" if resume and self.done_domains else "w"
        self.f = open(outfile, mode, newline="", encoding="utf-8-sig")
        self.writer = csv.DictWriter(self.f, fieldnames=ROW_FIELDS)
        if mode == "w":
            self.writer.writeheader()
            self.f.flush()

    def write(self, row):
        with self.lock:
            self.writer.writerow(row)
            self.f.flush()

    def close(self):
        self.f.close()


def main():
    ap = argparse.ArgumentParser(description="Shopify 스토어 진단 스캐너")
    ap.add_argument("stores_file")
    ap.add_argument("--out", default="scanned.csv")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--verify", action="store_true", help="상위 5개 품절 상품 교차검증")
    ap.add_argument("--cache-dir", default="raw_cache",
                     help="성공한 도메인의 원본 상품/이메일 데이터를 저장할 디렉토리 (enrich.py가 재사용). 끄려면 ''")
    args = ap.parse_args()

    with open(args.stores_file, encoding="utf-8") as f:
        domains = sorted({normalize(line) for line in f if line.strip()})
    domains = [d for d in domains if d]

    writer = ResultWriter(args.out, args.resume)
    todo = [d for d in domains if d not in writer.done_domains]
    print(f"[*] 전체 {len(domains)}개 중 스캔 대상 {len(todo)}개 "
          f"(이미 완료 {len(writer.done_domains)}개 건너뜀)")

    count = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(scan_one, d, args.verify, args.cache_dir): d for d in todo}
        for fut in as_completed(futures):
            r = fut.result()
            if not r:
                continue
            writer.write(r)
            count += 1
            flag = "*" if r["lead_score"] >= 70 else " "
            print(f"{flag} [{count}/{len(todo)}] {r['domain']:<35} "
                  f"방식={r['discovery_method']:<16} "
                  f"품절 {r['products_oos']:>4}/{r['products_total']:<5} "
                  f"앱={r['existing_apps'] or '없음':<20} 점수={r['lead_score']}")

    writer.close()
    print(f"\n[*] 저장: {args.out}")


if __name__ == "__main__":
    main()
