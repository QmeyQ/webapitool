"""现场验证：用真实页面跑 probe_page_state + site_policy.classify。

不带真实数据等于没测——这里直接连上正在跑的 Chrome 调试端口，把线上页面
喂给判定函数，看它到底给出什么状态。只读探测，不点任何东西。
"""
import json
import sys

from DrissionPage import ChromiumPage

import browser_core as bc
import site_strategy as sp

port = sys.argv[1] if len(sys.argv) > 1 else "9222"
page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
rows = []
for t in page.get_tabs():
    try:
        raw = t.run_js(bc._PAGE_STATE_JS)
        dom = sp.host_of(str(raw.get("url") or ""))
        st = bc.probe_page_state(t, dom)
        rows.append({
            "domain": dom,
            "classify": sp.classify(dom, raw),
            "probe_state": st.get("state"),
            "detail": str(st.get("detail") or "")[:120],
            "context_full": bool(st.get("context_full")),
            "limit_text": str(st.get("limit_text") or "")[:100],
            "banner_head": str(raw.get("banner") or "")[:160],
            "btn_texts": raw.get("btn_texts"),
            "editors": raw.get("rich_editors"),
            "login_ctas": raw.get("login_ctas"),
        })
    except Exception as e:
        rows.append({"error": f"{type(e).__name__}: {e}"[:160]})
print(json.dumps(rows, ensure_ascii=False, indent=1)[:4000])
