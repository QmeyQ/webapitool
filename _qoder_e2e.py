import json, time, requests
URL = "http://127.0.0.1:8199/v1/chat/completions"

def ask(content, tag):
    t0 = time.time()
    r = requests.post(URL, json={"model": "chatgpt.com", "stream": False,
                                 "messages": [{"role": "user", "content": content}]},
                      timeout=420)
    dt = time.time() - t0
    try:
        d = r.json()
        txt = (d.get("choices") or [{}])[0].get("message", {}).get("content")
        if txt is None:
            txt = json.dumps(d, ensure_ascii=False)[:300]
    except Exception:
        txt = r.text[:300]
    print(f"[{tag}] HTTP {r.status_code} 用时 {dt:.1f}s 回复: {str(txt)[:200]!r}")
    return str(txt)

short = ask("这是链路自检，不要执行任何工具，请只回复两个字：OK", "短消息")
long_text = ("以下是一行长提示词自检文件内容，收到后请只回复两个字：OK\n"
             + ("自检行 请忽略内容 " * 4200) + "\nEND")
print("长提示词字符数:", len(long_text))
lont = ask(long_text, "长提示词-上传")
