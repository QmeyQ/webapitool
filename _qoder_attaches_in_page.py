"""现场核对：网页对话里**实际存在**哪些附件，与本地 uploads 差多少。

之前的错误做法：从 IDE 重放的消息文本里认附件名，就当作"这个附件已经在
网页上下文里"。这两件事根本没有关系——uploads 里那些文件大多是回退过程
留下的，站点那边从没收到过。结果压缩出来的占位符指向一个不存在的附件，
AI 找不到附件就答不出来。
"""
import io
import json
import os
import re

from DrissionPage import ChromiumPage

NAME_RE = r"prompt_\d+_[0-9a-f]{6}(?:_part\d{2}of\d{2})?\.txt"

JS = r"""
function n(s){return (s||'').replace(/\s+/g,' ').trim();}
// 1) 消息区里真实渲染出来的附件（用户轮里的文件卡片/文件名文本）
var scope = document.querySelector('[data-turn-key],[class*="thread-scroll" i]')
          || document.body;
var txt = n(scope.innerText);
// 2) 更精确：找带文件名样式的节点
var nodes = document.querySelectorAll('a,div,span,button');
var chips = [];
var re = /prompt_\d+_[0-9a-f]{6}(?:_part\d{2}of\d{2})?\.txt/g;
for (var i = 0; i < nodes.length && chips.length < 30; i++) {
  var e = nodes[i];
  if (e.childElementCount > 0) continue;          // 只认叶子节点，避免重复
  var t = n(e.innerText || e.getAttribute('aria-label') || e.getAttribute('title'));
  var m = t.match(re);
  if (m) { for (var k = 0; k < m.length; k++) { if (chips.indexOf(m[k]) < 0) chips.push(m[k]); } }
}
// 3) composer 里当前挂着的附件（未发送）
var composer = document.querySelector('form') || document.body;
var pend = [];
var pm = (composer.innerText || '').match(re);
if (pm) { for (var q = 0; q < pm.length && pend.length < 10; q++) { if (pend.indexOf(pm[q]) < 0) pend.push(pm[q]); } }
return {
  url: location.href.slice(0, 80),
  turn_count: document.querySelectorAll('[data-turn-key]').length,
  in_message_area: (txt.match(re) || []).slice(0, 20),
  leaf_chips: chips,
  composer_pending: pend,
  area_len: txt.length
};
"""

port = int(os.environ.get("QPORT", "9222"))
page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
tab = page.get_tabs()[0]
d = tab.run_js(JS)
local = sorted(f for f in os.listdir("uploads") if re.fullmatch(NAME_RE, f)) \
    if os.path.isdir("uploads") else []
print(json.dumps({k: d[k] for k in ("url", "turn_count", "area_len")},
                 ensure_ascii=False))
seen = sorted(set((d["in_message_area"] or []) + (d["leaf_chips"] or [])))
print("网页对话里出现的附件名:", len(seen))
for s in seen[:20]:
    print("   ", s, "本地是否存在:", s in local)
print("composer 待发送附件:", d["composer_pending"])
print("本地 uploads 文件数:", len(local))
print("本地有、上下文里没有的（这些绝不能再当基准）:")
for f in local[-12:]:
    print("   ", f, "在上下文:", f in seen)
