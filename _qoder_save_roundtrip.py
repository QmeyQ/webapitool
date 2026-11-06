"""端到端复现"面板保存"这条路，验证不再丢站点配置。

面板做的事就是：GET /api/config → normalizeConfig(data) → POST /api/config。
用 dashboard.js 里那段真实的 normalizeConfig（抽出来在 node 里跑），
把真实 sites.json 原样走一遍，再逐键对比服务器上的结果。
跑完用 git 还原 sites.json。
"""
import io
import json
import re
import subprocess

import requests

API = "http://127.0.0.1:8199"

src = io.open("dashboard.js", encoding="utf-8").read()
fn = re.search(r"normalizeConfig\(raw\) \{.*?\n        \},", src, re.S)
assert fn, "抓不到 normalizeConfig"
body = fn.group(0).rstrip().rstrip(",")
js = ("const normalizeConfig = function " + body[len("normalizeConfig"):]
      + "\nconst raw = JSON.parse(require('fs').readFileSync('sites.json',"
        + "'utf8'));\n"
        + "console.log(JSON.stringify(normalizeConfig(raw)));\n")
io.open("_norm_round.js", "w", encoding="utf-8").write(js)
res = subprocess.run(["node", "_norm_round.js"], capture_output=True,
                     timeout=60)
normalized = json.loads(res.stdout.decode("utf-8", "replace"))

before = json.load(io.open("sites.json", encoding="utf-8"))
r = requests.post(API + "/api/config", json={"config": normalized}, timeout=30)
print("保存:", r.status_code, r.json().get("message"))

after = requests.get(API + "/api/config", timeout=20).json()
lost = []
for site, cfg in before.items():
    for k in cfg:
        if k not in (after.get(site) or {}):
            lost.append(f"{site}.{k}")
print("保存前后丢失的键:", lost or "无")
print("每站键数 前/后:",
      {s: (len(before[s]), len(after.get(s, {}))) for s in list(before)[:3]})
print("chatgpt.com 保存后的键:", sorted((after.get("chatgpt.com") or {}).keys()))
subprocess.run(["git", "checkout", "--", "sites.json"], check=True)
restored = json.load(io.open("sites.json", encoding="utf-8"))
print("已还原 sites.json，键数:", len(restored),
      "内容与 HEAD 一致:", subprocess.run(
          ["git", "diff", "--quiet", "--", "sites.json"]).returncode == 0)
