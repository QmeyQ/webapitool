"""现场取证：定位站点"账号受限/用量上限"提示在 DOM 里的真实位置。

目的：classify 只能吃"站点自己的提示区"语料，不能吃整页正文——正文里混着
我们自己的 prompt（含 rate limit 字样）。所以要实测这条提示挂在哪个容器、
有没有 role=alert / aria-live、旁边是不是有"重试"按钮。只读，不改页面。
"""
import json
import sys

from DrissionPage import ChromiumPage

JS = r"""
function n(s){return (s||'').replace(/\s+/g,' ').trim();}
function path(e){
  var seg=[], cur=e;
  for (var d=0; d<7 && cur && cur.tagName; d++) {
    var cls = n(typeof cur.className === 'string' ? cur.className : '')
                .split(' ').slice(0,3).join('.');
    seg.push(cur.tagName.toLowerCase()
      + (cur.getAttribute('role') ? '[role=' + cur.getAttribute('role') + ']' : '')
      + (cur.getAttribute('aria-live') ? '[aria-live=' + cur.getAttribute('aria-live') + ']' : '')
      + (cls ? '.' + cls : ''));
    cur = cur.parentElement;
  }
  return seg.join(' < ');
}
var keys = ['limit of messages per hour','messages per hour','try again later',
            'reached our limit','每小时','稍后再试','使用上限','受限'];
var bodyAll = Array.from(document.querySelectorAll('div,p,span,section'));
var hits = [];
for (var i = 0; i < bodyAll.length && hits.length < 6; i++) {
  var e = bodyAll[i];
  if (e.childElementCount > 3) continue;          // 只要最内层文本节点
  var t = n(e.innerText);
  if (t.length < 12 || t.length > 400) continue;
  for (var k = 0; k < keys.length; k++) {
    if (t.toLowerCase().indexOf(keys[k].toLowerCase()) >= 0) {
      var btns = [];
      var p = e;
      for (var u = 0; u < 6 && p; u++) {
        Array.from(p.querySelectorAll('button,a,[role="button"]')).forEach(function (b) {
          btns.push(n(b.innerText || b.getAttribute('aria-label')).slice(0, 20));
        });
        if (btns.length) break;
        p = p.parentElement;
      }
      hits.push({text: t.slice(0, 200), key: keys[k], depth: u,
                 container: path(e), nearby_buttons: btns.slice(0, 8),
                 in_alert: !!e.closest('[role="alert"],[role="status"],[aria-live]'),
                 in_form: !!e.closest('form')});
      break;
    }
  }
}
return {
  url: location.href.slice(0, 80),
  hits: hits,
  banner: (function () {
    var bits = [], bs = document.querySelectorAll(
      '[role="alert"],[role="status"],[aria-live="polite"],[aria-live="assertive"],'
      + 'form div[class*="footer" i],div[class*="thread-scroll" i] > div[class*="footer" i]');
    for (var z = 0; z < bs.length && bits.length < 8; z++) {
      var tz = n(bs[z].innerText);
      if (tz.length > 8 && tz.length < 400) bits.push(tz);
    }
    return bits.join(' | ').slice(0, 600);
  })(),
  retry_buttons: Array.from(document.querySelectorAll('button,a,[role="button"]'))
    .map(function (b) { return n(b.innerText || b.getAttribute('aria-label')).slice(0, 24); })
    .filter(function (x) { return /重试|retry|try again|升级|upgrade/i.test(x); }),
  editors: document.querySelectorAll('.ProseMirror,#prompt-textarea,[contenteditable=true]').length,
  sidebar_plan: (function () {
    var el = Array.from(document.querySelectorAll('a,div,span')).filter(function (x) {
      return /免费版|Free|Plus/i.test(n(x.innerText)) && n(x.innerText).length < 24;
    })[0];
    return el ? n(el.innerText) : '';
  })()
};
"""

port = sys.argv[1] if len(sys.argv) > 1 else "9222"
page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
rows = []
for t in page.get_tabs():
    try:
        rows.append(t.run_js(JS))
    except Exception as e:
        rows.append({"error": str(e)[:120]})
print(json.dumps(rows, ensure_ascii=False, indent=1)[:4000])
