#!/usr/bin/env python3
"""
collect.py - Shopify 스토어 도메인 수집

하는 일:
  1) eachspy.com (무료 공개 Shopify 스토어 디렉토리)의 sitemap.xml에서
     니치/국가/테마별 목록 페이지 URL을 찾는다 (sitemap 기반 탐색)
  2) 각 목록 페이지를 순회하며 샘플 스토어 도메인을 긁는다 (공개 디렉토리 페이지)
     -> sitemap 탐색이 실패하면 하드코딩된 시드 목록 페이지로 폴백
  3) --merge 로 넘긴 기존 CSV/텍스트 파일들의 도메인을 병합한다
  4) www./서브도메인 정규화, 중복 제거, 죽은 도메인 제거 후 stores.txt 저장

사용법:
  python collect.py --out stores.txt --target 500
  python collect.py --out stores.txt --target 500 --merge old_stores.txt leads.csv
"""

import argparse
import csv
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree

import requests

TIMEOUT = 10
LIVENESS_WORKERS = 20
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": UA}

DIRECTORY_SITEMAP = "https://www.eachspy.com/sitemap.xml"
DOMAIN_RE = re.compile(r"get-favicon/\?domain=([a-zA-Z0-9.\-]+)")

# sitemap 탐색이 실패했을 때를 위한 폴백 시드 (직접 확인된 목록 페이지)
SEED_DIRECTORY_PAGES = [
    "https://www.eachspy.com/shopify/stores/",
    "https://www.eachspy.com/shopify/clothing-stores/",
    "https://www.eachspy.com/shopify/jewelry-stores/",
    "https://www.eachspy.com/shopify/skincare-stores/",
    "https://www.eachspy.com/shopify/home-decor-stores/",
    "https://www.eachspy.com/shopify/stores-in-united-states/",
    "https://www.eachspy.com/shopify/stores-in-united-kingdom/",
    "https://www.eachspy.com/shopify/stores-in-australia/",
    "https://www.eachspy.com/shopify/stores-in-canada/",
    "https://www.eachspy.com/shopify/pet-stores/",
]


def normalize(domain: str) -> str:
    d = domain.strip().lower()
    if not d:
        return ""
    d = d.replace("https://", "").replace("http://", "").strip("/")
    d = d.split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def discover_listing_pages_via_sitemap():
    """eachspy sitemap.xml에서 스토어 목록 페이지 URL을 찾는다. 실패하면 빈 리스트."""
    try:
        r = requests.get(DIRECTORY_SITEMAP, timeout=TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return []
        root = ElementTree.fromstring(r.content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [el.text for el in root.findall(".//sm:loc", ns) if el.text]
        pages = [l for l in locs if "/shopify/" in l and "stores" in l]
        random.shuffle(pages)
        return pages
    except Exception:
        return []


def scrape_domains_from_page(session, url):
    try:
        r = session.get(url, timeout=TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return []
        return DOMAIN_RE.findall(r.text)
    except Exception:
        return []


def collect_from_directory(target):
    session = requests.Session()
    pages = discover_listing_pages_via_sitemap()
    source = "sitemap"
    if not pages:
        print("[!] sitemap 탐색 실패 -> 시드 목록 페이지로 폴백")
        pages = list(SEED_DIRECTORY_PAGES)
        source = "seed_fallback"

    domains = set()
    for i, url in enumerate(pages, 1):
        found = scrape_domains_from_page(session, url)
        before = len(domains)
        domains.update(normalize(d) for d in found)
        print(f"  [{source}] ({i}/{len(pages)}) {url} -> +{len(domains) - before} "
              f"(누적 {len(domains)})")
        if len(domains) >= target:
            break
        time.sleep(random.uniform(0.5, 1.2))

    return domains


def merge_local_files(paths):
    domains = set()
    for path in paths:
        try:
            if path.endswith(".csv"):
                with open(path, encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    col = "domain" if reader.fieldnames and "domain" in reader.fieldnames else reader.fieldnames[0]
                    for row in reader:
                        domains.add(normalize(row[col]))
            else:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        d = normalize(line)
                        if d:
                            domains.add(d)
            print(f"[*] 병합: {path} -> 누적 {len(domains)}개")
        except FileNotFoundError:
            print(f"[!] 파일 없음, 건너뜀: {path}")
        except Exception as e:
            print(f"[!] {path} 읽기 실패 ({type(e).__name__}), 건너뜀")
    return domains


def check_alive(domain):
    try:
        r = requests.head(f"https://{domain}/", timeout=6, headers=HEADERS, allow_redirects=True)
        if r.status_code < 500:
            return domain
    except Exception:
        pass
    try:
        r = requests.get(f"https://{domain}/", timeout=6, headers=HEADERS)
        if r.status_code < 500:
            return domain
    except Exception:
        pass
    return None


def filter_alive(domains):
    alive = []
    with ThreadPoolExecutor(max_workers=LIVENESS_WORKERS) as ex:
        futures = {ex.submit(check_alive, d): d for d in domains}
        for i, fut in enumerate(as_completed(futures), 1):
            d = fut.result()
            if d:
                alive.append(d)
            if i % 50 == 0:
                print(f"  생존 확인 {i}/{len(domains)} (생존 {len(alive)})")
    return alive


def main():
    ap = argparse.ArgumentParser(description="Shopify 스토어 도메인 수집기")
    ap.add_argument("--out", default="stores.txt")
    ap.add_argument("--target", type=int, default=3000)
    ap.add_argument("--merge", nargs="*", default=[], help="병합할 기존 CSV/텍스트 파일들")
    ap.add_argument("--skip-liveness", action="store_true", help="생존 확인(HTTP 요청) 건너뛰기")
    args = ap.parse_args()

    domains = set()

    if args.merge:
        domains |= merge_local_files(args.merge)

    if len(domains) < args.target:
        print(f"[*] 디렉토리 수집 시작 (현재 {len(domains)}개, 목표 {args.target}개)")
        domains |= collect_from_directory(args.target - len(domains))

    domains = {d for d in domains if d}
    print(f"\n[*] 중복 제거 후 {len(domains)}개")

    if not args.skip_liveness:
        print("[*] 생존 확인 중...")
        domains = sorted(filter_alive(domains))
        print(f"[*] 생존 {len(domains)}개")
    else:
        domains = sorted(domains)

    with open(args.out, "w", encoding="utf-8") as f:
        for d in domains:
            f.write(d + "\n")

    print(f"\n[*] 저장: {args.out} ({len(domains)}개)")


if __name__ == "__main__":
    main()
