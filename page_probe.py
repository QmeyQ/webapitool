"""
page_probe.py - 只读网页状态探针（工具类）

连接指定 CDP 端口的浏览器，找到目标站点的会话 tab，打印真实页面状态。
铁律：绝不点击、绝不输入、绝不 quit()——只读。

用法：
    python page_probe.py                # 默认 9222，找 chatgpt/poe tab
    python page_probe.py 9222 chatgpt.com
"""

import sys
import json
from DrissionPage import ChromiumPage


def probe(port: int = 9222, domain: str = "") -> dict:
    page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
    target = None
    for tab in page.get_tabs():
        url = tab.url or ""
        if domain and domain not in url:
            continue
        # 只要会话页（chatgpt.com/c/ 或 poe.com/chat/），不要首页
        if "/c/" in url or "/chat/" in url:
            target = tab
            break
        if not target:
            target = tab
    if target is None:
        return {"error": f"未找到包含 {domain or '任意站点'} 的 tab"}

    js = """
    const turns = document.querySelectorAll('[data-turn-key]').length;
    const inp = document.querySelector('#prompt-textarea, div[contenteditable="true"], textarea');
    const inpText = inp ? ((inp.value != null) ? inp.value : (inp.textContent || '')) : '';
    const stop = document.querySelector('[data-testid="stop-button"],[data-testid="stop"],[data-beam][data-active]');
    const genBtn = document.querySelector('button[aria-label*="停止"],button[aria-label*="Stop"],button[aria-label*="stop"]');
    // 取最后 2 个 turn 的纯文本（每个截断 400 字符）
    const tks = document.querySelectorAll('[data-turn-key]');
    const last = [];
    for (let i = Math.max(0, tks.length - 2); i < tks.length; i++) {
        last.push((tks[i].innerText || '').slice(0, 400));
    }
    return JSON.stringify({url: location.pathname, turns: turns,
        inputLen: inpText.length, generating: !!(stop || genBtn),
        lastTurns: last});
    """
    try:
        r = target.run_js(js)
        d = json.loads(r) if isinstance(r, str) else {"raw": str(r)}
    except Exception as e:
        d = {"error": f"run_js 失败: {e}"}
    d["tab_url"] = target.url
    d["tab_title"] = target.title
    return d


_DIAG_JS = """
const inp = document.querySelector('#prompt-textarea, div[contenteditable="true"], textarea');
const inpText = inp ? ((inp.value != null) ? inp.value : (inp.textContent || '')) : '';
let btn = document.querySelector('[data-testid="send-button"], button[aria-label*="发送"], button[aria-label*="Send"]');
let btnState = 'none';
if (btn) { btnState = (btn.disabled ? 'disabled' : 'enabled') + '/visible=' + (!!btn.offsetParent); }
const turns = document.querySelectorAll('[data-turn-key]').length;
const errs = [];
for (const e of document.querySelectorAll('[role="alert"], [class*="error"], [class*="Error"]')) {
    const t = (e.innerText || '').trim();
    if (t && t.length < 200 && !errs.includes(t)) errs.push(t);
}
const toasts = [];
for (const e of document.querySelectorAll('[data-state="open"], [role="status"]')) {
    const t = (e.innerText || '').trim().slice(0, 150);
    if (t) toasts.push(t);
}
return JSON.stringify({url: location.pathname, turns: turns,
    inputLen: inpText.length, sendBtn: btnState,
    errs: errs.slice(0, 5), toasts: toasts.slice(0, 3)});
"""


def probe_all(port: int = 9222, domain: str = "") -> list:
    """列出该浏览器上目标站点的所有 tab 诊断状态（发送按钮/报错/toast）。"""
    page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
    out = []
    for tab in page.get_tabs():
        url = tab.url or ""
        if domain and domain not in url:
            continue
        try:
            r = tab.run_js(_DIAG_JS)
            d = json.loads(r) if isinstance(r, str) else {"raw": str(r)}
        except Exception as e:
            d = {"error": str(e)}
        d["tab_url"] = url[:100]
        out.append(d)
    return out


def watch(port: int = 9222, domain: str = "", seconds: int = 60,
          interval: float = 2.0):
    """实时盯页面：只打印有变化的行（输入框有内容/轮数变化/生成中）。"""
    import time as _t
    page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
    deadline = _t.time() + seconds
    last_sig = {}
    while _t.time() < deadline:
        for tab in page.get_tabs():
            url = tab.url or ""
            if domain and domain not in url:
                continue
            try:
                r = tab.run_js(_DIAG_JS)
                d = json.loads(r) if isinstance(r, str) else {}
            except Exception:
                continue
            sig = (d.get("turns"), d.get("inputLen"), d.get("generating"),
                   d.get("sendBtn"))
            if sig == last_sig.get(url) and not (d.get("inputLen") or 0):
                continue
            if sig != last_sig.get(url):
                last_sig[url] = sig
                print(f"[{_t.strftime('%H:%M:%S')}] {url[:60]} "
                      f"turns={d.get('turns')} in={d.get('inputLen')} "
                      f"gen={d.get('generating')} btn={d.get('sendBtn')} "
                      f"err={d.get('errs')[:1]} toast={d.get('toasts')[:1]}",
                      flush=True)
        _t.sleep(interval)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    domain = sys.argv[2] if len(sys.argv) > 2 else ""
    watch_arg = next((a for a in sys.argv if a.startswith("--watch")), None)
    if "--all" in sys.argv:
        print(json.dumps(probe_all(port, domain), ensure_ascii=False, indent=2))
    elif watch_arg is not None:
        secs = int(watch_arg.split("=")[1]) if "=" in watch_arg else 60
        watch(port, domain, seconds=secs)
    else:
        print(json.dumps(probe(port, domain), ensure_ascii=False, indent=2))
