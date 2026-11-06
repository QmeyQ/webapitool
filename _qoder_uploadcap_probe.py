"""现场核对上传能力探测器在真实页面上的判断。"""
import json
import sys

from DrissionPage import ChromiumPage

import browser_core as bc

port = sys.argv[1] if len(sys.argv) > 1 else "9222"
page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
tab = page.get_tabs()[0]
facts = tab.run_js(bc.WorkflowExecutor._FILE_INPUT_FACTS_JS)
print("上传口事实:")
for f in facts or []:
    print("  ", json.dumps(f, ensure_ascii=False))
print("探测结论:", json.dumps(bc.probe_upload_capability(tab),
                             ensure_ascii=False))
