"""真实验证：面板保存的站点级"压缩下限/压缩开关"是否被运行中的链路用上。

流程走的是面板同一套接口：POST /api/config 写 sites.json → 发真实请求 →
看日志里生效值。跑完还原 sites.json。
"""
import io
import json
import subprocess
import time

import requests

API = "http://127.0.0.1:8199"
MARK = "CFG-7723"
_real = io.open("browser_core.py", encoding="utf-8").read()
PROMPT = ("只回答这段代码属于哪个文件，不要调用工具，最后一行原样输出标记 "
          + MARK + "\n\n" + _real[:4000])


def post_config(mutate):
    sites = requests.get(API + "/api/config", timeout=20).json()
    mutate(sites)
    r = requests.post(API + "/api/config", json={"config": sites}, timeout=30)
    print("  保存:", r.status_code, r.json().get("message", ""))
    disk = json.load(io.open("sites.json", encoding="utf-8"))
    return disk["chatgpt.com"]


def ask(tag):
    pos = io.open("log/app/server.log", encoding="utf-8",
                  errors="ignore").seek(0, 2)
    t0 = time.time()
    r = requests.post(API + "/v1/chat/completions",
                      json={"model": "chatgpt.com", "stream": False,
                            "messages": [{"role": "user",
                                          "content": PROMPT}]},
                      timeout=600)
    dt = time.time() - t0
    txt = ""
    try:
        d = r.json()
        txt = ((d.get("choices") or [{}])[0].get("message", {})
               .get("content")) or json.dumps(d, ensure_ascii=False)[:160]
    except Exception:
        txt = r.text[:160]
    print(f"[{tag}] HTTP {r.status_code} {dt:.1f}s 回复 {len(str(txt))} 字: "
          f"{str(txt)[:90]!r}")
    with io.open("log/app/server.log", encoding="utf-8", errors="ignore") as f:
        f.seek(pos)
        for line in f.read().splitlines():
            if "Packager]" in line and ("压缩" in line or "策略=" in line):
                print("   ", line[:200])
    return str(txt)


def set_floor(sites):
    sites["chatgpt.com"]["CONDENSE_MIN_CHARS"] = 2000
    sites["chatgpt.com"]["CONDENSE_ENABLED"] = True


def turn_off(sites):
    sites["chatgpt.com"]["CONDENSE_MIN_CHARS"] = 2000
    sites["chatgpt.com"]["CONDENSE_ENABLED"] = False


print("① 面板保存：压缩下限=2000")
cfg = post_config(set_floor)
print("   sites.json 里的值:", cfg.get("CONDENSE_MIN_CHARS"),
      cfg.get("CONDENSE_ENABLED"))
time.sleep(1.0)
ask("下限 2000（应能压）")

print("\n② 面板保存：压缩总开关关掉")
cfg = post_config(turn_off)
print("   sites.json 里的值:", cfg.get("CONDENSE_ENABLED"))
time.sleep(1.0)
ask("压缩已关闭（应整份发送）")

print("\n③ 还原 sites.json")
subprocess.run(["git", "checkout", "--", "sites.json"], check=True)
back = json.load(io.open("sites.json", encoding="utf-8"))["chatgpt.com"]
print("   还原后残留的新键:",
      [k for k in ("CONDENSE_MIN_CHARS", "CONDENSE_ENABLED") if k in back]
      or "无")
