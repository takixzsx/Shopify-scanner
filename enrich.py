#!/usr/bin/env python3
"""
enrich.py - 상위 리드 연락처 / 개인화 소재 보강

하는 일 (scan.py 결과 중 리드 점수 상위 N개에 대해서만):
  1) /pages/contact, /pages/about, 홈페이지 footer에서 연락처 이메일 추출
  2) 스토어명(홈페이지 title/og:site_name), 취급 카테고리 추정(product_type 다수결
     또는 상품명 상위 20개 키워드) 추출
  3) 품절 상품 중 이미지 수 + variant 수가 많은(=원래 잘 팔렸을 가능성이 높은)
     상위 3개 상품명/URL 추출 - draft.py의 개인화 소재로 쓰인다

사용법:
  python enrich.py scanned.csv --top 100 --out enriched.csv
"""

import argparse
import csv
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

TIMEOUT = 12
WORKERS = 6
CACHE_DIR = "raw_cache"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": UA}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
BAD_EMAIL_HINTS = (
    "sentry", "example.com", "wixpress", "@2x", "shopify.com", "klaviyo",
    "schema.org", "godaddy", "yourdomain", "domain.com", "sentry.io",
)
STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "of", "in", "to", "set",
    "new", "pack", "size", "small", "large", "medium", "mini", "kit", "co",
}

CONTACT_FORM_PATHS = ["/pages/contact", "/pages/contact-us", "/pages/support", "/pages/get-in-touch"]

OUT_FIELDS = [
    "domain", "store_name", "category", "contact_email", "contact_url",
    "products_oos", "products_total", "oos_ratio", "existing_apps", "lead_score",
    "top_oos_1_title", "top_oos_1_url",
    "top_oos_2_title", "top_oos_2_url",
    "top_oos_3_title", "top_oos_3_url",
]


def fetch(session, url):
    try:
        r = session.get(url, timeout=TIMEOUT, headers=HEADERS)
        if r.status_code == 200:
            return r
    except Exception:
        pass
    return None


def get_store_name(session, domain):
    r = fetch(session, f"https://{domain}/")
    if r is None:
        return domain
    soup = BeautifulSoup(r.text, "html.parser")
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        for sep in (" | ", " - ", " – "):
            if sep in title:
                return title.split(sep)[0].strip()
        return title
    return domain


def extract_email(html):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            addr = a["href"][7:].split("?")[0].strip()
            if addr and not any(b in addr.lower() for b in BAD_EMAIL_HINTS):
                return addr
    for m in EMAIL_RE.findall(soup.get_text(" ")):
        if not any(b in m.lower() for b in BAD_EMAIL_HINTS):
            return m
    return ""


def get_contact_email(session, domain):
    for path in ["/pages/contact", "/pages/contact-us", "/pages/about", "/"]:
        r = fetch(session, f"https://{domain}{path}")
        if r is None:
            continue
        email = extract_email(r.text)
        if email:
            return email
        time.sleep(0.2)
    return ""


def get_contact_url(session, domain):
    """이메일을 못 찾았을 때, <form>이 있는 문의 페이지 URL을 순서대로 찾는다."""
    for path in CONTACT_FORM_PATHS:
        url = f"https://{domain}{path}"
        r = fetch(session, url)
        if r is None:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        if soup.find("form"):
            return url
        time.sleep(0.2)
    return ""


def to_trimmed(p):
    variants = p.get("variants", [])
    return {
        "handle": p.get("handle", ""),
        "title": p.get("title", ""),
        "product_type": p.get("product_type", ""),
        "image_count": len(p.get("images", [])),
        "variant_count": len(variants),
        "available": bool(variants) and not all(not v.get("available", False) for v in variants),
    }


def load_cache(domain, cache_dir):
    """scan.py가 성공 시 남겨둔 raw_cache/<domain>.json 을 읽는다. 없으면 None."""
    path = os.path.join(cache_dir, f"{domain}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("products", []), data.get("store_name", ""), data.get("email", "")
    except Exception:
        return None


def get_products_page(session, domain):
    r = fetch(session, f"https://{domain}/products.json?limit=250&page=1")
    if r is None:
        return []
    try:
        return [to_trimmed(p) for p in r.json().get("products", [])]
    except Exception:
        return []


def guess_category(products):
    types = Counter(p.get("product_type", "").strip() for p in products if p.get("product_type", "").strip())
    if types:
        return types.most_common(1)[0][0]

    words = Counter()
    for p in products[:20]:
        title = p.get("title", "").lower()
        for w in re.findall(r"[a-z]+", title):
            if len(w) > 2 and w not in STOPWORDS:
                words[w] += 1
    if words:
        return ", ".join(w for w, _ in words.most_common(3))
    return ""


def top_oos_products(domain, products, n=3):
    oos = [p for p in products if not p.get("available", True)]
    oos.sort(key=lambda p: (p.get("image_count", 0), p.get("variant_count", 0)), reverse=True)

    result = []
    for p in oos[:n]:
        result.append({
            "title": p.get("title", ""),
            "url": f"https://{domain}/products/{p.get('handle', '')}",
        })
    while len(result) < n:
        result.append({"title": "", "url": ""})
    return result


def enrich_one(row, cache_dir):
    domain = row["domain"]
    session = requests.Session()

    cached = load_cache(domain, cache_dir) if cache_dir else None
    if cached:
        products, cached_name, cached_email = cached
    else:
        products, cached_name, cached_email = get_products_page(session, domain), "", ""

    store_name = cached_name or get_store_name(session, domain)
    category = guess_category(products)
    email = cached_email or get_contact_email(session, domain)
    contact_url = "" if email else get_contact_url(session, domain)
    top3 = top_oos_products(domain, products)

    out = {
        "domain": domain,
        "store_name": store_name,
        "category": category,
        "contact_email": email,
        "contact_url": contact_url,
        "products_oos": row.get("products_oos", ""),
        "products_total": row.get("products_total", ""),
        "oos_ratio": row.get("oos_ratio", ""),
        "existing_apps": row.get("existing_apps", ""),
        "lead_score": row.get("lead_score", ""),
    }
    for i, p in enumerate(top3, 1):
        out[f"top_oos_{i}_title"] = p["title"]
        out[f"top_oos_{i}_url"] = p["url"]
    return out


def main():
    ap = argparse.ArgumentParser(description="scan.py 결과 상위 리드 연락처/개인화 소재 보강")
    ap.add_argument("scanned_csv")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--out", default="enriched.csv")
    ap.add_argument("--cache-dir", default=CACHE_DIR,
                     help="scan.py가 남긴 raw_cache 디렉토리 (재요청 대신 재사용). 끄려면 ''")
    args = ap.parse_args()

    with open(args.scanned_csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    rows = [r for r in rows if r.get("discovery_method") != "none"
            and int(r.get("products_oos") or 0) > 0
            and not (r.get("existing_apps") or "").strip()]
    rows.sort(key=lambda r: -float(r.get("lead_score") or 0))
    rows = rows[:args.top]
    print(f"[*] 리드 점수 상위 {len(rows)}개 보강 시작")

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(enrich_one, r, args.cache_dir): r["domain"] for r in rows}
        for i, fut in enumerate(as_completed(futures), 1):
            out = fut.result()
            results.append(out)
            print(f"  [{i}/{len(rows)}] {out['domain']:<35} "
                  f"이메일={'있음' if out['contact_email'] else '없음':<6} "
                  f"카테고리={out['category'][:30]}")

    results.sort(key=lambda r: -float(r.get("lead_score") or 0))
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        w.writerows(results)

    print(f"\n[*] 저장: {args.out}")


if __name__ == "__main__":
    main()
