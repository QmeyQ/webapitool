"""在窗口里那段龙虾对话中，精确抓取"权限/拒绝/denied"类原文。只读。"""
import json, sys
from DrissionPage import ChromiumPage

JS = r"""
function n(s){return (s||'').replace(/\s+/g,' ').trim();}
var bt = document.body ? document.body.innerText : '';
var keys = ['权限','permission','denied','not allowed','拒绝','无权','无法','失败',
            'approve','批准','审批','sandbox','沙箱','admin','管理员','受限','risk'];
var seen = {}, hits = [];
for (var i=0;i<keys.length;i++){
  var k=keys[i], from=0;
  while (hits.length < 20) {
    var p = bt.toLowerCase().indexOf(k.toLowerCase(), from);
    if (p<0) break;
    var snip = n(bt.slice(Math.max(0,p-120), p+200));
    if (!seen[snip]) { seen[snip]=1; hits.push({k:k, t:snip}); }
    from = p + k.length;
  }
}
return {len: bt.length, hits: hits, tail: n(bt.slice(-900))};
"""
page = ChromiumPage(addr_or_opts="127.0.0.1:" + (sys.argv[1] if len(sys.argv)>1 else "9222"))
d = page.get_tabs()[0].run_js(JS)
print("对话正文长度:", d["len"])
for h in d["hits"][:14]:
    print("--["+h["k"]+"]--", h["t"][:320])
print("==== 页面末尾 ====")
print(d["tail"][:900])
