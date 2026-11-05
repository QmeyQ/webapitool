"""
send_verify.py - 发送校验辅助模块

从 browser_core.py 拆出的发送后校验信号采集（用户要求：
不要全堆在一个文件里，工具类独立成模块）。

设计原则：
- 金标准 = [data-turn-key] 会话轮数量增加（chatgpt 2026-11 实测
  有效；其他站点查无此属性返回 -1，自动退回旧信号）
- 输入框清空判据必须用 **fill 之后、CLICK 之前** 采样的基准值
  （fill 前采样的 input_len 本来就是 0，清空判据永远不成立——
  本次误判"发送未生效"的直接根因）
- 生成指示器多信号：data-testid 停止按钮 / aria-label 停止 / data-beam
"""

import json

# 选择器一律由调用方先经 browser_core._plain_css 处理成纯 CSS，
# 本模块不做 DrissionPage 语法解析（避免循环导入）


# 采样取"可见编辑器"的 JS 片段：站点 UI 变体常并存隐藏的旧输入框
# 实例（2026-11-05 chatgpt 首页实锤：2 个 ProseMirror，第 0 个隐藏
# 空文本，真实编辑器在第 1 个）。querySelector 撞上隐藏实例 →
# input_len 恒 0 → 清空判据永不成立 → "写入了检查为写入失败"。
# 可见优先 → 聚焦兜底 → 首个兜底。
_VISIBLE_INP_JS = (
    "(function(){try{"
    "const els=document.querySelectorAll(%s);"
    "for(const e of els){"
    "if(e.offsetWidth||e.offsetHeight)return e;}"
    "for(const e of els){if(e===document.activeElement)return e;}"
    "return els[0]||null;"
    "}catch(e){return null}})()")


def capture_send_state_js(input_selector: str, msg_selector: str) -> str:
    """发送前/后状态采样 JS：输入框长度 + 消息节点数 + 轮数。

    返回 JSON 字符串（i=input_len, m=msg_count, t=turn_count,
    a=article 数, b=排除输入框后的全页可见文本长度）。

    a/b 是通用兜底信号：站点改版/混淆 class 轮换导致
    msg_selector/turn 选择器失配（m/t 恒 -1 或不增）时，
    article 增长与全页文本增长仍能确认消息真的上屏——
    2026-11-05 实测 chatgpt 页面 article 结构随 UI 变体消失，
    单一站点专属选择器"有时正确有时不正确"。"""
    return (
        "const inp = " + _VISIBLE_INP_JS % json.dumps(
            input_selector or '___none___') + ";"
        "const t = inp ? ((inp.value != null) ? inp.value : "
        "(inp.textContent || '')) : '';"
        "let bl = -1;"
        "try {"
        "  const clone = document.body.cloneNode(true);"
        "  const ed = clone.querySelector(%s);"
        "  if (ed) ed.remove();"
        "  bl = ((clone.innerText || '').replace(/\\s+/g, '')).length;"
        "} catch (e) {}"
        "return JSON.stringify({"
        "i: t.length,"
        "m: document.querySelectorAll(%s).length,"
        "t: document.querySelectorAll('[data-turn-key]').length,"
        "a: document.querySelectorAll('article').length,"
        "b: bl});"
        % (json.dumps(input_selector or '___none___'),
           json.dumps(msg_selector or 'div[id^="message-"]')))


def poll_signal_js(input_selector: str) -> str:
    """发送后单次轮询 JS：返回信号字符串。

    - 'input_not_empty'  输入框仍有内容（未发送）
    - 'generating'       生成指示器激活（多信号）
    - 'maybe_sent'       输入框已清空，需上层对比计数确认
    """
    return (
        "const inp = " + _VISIBLE_INP_JS % json.dumps(
            input_selector) + ";"
        "const t = inp ? ((inp.value != null) ? inp.value : "
        "(inp.textContent || '')) : '';"
        "if (t.trim().length > 0) return 'input_not_empty';"
        "if (document.querySelector('[data-testid=\"stop-button\"],"
        "[data-testid=\"stop\"]')) return 'generating';"
        "if (document.querySelector('[data-testid*=\"stop\" i]')) "
        "return 'generating';"
        "if (document.querySelector('[data-beam][data-active]')) "
        "return 'generating';"
        "if (document.querySelector('button[aria-label*=\"停止\"]')) "
        "return 'generating';"
        "if (document.querySelector('button[aria-label*=\"stop\" i]')) "
        "return 'generating';"
        "return 'maybe_sent';")


def sent_confirmed(pre: dict, now: dict) -> bool:
    """对比前后状态判定发送成功（纯函数，可单测）。

    任一成立即成功：
    - 会话轮数增加（金标准）
    - 消息节点数增加
    - article 数增加（通用兜底：站点专属消息选择器失配时）
    - 输入框从非空变空（基准必须取 fill 后值）
    - 全页文本（排除输入框）显著增长——消息上屏的最终兜底，
      不依赖任何站点专属 UI。阈值 = max(60, min(草稿长一半, 1500))：
      toast/横幅等页面噪声（"出了点问题"/"正在加载"类提示均 <60
      字符）够不到；草稿大时上限 1500 保证流式回复前消息体即可达标。"""
    for k in ("turn_count", "msg_count", "article_count"):
        a, b = pre.get(k, -1), now.get(k, -1)
        if a >= 0 and b > a:
            return True
    a, b = pre.get("input_len", -1), now.get("input_len", -1)
    if a > 0 and b == 0:
        return True
    pa, pb = pre.get("body_len", -1), now.get("body_len", -1)
    if pa >= 0 and pb >= 0:
        grown = pb - pa
        draft = max(pre.get("input_len", 0), 0)
        if grown >= max(60, min(draft // 2, 1500)):
            return True
    return False


def account_check_js() -> str:
    """账号状态检查 JS（chatgpt 系）：UI 判据为主，接口为辅。

    2026-11-05 修正：/backend-api/me 对已登录账号也返回 ua- 匿名 id
    （email 空），绝不能再作为登录判据——实测 9223/9224/9226 登录
    完好（历史会话+账号名+升级订阅齐全）但 /me 全部报匿名，误判
    "登录丢失"。/api/auth/session 也会被 Cloudflare 拦截返回 HTML。
    可靠判据（页面 UI）：
    - 已登录：历史会话链接（a[href^="/c/"]）、升级订阅/Upgrade 文案
    - 未登录：免费注册/Sign up 引导 + 登录按钮，且无历史/升级
    - stuck：toast 卡"正在加载个人资料"
    返回 JSON：{anon, sess, email, stuck, error}。sess=True 是已登录
    的正信号（session 接口返回 accessToken），anon 只在 UI 明确
    显示未登录引导时为 True。
    """
    return (
        "return new Promise(res => {"
        "const done = r => res(JSON.stringify(r));"
        "const finish = sess => {"
        "const txt = (document.body.innerText || '');"
        "const signup = /免费注册|Sign up/.test(txt);"
        "const loginBtn = !!document.querySelector("
        "'[data-testid=\"login-button\"]');"
        "const upgrade = /升级订阅|Upgrade plan/.test(txt);"
        "const historyLinks = document.querySelectorAll("
        "'a[href^=\"/c/\"]').length;"
        "const toasts = [...document.querySelectorAll("
        "'[data-state=\"open\"],[role=\"status\"]')]"
        ".map(e => (e.innerText||'').trim())"
        ".filter(t => t && t.length < 60);"
        # 2026-11-05 实测：已登录页面（有历史+升级）的常驻 role=status
        # 元素也会命中 loading 文本，单看 toast 全员误报——stuck 需页面
        # 确实未就绪（无历史且无升级）。加载完成的页面不可能 stuck。
        "const stuck = toasts.some(t => /正在加载个人资料|loading profile/i.test(t))"
        "&& historyLinks === 0 && !upgrade;"
        "const anon = (signup || loginBtn) && !upgrade && historyLinks === 0;"
        "done({anon: anon, sess: sess,"
        "email: (sess && sess.user && sess.user.email) || '', stuck: stuck});"
        "};"
        "fetch('/api/auth/session', {credentials: 'include'})"
        ".then(r => r.text())"
        ".then(x => {"
        "let sess = false;"
        "if (x && x.startsWith('{')) {"
        "try { const d = JSON.parse(x);"
        "sess = !!(d.accessToken);"
        "if (sess) { sess = d; } } catch (e) {}"
        "}"
        "finish(sess);"
        "})"
        ".catch(e => finish(false));"
        "});")


def interpret_account_check(raw) -> dict:
    """解析 account_check_js 返回值（容错）。

    anon=True 或 stuck=True 都意味着发送不可能成功，
    应报 auth_error 让用户重新登录，而不是笼统"发送未生效"。
    sess（accessToken 正信号）可对冲 UI 判据的误报：已拿到
    accessToken 时即使 UI 引导文案偶现也不判匿名。"""
    out = {"anon": False, "stuck": False, "sess": False,
           "email": "", "error": ""}
    if not isinstance(raw, str) or not raw.startswith('{'):
        out["error"] = f"unexpected: {raw!r}"[:120]
        return out
    try:
        d = json.loads(raw)
    except Exception as e:
        out["error"] = str(e)
        return out
    out["anon"] = bool(d.get("anon"))
    out["stuck"] = bool(d.get("stuck"))
    out["email"] = d.get("email") or ""
    out["sess"] = bool(d.get("sess"))
    out["error"] = d.get("error") or ""
    # 正信号兜底：拿到 accessToken 说明会话有效，UI 判据误报不成立
    if out["sess"]:
        out["anon"] = False
    return out
