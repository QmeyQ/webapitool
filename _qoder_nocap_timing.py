"""真实计时验证：账号没有上传能力时，长 prompt 不再空烧一次上传+刷新。

对照：改动前同一场景实测 47.6s（含 12s 上传等待 + 刷新 + 回退），
期望现在 <35s 且日志里没有 "[Upload] 单文件已准备" / "上传不可用"。
"""
import io
import json
import time

import requests

import chrome_launcher as c

URL = "http://127.0.0.1:8199/v1/chat/completions"
MARK = "NOCAP-4411"

_real = io.open("browser_core.py", encoding="utf-8").read()
body = _real[:4000]          # 4k 字符：超过 max_prompt_length，本来会走附件
prompt = ("只回答这段代码属于哪个文件，不要调用工具，"
          f"最后一行原样输出标记 {MARK}\n\n" + body)
print("prompt 字符数:", len(prompt))


def one(tag):
    pos = io.open("log/app/server.log", encoding="utf-8",
                  errors="ignore").seek(0, 2)
    t0 = time.time()
    r = requests.post(URL, json={"model": "chatgpt.com", "stream": False,
                                 "messages": [{"role": "user",
                                               "content": prompt}]},
                      timeout=600)
    dt = time.time() - t0
    try:
        d = r.json()
        txt = ((d.get("choices") or [{}])[0].get("message", {})
               .get("content")) or json.dumps(d, ensure_ascii=False)[:200]
    except Exception:
        txt = r.text[:200]
    print(f"[{tag}] HTTP {r.status_code} 用时 {dt:.1f}s 回复: "
          f"{str(txt)[:120]!r}")
    with io.open("log/app/server.log", encoding="utf-8", errors="ignore") as f:
        f.seek(pos)
        new = f.read()
    keys = ("UploadCap", "Packager", "Upload]", "上传不可用", "刷新页面")
    for line in new.splitlines():
        if any(k in line for k in keys):
            print("   ", line[:190])
    wasted = ("[Upload] 单文件已准备" in new) or ("上传不可用" in new)
    print(f"   附件尝试={'有（说明还在空烧）' if wasted else '无（正确）'}")
    return dt, wasted


# 第一轮：现场探测出没有上传能力
dt1, w1 = one("第 1 轮（现场探测）")
# 第二轮：应命中能力登记缓存，连探测都省了
dt2, w2 = one("第 2 轮（用登记的缺失能力）")
print(f"\n用时：{dt1:.1f}s / {dt2:.1f}s（改动前实测 47.6s 一轮）")
print("两轮都没有再尝试附件上传:", not w1 and not w2)
