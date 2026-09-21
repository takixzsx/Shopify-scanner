"""
발송용 md 파일을 '복사 버튼 달린 HTML'로 바꿔줍니다.

쓰는 법:  python3 md2mail.py 발송_9월15일_11통.md
결과:     같은 이름의 .html 이 생기고, 더블클릭하면 브라우저에서 열립니다.
          버튼을 누르면 서식 없는 순수 텍스트로 복사돼서 메일 배경이 검게 안 됩니다.
"""
import html
import re
import sys
from pathlib import Path

src = Path(sys.argv[1] if len(sys.argv) > 1 else "발송_9월15일_11통.md")
text = src.read_text(encoding="utf-8")

# "## 제목" 아래의 To / 제목 / ```본문``` 을 한 덩어리로 뽑아냅니다
PAT = re.compile(
    r"^## (?P<head>[^\n]+)\n"
    r"\*\*To:\*\*\s*(?P<to>\S+)\s*\n"
    r"\*\*제목:\*\*\s*(?:`(?P<subj1>[^`]+)`|\n?```\n(?P<subj2>.+?)\n```)\s*\n"
    r"(?:\*\*본문:\*\*\s*\n)?```\n(?P<body>.*?)\n```",
    re.S | re.M)

mails = []
for m in PAT.finditer(text):
    d = m.groupdict()
    d["subj"] = d.pop("subj1") or d.pop("subj2")
    mails.append(d)

# 후속 메일(받는 사람이 여럿인 공용 문구)도 따로 집어넣습니다
follow = re.search(r"# 후속 메일.*?```\n(?P<body>.*?)\n```", text, re.S)

def esc(x):
    return html.escape(x or "", quote=True)

cards = []
for i, m in enumerate(mails):
    head = esc(m["head"])
    to = esc(m["to"])
    subj = esc(m["subj"])
    body = esc(m["body"])
    when = "지금" if "지금" in m["head"] else ""
    cards.append(f"""
<section class="card" id="m{i}">
  <h2>{head}</h2>
  <div class="row">
    <span class="lab">받는 사람</span>
    <code class="val">{to}</code>
    <button class="btn" data-target="to{i}">복사</button>
    <textarea class="hide" id="to{i}">{to}</textarea>
  </div>
  <div class="row">
    <span class="lab">제목</span>
    <code class="val">{subj}</code>
    <button class="btn" data-target="sj{i}">복사</button>
    <textarea class="hide" id="sj{i}">{subj}</textarea>
  </div>
  <div class="bodywrap">
    <div class="bodyhead">
      <span class="lab">본문</span>
      <button class="btn big" data-target="bd{i}">본문 복사</button>
    </div>
    <pre>{body}</pre>
    <textarea class="hide" id="bd{i}">{body}</textarea>
  </div>
  <label class="done"><input type="checkbox" data-k="m{i}"> 보냄</label>
</section>""")

follow_html = ""
if follow:
    fb = esc(follow.group("body"))
    follow_html = f"""
<section class="card follow">
  <h2>후속 메일 — 목요일 9/17, 기존 7곳에 답장으로</h2>
  <p class="note">Travel Skateshop · Geometric Skateshop · Faith Skate Supply · Decovasse ·
     Cornwall Baby Store · VacConnection · Baldorioty Music</p>
  <div class="bodywrap">
    <div class="bodyhead"><span class="lab">본문</span>
      <button class="btn big" data-target="fb">본문 복사</button></div>
    <pre>{fb}</pre>
    <textarea class="hide" id="fb">{fb}</textarea>
  </div>
</section>"""

OUT = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(src.stem)}</title>
<style>
:root{{--bg:#fafaf8;--card:#fff;--ink:#1a1a1a;--dim:#6b6b6b;--line:#e3e3e0;
--accent:#1f3864;--ok:#1f7a1f}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16171a;--card:#1e2024;--ink:#e8e8e6;
--dim:#9a9a97;--line:#31343a;--accent:#7aa2e3;--ok:#6fbf6f}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px 16px 80px;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif}}
.wrap{{max-width:780px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--dim);font-size:13px;margin:0 0 24px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px;margin:0 0 16px}}
.card.done{{opacity:.45}}
.card.follow{{border-color:var(--accent)}}
h2{{font-size:15px;margin:0 0 14px;color:var(--accent)}}
.row{{display:flex;align-items:center;gap:8px;margin:0 0 8px;flex-wrap:wrap}}
.lab{{font-size:11px;color:var(--dim);min-width:54px;letter-spacing:.02em}}
.val{{flex:1;min-width:0;font-size:13px;background:transparent;
overflow-wrap:anywhere;font-family:ui-monospace,SFMono-Regular,monospace}}
.btn{{border:1px solid var(--line);background:transparent;color:var(--ink);
border-radius:7px;padding:5px 12px;font-size:12px;cursor:pointer;white-space:nowrap}}
.btn:hover{{border-color:var(--accent);color:var(--accent)}}
.btn.copied{{border-color:var(--ok);color:var(--ok)}}
.btn.big{{padding:7px 16px;font-size:13px}}
.bodywrap{{margin-top:14px;border-top:1px solid var(--line);padding-top:12px}}
.bodyhead{{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}}
pre{{margin:0;padding:14px;background:var(--bg);border:1px solid var(--line);
border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;
font-family:ui-monospace,SFMono-Regular,monospace;max-height:260px;overflow:auto}}
.hide{{position:absolute;left:-9999px;opacity:0}}
.done{{display:inline-flex;align-items:center;gap:6px;margin-top:12px;
font-size:12px;color:var(--dim);cursor:pointer}}
.note{{font-size:12px;color:var(--dim);margin:-6px 0 12px}}
.tip{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:8px;padding:12px 14px;font-size:13px;margin:0 0 24px;color:var(--dim)}}
</style></head><body><div class="wrap">
<h1>{esc(src.stem)}</h1>
<p class="sub">버튼을 누르면 서식 없이 복사됩니다. 그대로 Gmail에 붙여넣으세요.</p>
<div class="tip">배경이 검게 나오는 건 에디터 서식이 딸려가서입니다.
이 페이지의 버튼은 순수 텍스트만 복사하니 그 문제가 없습니다.
혹시 다른 데서 복사할 일이 생기면 붙여넣기를 <b>Cmd+Shift+Option+V</b>로 하세요.</div>
{''.join(cards)}
{follow_html}
</div>
<script>
// file:// 에서도 되도록 두 가지 방법을 씁니다
function copyText(t){{
  if(navigator.clipboard && window.isSecureContext){{
    return navigator.clipboard.writeText(t);
  }}
  return new Promise(function(res,rej){{
    var ta=document.createElement('textarea');
    ta.value=t; ta.style.position='fixed'; ta.style.left='-9999px';
    document.body.appendChild(ta); ta.select();
    try{{ document.execCommand('copy'); res(); }}catch(e){{ rej(e); }}
    document.body.removeChild(ta);
  }});
}}
document.querySelectorAll('.btn').forEach(function(b){{
  b.addEventListener('click',function(){{
    var src=document.getElementById(b.dataset.target);
    copyText(src.value).then(function(){{
      var old=b.textContent; b.textContent='복사됨'; b.classList.add('copied');
      setTimeout(function(){{b.textContent=old;b.classList.remove('copied');}},1400);
    }}).catch(function(){{ b.textContent='실패'; }});
  }});
}});
// 보낸 것 체크는 이 브라우저에만 기억됩니다
document.querySelectorAll('.done input').forEach(function(c){{
  var k='sent_'+c.dataset.k;
  try{{ if(localStorage.getItem(k)==='1'){{c.checked=true;c.closest('.card').classList.add('done');}} }}catch(e){{}}
  c.addEventListener('change',function(){{
    c.closest('.card').classList.toggle('done',c.checked);
    try{{ localStorage.setItem(k,c.checked?'1':'0'); }}catch(e){{}}
  }});
}});
</script></body></html>"""

out = src.with_suffix(".html")
out.write_text(OUT, encoding="utf-8")
print(f"{out.name} 생성 완료 — 메일 {len(mails)}통" + (" + 후속 1건" if follow else ""))
