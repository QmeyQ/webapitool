"""把服务默认实例接到登录着的 acc3：先彻底清理工作区 profile 的 Chrome，
等锁释放后再以当前用户身份拉起 9222，最后确认页面就绪。"""
import json
import os
import subprocess
import time
import urllib.request

ROOT = os.getcwd()
UDD = os.path.join(ROOT, "chrome_profiles", "acc3")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def kill_workspace_browsers():
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", os.path.join(ROOT, "_qoder_restart.ps1"),
                    "-KillOnly"], capture_output=True)


kill_workspace_browsers()
print("已清理工作区 Chrome，等待 profile 锁释放…")
time.sleep(8)

proc = subprocess.Popen(
    [CHROME, "--remote-debugging-port=9222", f"--user-data-dir={UDD}",
     "--proxy-server=http://127.0.0.1:7897", "--no-first-run",
     "--no-default-browser-check", "https://chatgpt.com/"],
    creationflags=(subprocess.DETACHED_PROCESS
                   | subprocess.CREATE_NEW_PROCESS_GROUP))
print("acc3 Chrome pid=", proc.pid)

for i in range(20):
    time.sleep(3)
    if proc.poll() is not None:
        print("Chrome 进程已退出，code=", proc.returncode)
        break
    try:
        tabs = json.load(urllib.request.urlopen(
            "http://127.0.0.1:9222/json/list", timeout=4))
        pages = [t for t in tabs if t.get("type") == "page"]
        for t in pages:
            u = t.get("url") or ""
            if "chatgpt.com" in u and not u.startswith("chrome-error"):
                print(f"就绪: {u[:44]} | {t.get('title','')[:44]} | "
                      f"tabs={len(pages)}")
                raise SystemExit(0)
        if pages:
            print(f"  [{i}] 等待中: {pages[0].get('url','')[:50]}")
    except SystemExit:
        raise
    except Exception as e:
        print(f"  [{i}] CDP 未就绪 {type(e).__name__}")
print("未确认就绪")
