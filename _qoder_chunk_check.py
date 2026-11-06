"""真实验证：分片直输完成后，返回的是最后一条回复（不是上一片的回执）。

用户报的现象："只返回了倒数第二次的回复"。根因是分片之间 AI 会先回一句
"已收到第 1 片，请继续"，而 [Fast AI Pre] 用的"发送前计数"是第 1 片之前采的，
最后一片发完时当前计数早就超过它，于是被判成"AI 已经回了"，把上一片的回执
当成这一轮的答案返回。修法是最后一片发完后再重采一次发送前计数。

前置：sites.json 里 chatgpt 的 upload_config.enabled 已被临时置 false
（用 _qoder_toggle_longprompt.py 切），否则 >12000 字符会走附件而不是分片。
"""
import io
import json
import time

import requests

URL = "http://127.0.0.1:8199/v1/chat/completions"
MARK = "CHUNKFINAL-3172"

_real = io.open("browser_core.py", encoding="utf-8").read()
prompt = (
    "这是一份较长源码，被中转按片发送。在你收到写着"
    "\"以上消息已全部发送完毕\"的那一片之前，不要回答内容。\n"
    "收到最后一片后，只回复一个词：" + MARK + "，不要解释、不要调用工具。\n\n"
    + _real[:30000])

print("prompt 字符数:", len(prompt))
t0 = time.time()
r = requests.post(URL, json={"model": "chatgpt.com", "stream": False,
                             "messages": [{"role": "user",
                                           "content": prompt}]},
                  timeout=900)
dt = time.time() - t0
try:
    d = r.json()
    txt = ((d.get("choices") or [{}])[0].get("message", {})
           .get("content"))
    if txt is None:
        txt = json.dumps(d, ensure_ascii=False)[:500]
except Exception:
    txt = r.text[:500]
print(f"HTTP {r.status_code} 用时 {dt:.1f}s")
print("回复:", repr(str(txt))[:400])
print("是否为本轮最终回复（含标记）:", MARK in str(txt))
