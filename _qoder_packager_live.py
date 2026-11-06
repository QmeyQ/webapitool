"""真实两轮回放：验证"先压缩、再按压缩后的大小决定打包"。

用窗口上龙虾那段真实对话所在的新会话（同一网页对话）跑两轮：
  轮 A：把仓库真实源码文本（约 3k 字符，超过 max_prompt_length=2000）
        连同一个工具表发进去 → 策略应为 file（附件）
  轮 B：模拟 IDE 每轮重放历史——同一份内容 + assistant 回复 + 新增一行
        原文仍 >2000 字符，但与上一轮逐字相同的大段应当被压掉，
        压缩后 <2000 字符 → 策略应变为 direct（不再传附件）

改之前轮 B 会照样再传一份附件（基准丢了就整份重发）；
现在基准按"网页对话"存并持久化在 pack_state.json，重启也不丢。
"""
import io
import json
import time

import requests

import chrome_launcher as c

URL = "http://127.0.0.1:8199/v1/chat/completions"
MARK = "PKG-LIVE-6310"

_real = io.open("browser_core.py", encoding="utf-8").read()
BODY = _real[:3000]

TOOLS = [
    {"type": "function", "function": {
        "name": "exec", "description": "Run a shell command on the paired node",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "nodes_status", "description": "List paired nodes and status",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "screen_record", "description": "Capture the desktop screen",
        "parameters": {"type": "object", "properties": {
            "seconds": {"type": "integer"}}}}},
]


def ask(messages, tag):
    pos = io.open("log/app/server.log", encoding="utf-8",
                  errors="ignore").seek(0, 2)
    t0 = time.time()
    r = requests.post(URL, json={"model": "chatgpt.com", "stream": False,
                                "tools": TOOLS, "messages": messages},
                      timeout=900)
    dt = time.time() - t0
    txt = ""
    try:
        d = r.json()
        txt = ((d.get("choices") or [{}])[0].get("message", {})
               .get("content")) or json.dumps(d, ensure_ascii=False)[:200]
    except Exception:
        txt = r.text[:200]
    print(f"[{tag}] HTTP {r.status_code} {dt:.1f}s 回复 {len(str(txt))} 字: "
          f"{str(txt)[:150]!r}")
    with io.open("log/app/server.log", encoding="utf-8", errors="ignore") as f:
        f.seek(pos)
        for line in f.read().splitlines():
            if any(k in line for k in ("Packager", "LongPrompt", "Upload]",
                                       "Chunk] 消息", "CtxLimit")):
                print("   ", line[:200])
    return str(txt)


# 轮 A：单条大消息
ask([{"role": "user", "content":
      "只读这段代码，回答它属于哪个文件；不要调用任何工具。"
      "回答最后一行原样包含标记 " + MARK + "\n\n" + BODY}],
    "A 首轮（应走附件）")

# 轮 B：IDE 式重放——历史原样带上，再加一句新话
ask([{"role": "user", "content":
      "只读这段代码，回答它属于哪个文件；不要调用任何工具。"
      "回答最后一行原样包含标记 " + MARK + "\n\n" + BODY},
     {"role": "assistant", "content": "browser_core.py，" + MARK},
     {"role": "user", "content": "很好。现在只回复两个字：OK"}],
    "B 续传轮（压缩后应转直发）")

print("\npack_state.json 是否已生成:",
      io.open("pack_state.json", encoding="utf-8", errors="ignore")
      .read()[:120] + "…" if True else "")
