"""接管验证 v2：先确认自启 Chrome 真的绑上了调试端口（绝不让
DrissionPage 静默拉起空白浏览器），再驱动 WorkflowExecutor 实测
长提示词上传与填充读回。只填充不发送，测完刷新丢弃并关闭自启浏览器。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from DrissionPage import ChromiumPage
from browser_core import WorkflowExecutor
from config_engine import normalize_selector_group

PORT = int(os.environ.get("QODER_PORT", "9299"))
ACC = os.environ.get("QODER_ACC", "acc1")
ROOT = Path(__file__).resolve().parent
UDD = str(ROOT / "chrome_profiles" / ACC)
CHROME = next((c for c in (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
    if os.path.exists(c)), None)
B = f"http://127.0.0.1:{PORT}"


def cdp(path=""):
    return json.load(urllib.request.urlopen(B + path, timeout=4))


print(f"profile={UDD}")
proc = subprocess.Popen(
    [CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={UDD}",
     "--no-first-run", "--no-default-browser-check"],
    creationflags=(subprocess.DETACHED_PROCESS
                   | subprocess.CREATE_NEW_PROCESS_GROUP))
alive = False
for _ in range(15):
    time.sleep(2)
    try:
        print("CDP 就绪:", cdp("/json/version").get("Browser"))
        alive = True
        break
    except Exception:
        continue
if not alive:
    print(f"自启 Chrome 未能监听 {PORT}（Popen pid={proc.pid} "
          f"poll={proc.poll()}）——该 profile 可能已被其他 Chrome 进程占用")
    sys.exit(1)

sites = json.loads((ROOT / "sites.json").read_text(encoding="utf-8"))
sel = sites["chatgpt.com"]["selectors"]
INPUT = normalize_selector_group(sel.get("input_box"))

txt = ROOT / "uploads" / "qoder_selftest_long.txt"
txt.write_text("QODER 上传链路自检文件。\n" + ("自检行 " * 4000) +
               "\n请只回复 OK\n", encoding="utf-8")
report = {"测试文件字节": txt.stat().st_size}
try:
    page = ChromiumPage(addr_or_opts=f"127.0.0.1:{PORT}")
    tab = page.new_tab("https://chatgpt.com/")
    for _ in range(20):
        time.sleep(3)
        if "chatgpt.com" in (tab.url or ""):
            break
    time.sleep(12)
    print("落地:", tab.url, "|", (tab.title or "")[:40])

    ex = WorkflowExecutor(tab, stealth_mode=False)
    report["页面状态"] = ex._page_state()
    report["旧兜底取到的口"] = tab.run_js(
        "var e=document.querySelector('input[type=\"file\"]');"
        "return e?(e.accept||'(accept 空)'):'无 file input';")
    report["全部file口accept"] = tab.run_js(
        "return Array.from(document.querySelectorAll("
        "'input[type=\"file\"]')).map(function(e,i){"
        "return i+':'+(e.accept||'空')+(e.multiple?'+multi':'');});")

    t0 = time.time()
    try:
        ex._execute_upload_file(file_input_selector="",
                                upload_btn_selector="",
                                file_path=str(txt),
                                optional=False, wait_after=3.0)
        report["上传"] = f"成功，附件已验证可见（{time.time() - t0:.1f}s）"
    except Exception as e:
        report["上传"] = f"失败 {type(e).__name__}: {e}"

    probe = "QODER 自检：只验证填充读回，不发送。"
    try:
        ex._execute_fill(INPUT, probe, "input_box", False)
        report["填充读回"] = (tab.run_js(
            "var e=document.querySelector("
            "'div.ProseMirror[contenteditable=\"true\"]');"
            "return e?(e.innerText||''):'';") or "")[:40]
    except Exception as e:
        report["填充读回"] = f"失败 {type(e).__name__}: {e}"

    # 真实错误页场景：守卫必须叫出真名，而不是"编辑器拒绝注入/额度耗尽"
    try:
        tab.get("https://qoder-nonexistent-host-test.invalid/")
        time.sleep(6)
        report["错误页状态"] = ex._page_state()
        try:
            ex._guard_page_usable("填充")
            report["错误页守卫"] = "未拦截（FAIL）"
        except Exception as ge:
            report["错误页守卫"] = f"已拦截: {type(ge).__name__} {ge}"
    except Exception as e:
        report["错误页状态"] = f"导航失败: {e}"

    try:
        tab.get("https://chatgpt.com/")
        time.sleep(8)
    except Exception:
        pass
    try:
        tab.refresh()
        time.sleep(4)
        print("已刷新丢弃草稿/附件（未发送任何消息）")
    except Exception as e:
        print("刷新失败:", e)
finally:
    # 只按"本进程用到的完整 profile 路径"精确清理，绝不匹配生产实例
    # （生产实例路径同样以 chrome_profiles\acc1 结尾，宽匹配会误杀）
    subprocess.run(["powershell", "-NoProfile", "-c",
                    "Stop-Process -Id " + str(proc.pid) +
                    " -Force -ErrorAction SilentlyContinue;"
                    " Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\""
                    " | Where-Object { $_.CommandLine -notmatch '--type=' }"
                    " | Where-Object { $_.CommandLine -like '*" + UDD + "*' }"
                    " | ForEach-Object { Stop-Process -Id $_.ProcessId"
                    " -Force -ErrorAction SilentlyContinue }"],
                   capture_output=True)
    txt.unlink(missing_ok=True)
    print("\n=== 汇总 ===")
    print(json.dumps(report, ensure_ascii=False, indent=1))
