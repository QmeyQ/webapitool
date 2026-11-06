"""验证「请求触发的自动启动」是否起在可见会话里。

流程：杀掉一个次要账号的窗口 → 发一个请求（该账号登录态与主账号无关，
请求注定失败，这里只要它触发 ensure_connection / switch_instance 的自动启动）
→ 查该端口的进程 SessionId，必须是 1（用户桌面）而不是 0（服务桌面）。

端口从命令行给，默认 9226（acc12，最少被用到的那个），
不要拿用户可能正在登录的窗口做实验。
"""
import sys
import time

import requests

import chrome_launcher as c

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9226
w = c.window_status([PORT])[PORT]
print("before:", w)
if w["pid"]:
    print("kill:", c.kill_pid(w["pid"]))
time.sleep(2)

try:
    r = requests.post("http://127.0.0.1:8199/v1/chat/completions",
                      json={"model": "chatgpt.com", "stream": False,
                            "messages": [{"role": "user",
                                          "content": "只回复 OK"}]},
                      timeout=150)
    print("请求返回:", r.status_code, r.text[:200])
except Exception as e:
    print("请求异常:", type(e).__name__, str(e)[:160])

for _ in range(10):
    time.sleep(3)
    now = c.window_status([PORT])[PORT]
    if now["pid"]:
        break
print("after :", now)
print("可见 =", now["visible"], "（期望 True：窗口应弹在你自己的桌面上）")
