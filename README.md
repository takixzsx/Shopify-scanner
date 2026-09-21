# Shopify 품절 스캐너

Shopify 스토어의 품절 상황을 공개 엔드포인트로 조사하고, 재입고 알림이
필요해 보이는 스토어를 추려 개인화된 문의 메일 초안까지 만드는 파이프라인.

## 파이프라인

```
collect.py  →  scan.py  →  enrich.py  →  draft.py
 도메인 수집    품절 진단     연락처 보강    메일 초안
```

| 단계 | 스크립트 | 하는 일 |
|---|---|---|
| 1 | `collect.py` | 공개 Shopify 디렉토리의 sitemap에서 스토어 도메인을 모으고, 정규화·중복 제거·죽은 도메인 제거 |
| 2 | `scan.py` | `/products.json`으로 품절 상품 수를 센다. 막히면 sitemap → 개별 상품 JSON으로 폴백. 통화·Shopify Plus 여부·설치된 앱으로 규모 추정 |
| 3 | `enrich.py` | 상위 리드만 골라 연락처 이메일·스토어명·카테고리, 그리고 "원래 잘 팔렸을" 품절 상품 3개를 뽑는다 |
| 4 | `draft.py` | 스토어별 개인화 콜드메일 초안 생성 (120단어 이내, 사실 → 문제 → 질문) |

부속 스크립트:

- `shopify_stock_scan.py` — 의존성 없는 v1 단독 스캐너 (표준 라이브러리만 사용)
- `md2mail.py` — 발송용 마크다운을 복사 버튼 달린 HTML로 변환

## 실행

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python collect.py --out stores.txt --target 500
python scan.py    stores.txt --out scanned.csv --resume
python enrich.py  scanned.csv --top 100 --out enriched.csv
python draft.py   enriched.csv --out drafts.csv
```

`scan.py`는 `--resume`으로 중단 지점부터 이어갈 수 있다. 진행 상황을 CSV에
즉시 기록하기 때문이다.

## 수집 원칙

- `robots.txt`를 존중한다
- 요청 간격을 무작위화하고, 차단당하면 지수 백오프로 물러난다
- 공개 엔드포인트(`/products.json`, sitemap, 공개 연락처 페이지)만 읽는다
- 메일 초안은 매출 수치를 날조하지 않는다. 실제로 확인한 품절 상품 URL만 인용한다

## 커밋하지 않는 것

스캔 결과 CSV, 도메인 목록, 메일 초안, 발송 기록은 `.gitignore`에 있다.
제3자 업체의 연락처가 들어있어 git 히스토리에 영구히 남기지 않는다.
