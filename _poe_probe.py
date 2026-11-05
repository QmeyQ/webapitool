"""poe 分片探针：扫描所有 /chat/ tab，统计分片上屏与输入框状态（只读）。"""
import json
import sys
from DrissionPage import ChromiumPage

port = int(sys.argv[1]) if len(sys.argv) > 1 else 9224
page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")

JS = """
const txt = document.body.innerText || '';
const heads = (txt.match(/\\[长消息分片 \\d+\\/\\d+\\]/g) || []);
const ta = document.querySelector('textarea');
const taLen = ta ? (ta.value||'').length : -1;
const btns = [...document.querySelectorAll('button')].filter(b =>
    /发送|Send|submit/i.test(b.getAttribute('aria-label')||'') ||
    /send/i.test(b.dataset.testid||''))
    .map(b => (b.dataset.testid||'?') + ':' + (b.disabled?'dis':'en'));
return JSON.stringify({path: location.pathname.slice(-10), heads: heads,
    taLen: taLen, sendBtns: btns.slice(0,3),
    hasMK1: txt.includes('MK1'), hasMK11: txt.includes('MK11'),
    hasHEAD9: txt.includes('HEAD9'), hasTAIL9: txt.includes('TAIL9'),
    generating: txt.includes('停止') || !!document.querySelector(
        'button[class*="stop"]'),
    tail: txt.replace(/\\s+/g,' ').slice(-200)});
"""

for t in page.get_tabs():
    u = t.url or ""
    if "/chat/" not in u:
        continue
    try:
        r = t.run_js(JS)
        d = json.loads(r) if isinstance(r, str) else {"raw": str(r)[:80]}
    except Exception as e:
        d = {"error": str(e)[:120]}
    d["tab_url"] = u[-14:]
    print(json.dumps(d, ensure_ascii=False, indent=1))
