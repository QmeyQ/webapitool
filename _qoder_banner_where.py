"""定位 banner 语料里"你说：…／ChatGPT 说：…"这段对话回显到底来自哪个节点。

banner 只能放站点自己的提示；这段是我们自己发出的原文，留在语料里就会
因为 prompt 含 rate limit 之类字样而把好账号误判成"受限"。
"""
import json

from DrissionPage import ChromiumPage

JS = r"""
function n(s){return (s||'').replace(/\s+/g,' ').trim();}
function chain(e){
  var seg=[], cur=e;
  for (var d=0; d<8 && cur && cur.tagName; d++) {
    var cls = n(typeof cur.className==='string'?cur.className:'').split(' ').slice(0,4).join('.');
    seg.push(cur.tagName.toLowerCase()
      + (cur.id ? '#'+cur.id : '')
      + (cur.getAttribute('role') ? '[role='+cur.getAttribute('role')+']' : '')
      + (cur.getAttribute('aria-live') ? '[aria-live='+cur.getAttribute('aria-live')+']' : '')
      + (cur.getAttribute('data-turn-key') ? '[turn='+cur.getAttribute('data-turn-key')+']' : '')
      + (cur.getAttribute('data-testid') ? '[testid='+cur.getAttribute('data-testid')+']' : '')
      + (cls ? '.'+cls : ''));
    cur = cur.parentElement;
  }
  return seg.join(' < ');
}
var SEL = '[role="alert"],[role="status"],[aria-live="polite"],[aria-live="assertive"],'
        + 'form div[class*="footer" i],div[class*="thread-scroll" i] > div[class*="footer" i]';
var bs = document.querySelectorAll(SEL);
var hits = [];
for (var i = 0; i < bs.length; i++) {
  var t = n(bs[i].innerText);
  if (t.indexOf('你说：') < 0 && t.indexOf('ChatGPT 说：') < 0) continue;
  hits.push({
    text: t.slice(0, 160),
    len: t.length,
    chain: chain(bs[i]),
    in_msg_probe: !!(bs[i].closest && bs[i].closest(
      '[data-turn-key],[data-message-id],article,[class*="markdown" i],[class*="prose" i]')),
    tag_marker: (function(){
      var p = bs[i].closest('[data-message-id]') ? 'data-message-id' :
              bs[i].closest('[data-turn-key]') ? 'data-turn-key' :
              bs[i].closest('[data-testid]') ? (bs[i].closest('[data-testid]').getAttribute('data-testid')) : '';
      return p;
    })()
  });
}
return {count_total: bs.length, hits: hits};
"""

page = ChromiumPage(addr_or_opts="127.0.0.1:9222")
t = page.get_tabs()[0]
print(json.dumps(t.run_js(JS), ensure_ascii=False, indent=1)[:3000])
