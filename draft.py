#!/usr/bin/env python3
"""
draft.py - enrich.py 결과로 스토어별 개인화 콜드메일 초안 생성

메일 규칙:
  - 제목: "{품절개수} sold-out products on {스토어명}"
  - 본문 120단어 이내, 3단 구성 (사실 -> 문제 -> 질문)
  - 과장/매출 수치 날조/이모지/가격 언급 금지, 쉬운 영어
  - 실제 품절 상품 URL 1개 포함
  - send_to: contact_email이 있으면 그 이메일, 없으면 contact_url (문의 폼)을 발송 경로로 표시
  - short_body: 문의 폼 제출용으로 800자 이내로 더 줄인 본문

사용법:
  python draft.py enriched.csv --out drafts.csv
"""

import argparse
import csv

MAX_WORDS = 120
MAX_SHORT_CHARS = 800


def word_count(text):
    return len(text.split())


def join_examples(titles):
    titles = [t for t in titles if t]
    if not titles:
        return ""
    if len(titles) == 1:
        return titles[0]
    if len(titles) == 2:
        return f"{titles[0]} and {titles[1]}"
    return f"{titles[0]}, {titles[1]}, and {titles[2]}"


def build_email(row):
    store_name = row.get("store_name") or row.get("domain")
    oos = row.get("products_oos") or "0"
    examples = [row.get("top_oos_1_title", ""), row.get("top_oos_2_title", ""), row.get("top_oos_3_title", "")]
    example_url = row.get("top_oos_1_url", "")

    subject = f"{oos} sold-out products on {store_name}"

    example_text = join_examples(examples)
    fact = f"I checked {store_name} and found {oos} products that are sold out right now"
    fact += f", for example {example_text}." if example_text else "."

    problem = ("People who land on one of these sold-out pages can only leave the site. "
               "Right now the store keeps none of that interest.")

    ask = "Would you like a free waitlist signup added to pages like this, so you keep those visitors instead of losing them?"

    parts = [fact, problem]
    if example_url:
        parts.append(f"One example page: {example_url}")
    parts.append(ask)
    body = " ".join(parts)

    wc = word_count(body)
    if wc > MAX_WORDS:
        parts_no_link = [fact, problem, ask]
        body = " ".join(parts_no_link)

    fact_no_examples = f"I checked {store_name} and found {oos} products that are sold out right now."

    candidates = [
        " ".join([fact, problem] + ([f"One example page: {example_url}"] if example_url else []) + [ask]),
        " ".join([fact, problem, ask]),
        " ".join([fact_no_examples, problem] + ([f"One example page: {example_url}"] if example_url else []) + [ask]),
        " ".join([fact_no_examples, problem, ask]),
    ]
    short_body = next((c for c in candidates if len(c) <= MAX_SHORT_CHARS), candidates[-1])
    if len(short_body) > MAX_SHORT_CHARS:
        short_body = short_body[:MAX_SHORT_CHARS - 3].rsplit(" ", 1)[0] + "..."

    return subject, body, word_count(body), short_body


def main():
    ap = argparse.ArgumentParser(description="enrich.py 결과로 개인화 콜드메일 초안 생성")
    ap.add_argument("enriched_csv")
    ap.add_argument("--out", default="drafts.csv")
    args = ap.parse_args()

    with open(args.enriched_csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    drafts = []
    over_limit = 0
    no_channel = 0
    for row in rows:
        subject, body, wc, short_body = build_email(row)
        if wc > MAX_WORDS:
            over_limit += 1

        send_to = row.get("contact_email") or row.get("contact_url") or ""
        if not send_to:
            no_channel += 1

        drafts.append({
            "domain": row["domain"], "subject": subject, "body": body, "word_count": wc,
            "send_to": send_to, "short_body": short_body,
        })

    fieldnames = ["domain", "subject", "body", "word_count", "send_to", "short_body"]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(drafts)

    print(f"[*] {len(drafts)}개 초안 생성, 저장: {args.out}")
    if over_limit:
        print(f"[!] 120단어 초과 {over_limit}개 - 확인 필요")
    if no_channel:
        print(f"[!] 이메일도 문의폼도 없어 발송 경로 없음 {no_channel}개")


if __name__ == "__main__":
    main()
