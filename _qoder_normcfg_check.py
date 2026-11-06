"""把 dashboard.js 里真实的 normalizeConfig 抽出来跑一遍，验证它不再丢键。

这是真实路径：面板载入 /api/config 后第一件事就是 normalizeConfig，
然后"保存配置"会把结果整个 POST 回去。以前它只保留
selectors/workflow/stealth，于是站点级的 CHUNK_CHARS / CONDENSE_* /
MAX_CHUNKED_CHUNKS / upload_config 会被静默抹掉——面板上看着设好了，
下一轮又变回默认。
"""
import io
import json
import re
import subprocess

src = io.open("dashboard.js", encoding="utf-8").read()
m = re.search(r"normalizeConfig\(raw\) \{.*?\n        \},", src, re.S)
if not m:
    raise SystemExit("没抓到 normalizeConfig，检查 dashboard.js 结构")
fn = m.group(0).rstrip(",").rstrip()
fn = "const normalizeConfig = function " + fn[len("normalizeConfig"):]

payload = {
    "chatgpt.com": {
        "selectors": {"input_box": "#in"},
        "workflow": [{"action": "FILL_INPUT"}],
        "stealth": False,
        "CHUNK_CHARS": 9000,
        "MAX_CHUNKED_CHUNKS": 12,
        "CONDENSE_MIN_CHARS": 30000,
        "CONDENSE_ENABLED": False,
        "upload_config": {"enabled": False, "max_prompt_length": 4000,
                          "max_file_size": 1048576},
        "entry_url": "https://chatgpt.com/",
    }
}
io.open("_norm_check.js", "w", encoding="utf-8").write(
    fn + "\nconst raw = " + json.dumps(payload, ensure_ascii=False)
    + ";\nconsole.log(JSON.stringify(normalizeConfig(raw)));\n")
r = subprocess.run(["node", "_norm_check.js"], capture_output=True,
                   text=True, timeout=60)
if r.returncode:
    raise SystemExit("node 跑失败: " + r.stderr[:400])
out = json.loads(r.stdout)
site = out["chatgpt.com"]
kept = [k for k in ("CHUNK_CHARS", "MAX_CHUNKED_CHUNKS", "CONDENSE_MIN_CHARS",
                    "CONDENSE_ENABLED", "upload_config", "entry_url")
        if k in site]
missing = [k for k in ("CHUNK_CHARS", "MAX_CHUNKED_CHUNKS",
                       "CONDENSE_MIN_CHARS", "CONDENSE_ENABLED",
                       "upload_config", "entry_url") if k not in site]
print("保留下来的键:", kept)
print("丢掉的键:", missing or "无")
assert site.get("selectors") == {"input_box": "#in"}
assert site.get("workflow") == [{"action": "FILL_INPUT"}]
assert not missing, "normalizeConfig 仍在丢站点级配置"
print("值也一致:", site["CHUNK_CHARS"] == 9000,
      site["CONDENSE_ENABLED"] is False,
      site["upload_config"]["max_prompt_length"] == 4000)
