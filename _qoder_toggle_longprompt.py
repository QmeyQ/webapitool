"""临时关掉 chatgpt 的长提示词附件路由，让 >12000 字符的 prompt 真的走
"分片直输"——这是唯一能真实复现"只返回倒数第二次回复"的场景。

跑完必须还原 sites.json（原文备份在 sites.json.bak-visible-check）。
"""
import io
import json
import os
import shutil
import sys

P = "sites.json"
BAK = "sites.json.bak-visible-check"

if len(sys.argv) > 1 and sys.argv[1] == "restore":
    if os.path.exists(BAK):
        shutil.move(BAK, P)
        cfg = json.load(open(P, encoding="utf-8"))["chatgpt.com"]
        print("已还原 sites.json，chatgpt upload_config =",
              json.dumps(cfg.get("upload_config"), ensure_ascii=False))
    else:
        print("没有备份可还原（可能已经还原过）")
    sys.exit(0)

shutil.copy2(P, BAK)
d = json.load(open(P, encoding="utf-8"))
d["chatgpt.com"]["upload_config"] = {"enabled": False}
io.open(P, "w", encoding="utf-8").write(
    json.dumps(d, ensure_ascii=False, indent=2))
print("已临时禁用 chatgpt 附件路由，备份在", BAK)

from data_models import merge_upload_config
from browser_core import LongPromptRouter
cfg = merge_upload_config(d["chatgpt.com"]["upload_config"])
print("运行期 enabled =", cfg.get("enabled"))
for n in (30000, 60000):
    print(f"{n} 字符策略 =", LongPromptRouter.decide("X" * n, cfg)[0])
