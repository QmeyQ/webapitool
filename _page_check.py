"""检查当前活动页（chatgpt tab）：错误面板、composer、最后消息。"""
import sys
import time
from DrissionPage import ChromiumPage

port = int(sys.argv[1]) if len(sys.argv) > 1 else 9224
page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
for t in page.get_tabs():
    u = t.url or ""
    if "chatgpt.com" in u:
        print("tab:", u)
        print(t.run_js(r"""
const txt = (document.body.innerText || '');
const errIdx = txt.search(/Unknown error|出了点问题|Something went wrong|无法生成|发生错误/i);
const ta = document.querySelector('#prompt-textarea');
const turns = document.querySelectorAll('[data-turn-key],article,[data-testid^="conversation-turn"]').length;
return JSON.stringify({
  err: errIdx >= 0 ? txt.slice(errIdx, errIdx + 120).replace(/\n/g, ' | ') : null,
  taLen: ta ? (ta.innerText || ta.value || '').length : -1,
  turns: turns});"""))
