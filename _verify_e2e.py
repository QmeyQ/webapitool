"""真实 web 端端到端验证发送校验（用户要求：自己检查 web 端判断问题）。

Part A（无副作用）：向会话区注入临时文本节点模拟"消息上屏"，
验证 body_len 增长兜底判据在真实页面上能命中 sent_confirmed。
Part B（真实发送）：fill 短测试消息 → 采样 → CLICK → 轮询，
验证校验链路判"发送成功"且判据来源清晰。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from DrissionPage import ChromiumPage
from browser_core import _plain_css
from send_verify import (capture_send_state_js, poll_signal_js,
                         sent_confirmed)

port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
INP = 'div.ProseMirror[contenteditable="true"]'
SEND = '[data-testid="send-button"]'

page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
tab = next(t for t in page.get_tabs()
           if "chatgpt.com" in (t.url or ""))
print("tab:", tab.url)

def sample(tag):
    raw = tab.run_js(capture_send_state_js(INP, "[data-turn-key]"))
    d = json.loads(raw)
    print(f"  [{tag}] i={d['i']} m={d['m']} t={d['t']} a={d['a']} b={d['b']}")
    # 与 browser_core._capture_send_state 相同的字段映射
    return {"input_len": d["i"], "msg_count": d["m"],
            "turn_count": d["t"], "article_count": d["a"],
            "body_len": d["b"]}

def poll_until_confirm(pre, seconds=15.0):
    """复刻 _verify_and_recover_send._poll 的判据循环"""
    deadline = time.time() + seconds
    hit = None
    while time.time() < deadline:
        time.sleep(0.5)
        sig = tab.run_js(poll_signal_js(INP))
        if sig == 'generating':
            return 'generating'
        now = sample("poll")
        # 逐判据定位命中来源
        for k in ("turn_count", "msg_count", "article_count"):
            if pre.get(k, -1) >= 0 and now.get(k, -1) > pre.get(k, -1):
                return f'{k}增长(哨兵:{k})'
        if pre.get("input_len", -1) > 0 and now.get("input_len") == 0:
            return 'input清空'
        pa, pb = pre.get("body_len", -1), now.get("body_len", -1)
        if pa >= 0 and pb >= 0:
            grown = pb - pa
            if grown >= max(60, min(pre.get("input_len", 0) // 2, 1500)):
                return f'body_len增长{grown}'
        if not hit and sig != 'maybe_sent':
            hit = sig
    return None

print("\n== Part A: 注入模拟消息上屏（无副作用）")
pre = sample("pre")
inject = tab.run_js(
    "(function(){"
    "const p=document.createElement('div');"
    "p.id='__audit_probe_msg__';"
    "p.textContent='校验信号注入测试。' + 'X'.repeat(200);"
    "document.body.appendChild(p);"
    "return 'ok';})()")
now = sample("now")
ok = sent_confirmed(pre, now)
src = (now["body_len"] - pre["body_len"]) if ok else None
print(f"  注入后 sent_confirmed={ok} (body_len增长={src})")
tab.run_js("document.getElementById('__audit_probe_msg__').remove();")
restored = sample("restored")
print(f"  节点已移除，body_len还原差={restored['body_len'] - pre['body_len']}")
assert ok, "Part A 失败：注入模拟未被兜底判据命中"
print("  Part A PASS")

print("\n== Part B: 真实发送全链路")
fill_ok = "no-editor"
try:
    ele = tab.ele(f'css:{INP}', timeout=3)
    if ele:
        try:
            ele.clear()
        except Exception:
            pass
        try:
            ele.focus()
        except Exception:
            pass
        tab.run_cdp("Input.insertText", text="连接测试：请回复OK")
        fill_ok = "inserted"
except Exception as e:
    fill_ok = f"err:{e}"
print(f"  fill: {fill_ok}")
time.sleep(0.8)
pre_send = sample("pre_send")
assert pre_send["input_len"] > 0, "fill 未写入输入框"
clicked = tab.run_js(
    "(function(){"
    "const b=document.querySelector('[data-testid=\"send-button\"]')"
    "|| document.querySelector("
    "'button[aria-label*=\"发送\"],button[aria-label*=\"Send\" i]');"
    "if(!b) return 'no-send-btn';"
    "b.click(); return 'clicked';})()")
if clicked != 'clicked':
    tab.run_cdp("Input.dispatchKeyEvent", type="rawKeyDown",
                key="Enter", code="Enter",
                windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
    tab.run_cdp("Input.dispatchKeyEvent", type="keyUp",
                key="Enter", code="Enter",
                windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
    clicked = "enter-fallback"
print(f"  click: {clicked}")
result = poll_until_confirm(pre_send)
print(f"  校验结果: {result}")
after = sample("after")
print(f"  发送后输入框残留: {after['input_len']} 字符")
if result and result not in ('generating',):
    print("\nE2E PASS: 真实发送被判'发送成功'，判据来源 =", result)
else:
    print("\nE2E 需人工确认：未在窗口内确认判据（可能生成指示器延迟）")