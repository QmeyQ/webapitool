"""校验 banner 语料里到底还有没有我们自己的对话原文。

真实页面（:9222）实测曾把 "你说：…／ChatGPT 说：…" 整段搬进 banner，
而我们的 prompt 里常带 rate limit / usage limit 字样——那种语料会把
正常账号误判成"受限"并打进 1 小时冷却。这里用当前页面重跑一次，
并把带受限字样的假语料塞进去验证判定不会被带偏。
"""
import json

from DrissionPage import ChromiumPage

import browser_core as bc
import site_strategy as sp

port = 9222
page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
t = page.get_tabs()[0]
raw = t.run_js(bc._PAGE_STATE_JS)
banner = str(raw.get("banner") or "")
print("banner 长度:", len(banner))
print("banner 前 500:", repr(banner[:500]))
print("banner 里是否还有对话回显:",
      ("你说：" in banner) or ("ChatGPT 说：" in banner))
print("limit_hits:", json.dumps(raw.get("limit_hits"), ensure_ascii=False)[:300])
print("classify(真实页面):", sp.classify("chatgpt.com", raw))

# 造一次"我们自己的 prompt 里有受限字样"的真实场景：正文里带，提示区不带
fake = dict(raw)
fake["tail"] = ("HTTP 429 rate limit exceeded, please try again later. "
                "usage limit 每小时消息上限 你也该升级了")
fake["head"] = fake["tail"]
print("把受限字样放进正文后 classify:", sp.classify("chatgpt.com", fake))
