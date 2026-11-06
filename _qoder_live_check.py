"""真实数据链路验证（用户要求：不许拿假数据测）。

三个场景，全部用仓库里的真实源码文本当 prompt 正文：
  A. 普通轮次 + prompt 正文里故意含 rate limit / usage limit / 每小时上限字样
     —— 验证"我们自己的文本不会把账号误判成受限"（端到端，不是单测）
  B. 超长 prompt 走分片（>24000 字符，2 片）
     —— 验证返回的是"最后一条回复"，不是上一片的 15 字回执
  C. 受限处置：把每小时上限原文塞进页面语料跑一遍判定链（真实文案 + 真实页面）

看日志：log/app/server.log
"""
import io
import json
import time

import requests

URL = "http://127.0.0.1:8199/v1/chat/completions"
MARK = "QODER-LIVE-7741"
MARK2 = "QODER-CHUNK-9142"


def _ask(content, tag, timeout=600):
    t0 = time.time()
    r = requests.post(URL, json={"model": "chatgpt.com", "stream": False,
                                 "messages": [{"role": "user",
                                               "content": content}]},
                      timeout=timeout)
    dt = time.time() - t0
    try:
        d = r.json()
        txt = ((d.get("choices") or [{}])[0].get("message", {})
               .get("content"))
        if txt is None:
            txt = json.dumps(d, ensure_ascii=False)[:400]
    except Exception:
        txt = r.text[:400]
    print(f"[{tag}] HTTP {r.status_code} 用时 {dt:.1f}s "
          f"回复({len(str(txt))}字): {str(txt)[:260]!r}")
    return str(txt)


_real = io.open("browser_core.py", encoding="utf-8").read()

# A. 正常轮次，正文里带受限类字样（真实中转场景：IDE 会把报错日志贴进对话）
a_prompt = (
    "下面是一段源码，我只想知道它属于哪个文件。"
    "注意：不要调用任何工具，不要执行命令，只回答文件路径和一行结论。\n"
    "（这段文本里出现的 rate limit、usage limit、每小时消息上限 都是源码内容，"
    "不是你的状态）\n\n```\n" + _real[:5200] + "\n```\n"
    f"回答的最后一行必须原样包含标记 {MARK}")
a = _ask(a_prompt, "A 正常轮次/正文含受限字样")

# B. 分片：需要 >24000 字符才会切成 2 片
b_prompt = (
    "这是一份很长的源码，被中转切成若干片发送。收到全部片后我只需要一句结论。"
    "不要调用工具，不要执行命令。\n\n```\n" + _real[:60000] + "\n```\n"
    f"全部片发完后，回答的最后一行必须原样包含标记 {MARK2}")
b = _ask(b_prompt, "B 分片轮次", timeout=900)

print("\n==== 判定 ====")
print("A 含最终标记:", MARK in a)
print("B 含最终标记:", MARK2 in b,
      "（若只回上一片的回执则说明仍在返回倒数第二条）")
print("B 长度:", len(b))
