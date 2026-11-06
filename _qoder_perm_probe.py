"""读取当前窗口上龙虾（OpenClaw/CodeArt）的对话，抓权限相关原文。只读。"""
import json, sys
from DrissionPage import ChromiumPage

JS = r"""
function n(s){return (s||'').replace(/\s+/g,' ').trim();}
var bt = document.body ? document.body.innerText : '';
var keys = ['权限','permission','无权','拒绝','not allowed','denied','无法执行','没有权限',
            'approve','批准','审批','tool','工具','沙箱','sandbox','error','失败'];
var hits = [];
for (var i=0;i<keys.length;i++){
  var k=keys[i], from=0;
  while (hits.length < 14) {
    var p = bt.toLowerCase().indexOf(k.toLowerCase(), from);
    if (p<0) break;
    hits.push({k:k, t: bt.slice(Math.max(0,p-90), p+160)});
    from = p + k.length;
  }
}
return {url: location.href.slice(0,90), len: bt.length,
        head: bt.slice(0,700), tail: bt.slice(-1400), hits: hits};
"""
page = ChromiumPage(addr_or_opts="127.0.0.1:" + (sys.argv[1] if len(sys.argv)>1 else "9222"))
t = page.get_tabs()[0]
d = t.run_js(JS)
print("url:", d["url"], "body 字符数:", d["len"])
print("--- tail ---")
print(d["tail"][:1400])
print("--- 权限/工具命中 ---")
for h in d["hits"][:8]:
    print("["+h["k"]+"]", h["t"][:200])
