"""
쇼피파이 품절 스캐너 v1.1
─────────────────────────────────────────────────────────────
스토어 주소 목록을 넣으면 각 스토어의 품절 현황과 이메일을 뽑아 CSV로 저장합니다.

쓰는 법
  1) 같은 폴더에 stores.txt 를 만들고 스토어 주소를 한 줄에 하나씩 적습니다.
     https://legacy-skate-store.myshopify.com
     https://travel-skateshop.myshopify.com
  2) 터미널에서 실행:  python shopify_stock_scan.py
  3) results.csv 가 만들어집니다. 품절이 많은 순으로 정렬돼 있습니다.

필요한 것: 파이썬 3.8 이상. 설치할 패키지 없음(표준 라이브러리만 씀).
"""

import csv
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ─── 설정 ────────────────────────────────────────────────────
INPUT_FILE = "stores.txt"      # 스토어 주소 목록 파일
OUTPUT_FILE = "results.csv"    # 결과가 저장될 파일
MAX_PAGES = 10                 # 스토어당 최대 몇 페이지까지 긁을지 (250개 x 10 = 2500개)
DELAY = 1.0                    # 요청 사이 쉬는 시간(초). 너무 빠르면 차단당합니다
TIMEOUT = 20                   # 응답 대기 시간(초)
GOOD_MIN = 0.05                # 품절 variant 비율이 이 값 이상이면 후보
TOO_HIGH = 0.60                # 이 값을 넘으면 재입고를 안 하는 죽은 스토어로 봅니다
MIN_ABS = 30                   # 품절 variant가 이 개수는 넘어야 연락할 값어치가 있습니다
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
# ────────────────────────────────────────────────────────────

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# 스토어 이메일이 아닌 것들 (쇼피파이 기본 문구나 외부 서비스)
EMAIL_BLOCKLIST = ("example.com", "sentry.io", "shopify.com", "@2x", "yourdomain")

# info@ orders@ 같은 공용함은 담당자가 없거나 필터링됩니다. 사람 계정을 우선합니다.
ROLE_PREFIXES = ("info@", "orders@", "support@", "admin@", "sales@",
                 "help@", "contact@", "hello@", "noreply@", "no-reply@")


def fetch(url):
    """URL을 열어서 본문 문자열을 돌려줍니다. 실패하면 None."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            return r.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None


def normalize(url):
    """주소 끝의 / 를 떼고 https:// 를 붙여 통일합니다."""
    url = url.strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    return url


def scan_products(base):
    """
    products.json 을 페이지 단위로 끝까지 읽어서 재고 현황을 셉니다.
    쇼피파이는 한 번에 최대 250개만 주기 때문에 page 를 넘겨가며 반복해야 합니다.
    (250이 나왔다고 상품이 250개인 게 아닙니다. 잘린 겁니다.)
    """
    total_products = 0
    total_variants = 0
    sold_out_variants = 0
    fully_sold_out = []
    opened = False

    for page in range(1, MAX_PAGES + 1):
        url = f"{base}/products.json?limit=250&page={page}"
        body = fetch(url)
        if body is None:
            break
        try:
            products = json.loads(body).get("products", [])
        except json.JSONDecodeError:
            break

        opened = True
        if not products:          # 빈 페이지가 나오면 끝까지 읽은 것
            break

        for p in products:
            total_products += 1
            variants = p.get("variants", [])
            total_variants += len(variants)
            unavailable = [v for v in variants if not v.get("available", True)]
            sold_out_variants += len(unavailable)
            # 모든 옵션이 품절이면 '완전 품절'
            if variants and len(unavailable) == len(variants):
                fully_sold_out.append(p.get("title", "").strip())

        if len(products) < 250:   # 250개 미만이면 마지막 페이지
            break
        time.sleep(DELAY)

    return {
        "opened": opened,
        "total_products": total_products,
        "total_variants": total_variants,
        "sold_out_variants": sold_out_variants,
        "fully_sold_out_count": len(fully_sold_out),
        "sample_sold_out": fully_sold_out[0] if fully_sold_out else "",
        "truncated": total_products >= 250 * MAX_PAGES,
    }


def find_email(base):
    """
    이메일은 Contact 페이지가 아니라 정책 페이지에 있습니다.
    환불·개인정보 정책은 법적으로 연락처를 적게 되어 있어서 거의 항상 들어 있습니다.

    같은 페이지에 여러 개가 있으면 사람 계정을 먼저 고릅니다.
    orders@ 같은 공용함으로 보낸 영업 메일은 대부분 읽히지 않습니다.
    """
    role_fallback = ""
    for path in ("/policies/refund-policy",
                 "/policies/privacy-policy",
                 "/policies/terms-of-service"):
        body = fetch(base + path)
        if not body:
            continue
        for candidate in EMAIL_RE.findall(body):
            low = candidate.lower()
            if any(bad in low for bad in EMAIL_BLOCKLIST):
                continue
            if low.startswith(ROLE_PREFIXES):
                role_fallback = role_fallback or candidate
                continue
            return candidate          # 사람 계정을 찾으면 즉시 반환
        time.sleep(DELAY)
    return role_fallback              # 사람 계정이 없으면 공용함이라도

def verdict(row):
    """
    후보로 쓸 만한지 판정합니다.

    핵심: 완전 품절 '상품' 비율이 아니라 품절 'variant' 비율을 봅니다.
    재입고 알림은 옵션(사이즈·색상) 하나하나에 붙기 때문입니다.
    신발 가게는 완전 품절이 0개여도 사이즈 품절이 1000개일 수 있고,
    그런 가게가 사실 최고의 고객입니다.

    그리고 품절률은 높을수록 좋은 게 아닙니다.
    너무 높으면(TOO_HIGH 이상) 재입고를 아예 안 하고 과거 상품을
    목록에 남겨둔 아카이브형 스토어라 팔 대상이 아닙니다.
    """
    if not row["opened"]:
        return "안열림"
    if row["total_variants"] == 0:
        return "상품없음"

    ratio = row["sold_out_variants"] / row["total_variants"]
    note = " ·페이지한도" if row.get("truncated") else ""

    if ratio >= TOO_HIGH:
        return f"재입고 안 함 ({ratio:.0%}){note}"
    if ratio >= GOOD_MIN and row["sold_out_variants"] >= MIN_ABS:
        return f"좋은 후보 ({ratio:.0%}){note}"
    if ratio >= GOOD_MIN:
        return f"작음 ({ratio:.0%}){note}"
    return f"약함 ({ratio:.0%}){note}"


def main():
    path = Path(INPUT_FILE)
    if not path.exists():
        print(f"[!] {INPUT_FILE} 이 없습니다. 스토어 주소를 한 줄에 하나씩 적어 만들어 주세요.")
        sys.exit(1)

    raw = path.read_text(encoding="utf-8").splitlines()
    # 주석(#)과 빈 줄은 주소로 바꾸기 전에 먼저 걸러야 합니다
    raw = [l.strip() for l in raw if l.strip() and not l.strip().startswith("#")]
    stores = [normalize(l) for l in raw]
    stores = [s for s in stores if s]

    if not stores:
        print(f"[!] {INPUT_FILE} 에 주소가 없습니다.")
        sys.exit(1)

    rows = []
    for i, base in enumerate(stores, 1):
        print(f"[{i}/{len(stores)}] {base} ...", end=" ", flush=True)
        row = scan_products(base)
        row["store"] = base
        row["email"] = find_email(base) if row["opened"] else ""
        row["verdict"] = verdict(row)
        rows.append(row)
        print(f"품절 {row['sold_out_variants']}개 / 상품 {row['total_products']}개 · {row['verdict']}")
        time.sleep(DELAY)

    # 품절이 많은 순으로 정렬 — 위에 있을수록 먼저 연락할 곳
    rows.sort(key=lambda r: r["sold_out_variants"], reverse=True)

    # utf-8-sig 로 저장해야 엑셀에서 한글이 안 깨집니다
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["스토어 주소", "전체 상품 수", "전체 variant 수", "품절 variant 수",
                    "완전 품절 상품 수", "대표 품절 상품", "이메일", "판정"])
        for r in rows:
            w.writerow([r["store"], r["total_products"], r["total_variants"], r["sold_out_variants"],
                        r["fully_sold_out_count"], r["sample_sold_out"],
                        r["email"], r["verdict"]])

    good = sum(1 for r in rows if r["verdict"].startswith("좋은"))
    print(f"\n완료. {OUTPUT_FILE} 에 {len(rows)}곳을 저장했습니다. 좋은 후보 {good}곳.")


if __name__ == "__main__":
    main()
