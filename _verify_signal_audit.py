"""审计发送校验信号在真实页面上的有效性：逐 tab 检查
capture_send_state_js 的 i/m/t、选择器命中率、生成指示器、
article 数与排除输入框后的全页文本长度（通用兜底信号基线）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from DrissionPage import ChromiumPage
from browser_core import _plain_css
from send_verify import capture_send_state_js, poll_signal_js

port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
sites = json.loads(Path("sites.json").read_text(encoding="utf-8"))

PROBE_JS = (
    "const inp = document.querySelector(__INP__);"
    "let inpFound = !!inp;"
    "let bodyLen = -1;"
    "try {"
    "  const clone = document.body.cloneNode(true);"
    "  const ed = __INP__ ? clone.querySelector(__INP__) : null;"
    "  if (ed) ed.remove();"
    "  bodyLen = ((clone.innerText || '').replace(/\\s+/g, '')).length;"
    "} catch (e) {}"
    "return JSON.stringify({inpFound: inpFound,"
    "msgHit: __MSG__ ? document.querySelectorAll(__MSG__).length : -1,"
    "articles: document.querySelectorAll('article').length,"
    "bodyLen: bodyLen,"
    "stops: {"
    "'testid_stop': !!document.querySelector("
    "'[data-testid=\"stop-button\"],[data-testid=\"stop\"]'),"
    "'testid_any_i': !!document.querySelector('[data-testid*=\"stop\" i]'),"
    "'beam': !!document.querySelector('[data-beam][data-active]'),"
    "'aria_cn': !!document.querySelector('button[aria-label*=\"停止\"]'),"
    "'aria_any_i': !!document.querySelector('button[aria-label*=\"stop\" i]'),"
    "'gen_testid': !!document.querySelector("
    "'[data-testid*=\"generating\" i],[class*=\"generating\" i],"
    "[class*=\"thinking\" i]')"
    "}});")

page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
seen = set()
for t in page.get_tabs():
    url = t.url or ""
    if url.startswith("chrome") or "newtab" in url:
        continue
    host = ""
    for dom in sites:
        if dom.replace("www.", "") in url.replace("www.", ""):
            host = dom
            break
    if not host or host in seen:
        continue
    seen.add(host)
    sel = sites[host].get("selectors") or {}
    inp = _plain_css(sel.get("input_box") or "")
    msg = _plain_css(sel.get("message_wrapper")
                     or sel.get("result_container") or "")
    print(f"\n===== {host}  {url[:80]}")
    print(f"  input_box={inp!r}  msg={msg!r}")
    try:
        state = json.loads(t.run_js(capture_send_state_js(inp, msg)))
        print(f"  state: input_len={state.get('i')} "
              f"msg_count={state.get('m')} turn={state.get('t')}")
    except Exception as e:
        print(f"  state 采集失败: {e}")
        continue
    probe_js = PROBE_JS.replace("__INP__", json.dumps(inp))
    probe_js = probe_js.replace("__MSG__", json.dumps(msg or ""))
    try:
        probe = t.run_js(probe_js)
        print(f"  probe: {probe}")
    except Exception as e:
        print(f"  probe 失败: {e}")
    try:
        sig = t.run_js(poll_signal_js(inp))
        print(f"  poll_signal: {sig}")
    except Exception as e:
        print(f"  poll 失败: {e}")