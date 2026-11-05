"""
browser_core.py - 浏览器自动化核心模块

【修改声明】本文件修改自 Universal Web-to-API（AGPL-3.0）上游项目。
修改清单见 CHANGES.md，最后修改日期 2026-11-05。本程序整体仍以 AGPL-3.0 发布。

v5.3 关键修复（IDE 工具调用兼容）：
- ToolSchemaConformer：工具名/参数名/类型对齐 IDE 声明的 tools schema，
  必填缺失补占位（IDE 报可见错误而非静默卡死）
- tools 定义兼容 OpenAI / Anthropic(input_schema) / Gemini(parameters) 风格
- 续聊上下文保留 assistant tool_calls 配对，网页 AI 不再"失忆"
- 流式首 chunk 携带 role:assistant；DSML/JSON 解析出口统一做 schema 对齐

v5.2 关键修复：
- DIRECT_TOOL_DEBUG=False
- ToolCallStreamParser 支持 5 种格式（```tool_call / ```json / 裸 / DSML / <tool_call>）
- 参数名归一化 + 完整 JSON 分片缓冲
- 检测并点击 DeepSeek "继续" 按钮（ds-button__background），点击后重置沉默计时继续获取

v5.4 关键修复（IDE 交互规则对齐 + 输出净化）：
- 深度思考独立通道：网页端"思考"容器（class/id 含 think/thought/reason）
  的文本不再混入正文，改走 OpenAI 兼容的 reasoning_content 增量下发
  （流式 pack_reasoning_chunk / 非流式 message.reasoning_content）
- 流头部净化：提示词角色标记回显（[user]/[assistant]/[tool_result…]）
  与"已深度思考（用时X秒）"等 UI 叙述行不再泄漏为正文
- 正文抽取跳过 script/style/noscript/template/iframe/svg
- finish chunk 补齐 usage；发送前重复 tool_call 去重
"""

import os
import re
import time
import json
import socket
import random
import logging
import threading
import hashlib
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse
from typing import Generator, Optional, Dict, Any, List, Callable
from dataclasses import dataclass
from DrissionPage import ChromiumPage

import send_verify


DIRECT_TOOL_DEBUG = False


# 多实例编辑器站点的输入元素解析（挂到 window 上供本模块各段 JS 复用）。
#
# ChatGPT 点"新聊天"后 DOM 里会同时留着新旧两组 composer，文档序第一个常是
# 已废弃的隐藏实例；而 DrissionPage 的 finder 可能命中另一个。写入用 A、读回
# 用 B 时，读回恒为 0 → 被报成"编辑器拒绝注入"，真实原因只是选错了实例。
# 统一规则：可见优先，聚焦者优先，否则取最后一个可见者（与旧兜底一致）。
_EDITOR_RESOLVE_INSTALL_JS = """
if (typeof window.__pickEditor !== 'function') {
  window.__pickEditor = function (sel) {
    var vis = function (e) {
      return !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
    };
    var cands = [];
    try { cands = Array.from(document.querySelectorAll(sel || '')); }
    catch (e) { cands = []; }
    if (!cands.length) {
      cands = Array.from(document.querySelectorAll(
        '[contenteditable="true"],[contenteditable="plaintext-only"],'
        + '.ProseMirror,#prompt-textarea,textarea'));
    }
    var live = cands.filter(vis);
    var pool = live.length ? live : cands;
    if (!pool.length) return null;
    var act = document.activeElement;
    for (var i = 0; i < pool.length; i++) {
      if (pool[i] === act) return pool[i];
    }
    return pool[pool.length - 1];
  };
}
return 1;
"""


class BrowserConstants:
    """浏览器相关常量"""

    _config = None
    _config_file = Path("browser_config.json")

    _DEFAULTS = {
        'DEFAULT_PORT': 9222,
        'CONNECTION_TIMEOUT': 10,
        'EDITOR_READY_TIMEOUT': 30,
        'STEALTH_DELAY_MIN': 0.1,
        'STEALTH_DELAY_MAX': 0.3,
        'ACTION_DELAY_MIN': 0.15,
        'ACTION_DELAY_MAX': 0.3,
        'DEFAULT_ELEMENT_TIMEOUT': 3,
        'FALLBACK_ELEMENT_TIMEOUT': 1,
        'ELEMENT_CACHE_MAX_AGE': 5.0,
        'STREAM_CHECK_INTERVAL_MIN': 0.1,
        'STREAM_CHECK_INTERVAL_MAX': 1.0,
        'STREAM_CHECK_INTERVAL_DEFAULT': 0.3,
        'STREAM_SILENCE_THRESHOLD': 8.0,
        'STREAM_MAX_TIMEOUT': 600,
        'STREAM_INITIAL_WAIT': 180,
        'STREAM_RERENDER_WAIT': 0.5,
        'STREAM_CONTENT_SHRINK_TOLERANCE': 3,
        'STREAM_MIN_VALID_LENGTH': 10,
        'STREAM_STABLE_COUNT_THRESHOLD': 10,
        'STREAM_SILENCE_THRESHOLD_FALLBACK': 15.0,
        'MAX_MESSAGE_LENGTH': 1000000,
        'MAX_MESSAGES_COUNT': 100,
        'STREAM_INITIAL_ELEMENT_WAIT': 10,
        'STREAM_MAX_ABNORMAL_COUNT': 5,
        'STREAM_MAX_ELEMENT_MISSING': 10,
        'STREAM_CONTENT_SHRINK_THRESHOLD': 0.3,
        'STREAM_USER_MSG_WAIT': 1.5,
        'STREAM_PRE_BASELINE_DELAY': 0.3,
        'UPLOAD_WAIT_AFTER': 1.5,
        'CONTINUE_CHECK_SILENCE': 2.0,
        'DIRECT_CHUNK_CHARS': 12000,
    }

    DEFAULT_PORT = 9222
    CONNECTION_TIMEOUT = 10

    STEALTH_DELAY_MIN = 0.1
    STEALTH_DELAY_MAX = 0.3
    ACTION_DELAY_MIN = 0.15
    ACTION_DELAY_MAX = 0.3

    DEFAULT_ELEMENT_TIMEOUT = 3
    FALLBACK_ELEMENT_TIMEOUT = 1
    ELEMENT_CACHE_MAX_AGE = 5.0

    STREAM_CHECK_INTERVAL_MIN = 0.1
    STREAM_CHECK_INTERVAL_MAX = 1.0
    STREAM_CHECK_INTERVAL_DEFAULT = 0.3

    STREAM_SILENCE_THRESHOLD = 8.0
    STREAM_MAX_TIMEOUT = 600
    STREAM_INITIAL_WAIT = 180

    STREAM_RERENDER_WAIT = 0.5
    STREAM_CONTENT_SHRINK_TOLERANCE = 3
    STREAM_MIN_VALID_LENGTH = 10

    STREAM_STABLE_COUNT_THRESHOLD = 10
    STREAM_SILENCE_THRESHOLD_FALLBACK = 15.0

    MAX_MESSAGE_LENGTH = 1000000
    MAX_MESSAGES_COUNT = 100

    STREAM_INITIAL_ELEMENT_WAIT = 10
    STREAM_MAX_ABNORMAL_COUNT = 5
    STREAM_MAX_ELEMENT_MISSING = 10
    STREAM_CONTENT_SHRINK_THRESHOLD = 0.3

    STREAM_USER_MSG_WAIT = 1.5
    STREAM_PRE_BASELINE_DELAY = 0.3

    @classmethod
    def _load_config(cls):
        # 依次尝试：当前工作目录 → 本模块所在目录
        # （browser_config.json 原先只按相对路径读，启动目录不同会静默回退到默认值）
        candidates = [cls._config_file,
                      Path(__file__).resolve().parent / "browser_config.json"]
        for candidate in candidates:
            if candidate.exists():
                try:
                    with open(candidate, 'r', encoding='utf-8') as f:
                        cls._config = json.load(f)
                    return
                except Exception:
                    pass
        cls._config = cls._DEFAULTS.copy()

    @classmethod
    def get(cls, key: str):
        if cls._config is None:
            cls._load_config()
        return cls._config.get(key, cls._DEFAULTS.get(key))

    @classmethod
    def get_defaults(cls):
        return cls._DEFAULTS.copy()

    @classmethod
    def reload(cls):
        cls._config = None
        cls._load_config()


# 深度思考 reasoning_content 独立通道开关（browser_config.json / env 可覆盖）。
# 默认开启：思考增量走 delta.reasoning_content（DeepSeek/OpenRouter 风格）。
# 若客户端不识别该字段，可设 BROWSER_SEND_REASONING=false 或
# browser_config.json "SEND_REASONING_CONTENT": false 关闭思考下发。
SEND_REASONING_CONTENT = (
    os.environ.get('BROWSER_SEND_REASONING', '').lower() != 'false'
    and BrowserConstants.get('SEND_REASONING_CONTENT') is not False)


def _plain_css(selector: str) -> str:
    """DrissionPage 选择器 → 纯 CSS 选择器（供 document.querySelector 用）。

    站点配置里 input_box 等可能带 'css:' 前缀，直接注入 JS 的
    querySelector 会抛 SyntaxError 导致发送校验信号全灭。"""
    s = (selector or "").strip()
    if s.lower().startswith("css:"):
        return s[4:]
    if s.lower().startswith("tag:"):
        return s[4:]
    return s


def _resolve_profile_dir(raw: str) -> str:
    """账号 profile 路径解析（锚定项目根，失效时自动改道）。

    user_data_dir 写绝对路径时，项目目录一旦改名/移动，旧路径并不报错，
    Chrome 会静默新建一个未登录的空 profile——表现是"输入框找不到、
    附件注入无反应"，排查方向极易被带去怀疑站点或额度。2026-11-05 实锤：
    配置里的 desktop 旧目录已不存在，5 个账号在 16:55 全被新建空 profile 顶掉。
    因此相对路径按项目根解析；绝对路径不存在而项目内有同名 profile 时改用它。"""
    if not raw:
        return ""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    if p.exists():
        return str(p)
    local = (Path(__file__).resolve().parent
             / "chrome_profiles" / p.name)
    if local.is_dir():
        logger.warning(
            f"[Instance] profile 路径已失效: {p} —— 改用项目内同名 profile "
            f"{local}（登录态在此）")
        return str(local)
    return str(p)


# 站点页面是否还处在"能输入"的状态：错误页 / 登录墙 / 无可见输入控件
_PAGE_STATE_JS = r"""
var out = {url: location.href || '', err_code: '',
           rich_editors: 0, plain_textareas: 0, login_ctas: 0, btn_texts: ''};
var bt = (document.body && document.body.innerText) || '';
var m = bt.match(/ERR_[A-Z0-9_]+/);
if (m) { out.err_code = m[0]; }
var rich = ['[contenteditable="true"]', '[contenteditable="plaintext-only"]',
            '.ProseMirror', '#prompt-textarea'];
for (var i = 0; i < rich.length; i++) {
  var els = document.querySelectorAll(rich[i]);
  for (var j = 0; j < els.length; j++) {
    var e = els[j];
    if ((e.offsetWidth > 40 || e.offsetHeight > 16) &&
        (e.offsetWidth || e.offsetHeight || e.getClientRects().length)) {
      out.rich_editors++;
    }
  }
}
var tas = document.querySelectorAll('textarea');
for (var k = 0; k < tas.length; k++) {
  var a = tas[k];
  if (a.offsetWidth > 40 || a.offsetHeight > 16) { out.plain_textareas++; }
}
var btns = document.querySelectorAll('button,a,[role="button"]');
var hits = [];
for (var n = 0; n < btns.length; n++) {
  var t = (btns[n].innerText || btns[n].getAttribute('aria-label') || '').trim();
  if (/^(log in|log_in|sign in|sign up|register|get started|continue with|登录|注册|开始使用|立即登录|免费注册)/i.test(t)) { hits.push(t.slice(0, 12)); }
}
if (hits.length) { out.login_ctas = hits.length; out.btn_texts = hits.slice(0, 3).join('|'); }

// 站点自身的"上下文窗口已达上限"横幅：必须同时命中「新聊天类按钮」和
// 该按钮近邻容器里的上限文案。只看文案会把我们自己的 prompt（正文里常出现
// "上下文"）和侧栏常驻的"新聊天"按钮误判成上限，只看按钮更不行。
out.context_full = 0;
out.limit_text = '';
function nearLimitText(e) {
  var n = e;
  for (var d = 0; d < 6 && n; d++) {
    var t = (n.innerText || '').trim();
    if (t.length > 0 && t.length < 400 &&
        /已达上限|上下文窗口.*(上限|已满)|context (window|limit).*reach|reached.*context limit|conversation.*too long/i.test(t)) {
      return t.replace(/\s+/g, ' ').slice(0, 140);
    }
    n = n.parentElement;
  }
  return '';
}
for (var q = 0; q < btns.length; q++) {
  var lab = (btns[q].innerText || btns[q].getAttribute('aria-label') || '').trim();
  if (!/^(新聊天|开启新聊天|start a new chat|new chat|开启新对话)$/i.test(lab)) continue;
  var hit = nearLimitText(btns[q]);
  if (hit) { out.context_full = 1; out.limit_text = hit; break; }
}
return out;
"""

# 状态 → 人话，供守卫与仪表盘复用（避免再把真因说成"额度耗尽"）
_PAGE_STATE_MEANING = {
    "error_page": "站点不可达（浏览器加载到错误页）",
    "need_login": "账号未登录（profile 无会话，停在登录墙）",
    "no_editor": "composer 未渲染（页面结构变化或被跳转）",
}


def probe_page_state(tab) -> dict:
    """只读探测当前标签页可用性：ok / error_page / need_login /
    no_editor / unknown。探测失败一律 unknown，绝不因守卫误伤正常站点。"""
    if tab is None:
        return {"state": "unknown", "detail": "无标签页"}
    try:
        d = tab.run_js(_PAGE_STATE_JS)
    except Exception as e:
        # 探测本身失败也要可见：曾因 JS 拼接出语法错误而静默返回
        # unknown，守卫形同虚设，线上看到的仍是"编辑器拒绝注入"
        logger.warning(f"[Page] 状态探测失败: {e}")
        return {"state": "unknown", "detail": f"探测失败: {e}"}
    if not isinstance(d, dict):
        return {"state": "unknown", "detail": ""}
    url = str(d.get("url") or "")
    if (url.startswith("chrome-error://")
            or url.startswith("about:neterror") or d.get("err_code")):
        return {"state": "error_page", "raw": d,
                "detail": (f"站点页面加载失败 "
                           f"({d.get('err_code') or '错误页'}，"
                           f"当前 URL {url[:60]})")}
    # 登录 CTA 与富文本编辑器分开判定：ChatGPT 登出态页面上仍有一个
    # textarea 占位，只按"有没有输入控件"会被误判成可用页面
    if d.get("login_ctas") and not d.get("rich_editors"):
        return {"state": "need_login", "raw": d,
                "detail": (f"页面停在登录墙（出现 "
                           f"{d.get('btn_texts') or '登录/注册'} 入口，"
                           "且没有真正的富文本编辑器）")}
    if not (d.get("rich_editors") or d.get("plain_textareas")):
        return {"state": "no_editor", "raw": d,
                "detail": "页面上没有可见输入控件"}
    st = {"state": "ok", "raw": d, "detail": ""}
    # 上下文上限不改 state（页面照样可输入可发送，站点只是静默丢上文），
    # 只带一个标记：调用方据此新起对话并停用压缩
    if d.get("context_full"):
        st["context_full"] = True
        st["limit_text"] = str(d.get("limit_text") or "")[:140]
    return st


def _drission_find(tab, selector: str, timeout: int = 1):
    """按 ElementFinder 的语法规则查找元素（与 _find_with_syntax 一致）。

    裸 CSS 选择器必须加 'css:' 前缀，否则 DrissionPage 会当成其他语法
    解析，永远找不到元素——等待循环会全部空转超时。"""
    s = (selector or "").strip()
    if not s:
        return None
    try:
        if s.startswith(('tag:', '@', 'xpath:', 'css:')) or '@@' in s:
            return tab.ele(s, timeout=timeout)
        return tab.ele(f'css:{s}', timeout=timeout)
    except Exception:
        return None


def _split_prompt_chunks(text: str, limit: int) -> List[str]:
    """把超长 prompt 切成 ≤limit 的片段：优先按空行段落边界切，
    超长段落硬切；切完后把过小的片段（<1000 字符）并入相邻片，
    避免出现"分片 1 只有 192 字符"这种碎片（巨型无换行单段硬切
    后的残余首段会独立成片）。"""
    if not text:
        return [""]
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    paras = text.split("\n\n")
    for idx, para in enumerate(paras):
        # 段间补 \n\n；末段不补——否则拼接会比原文多尾部空白，
        # 且拒收重切每轮再膨胀 2 字符
        piece = para + "\n\n" if idx < len(paras) - 1 else para
        if len(piece) > limit:
            if cur:
                chunks.append("".join(cur))
                cur, cur_len = [], 0
            for j in range(0, len(piece), limit):
                chunks.append(piece[j:j + limit])
            continue
        if cur_len + len(piece) > limit:
            chunks.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(piece)
        cur_len += len(piece)
    if cur:
        chunks.append("".join(cur))
    if not chunks:
        return [text[:limit]]
    # 合并小片（<1000 字符）：优先并入前一片（不超过上限+余量），
    # 首片小片则并入下一片开头，避免"分片 1 只有 192 字符"碎片
    merged: List[str] = []
    pending = ""
    for ch in chunks:
        if len(ch) < 1000:
            pending += ch
            continue
        if pending:
            if merged and len(merged[-1]) + len(pending) <= limit + 500:
                merged[-1] += pending
            else:
                ch = pending + ch
            pending = ""
        merged.append(ch)
    if pending:
        if merged:
            merged[-1] += pending
        else:
            merged.append(pending)
    return merged


def match_site_domain(model_id: str, domains: List[str]) -> Optional[str]:
    """model id → 站点域名。精确 > 去点精确 > 子串 > 去点子串；无命中 None。

    去点匹配让 zai/z.a.i 等写法归一到 z.ai。
    复合 id（如 poe/poe.com、poe.com/Assistant）按 "/" 分段逐段尝试，
    任一段命中即返回该站点。"""
    m = (model_id or "").strip().lower()
    if not m:
        return None
    m_flat = m.replace(".", "")
    lowered = [(d, d.lower()) for d in domains if d]
    for _, dl in lowered:
        if m == dl:
            return next(d for d, l in lowered if l == dl)
    for d, dl in lowered:
        if dl.replace(".", "") == m_flat:
            return d
    for d, dl in lowered:
        if m in dl:
            return d
    for d, dl in lowered:
        if m_flat and m_flat in dl.replace(".", ""):
            return d
    # 复合 id 分段匹配（段内不再含 "/"，递归安全）
    if "/" in m:
        for seg in m.split("/"):
            seg = seg.strip()
            if seg and "/" not in seg:
                hit = match_site_domain(seg, domains)
                if hit:
                    return hit
    return None


_MODEL_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def extract_model_slug(model_id: str, domain: Optional[str] = None) -> str:
    """从 model id 提取站点子模型名（poe 即 bot slug），无则返回 ""（主模型）。

    - "poe.com/Assistant" / "poe/Assistant" → "Assistant"
    - "poe.com" / "poe" / "poe/poe.com" / "web-browser" → ""（主模型）
    """
    m = (model_id or "").strip()
    if not m or "/" not in m or m.lower() in ("web-browser", "default"):
        return ""
    segs = [s.strip() for s in m.split("/") if s.strip()]
    if not segs:
        return ""
    seg = segs[-1]
    if "." in seg:  # 域名样式段不是子模型
        return ""
    if domain:
        dl = (domain or "").lower()
        sl = seg.lower()
        if sl == dl or dl.startswith(sl):
            return ""
    if not _MODEL_SLUG_RE.match(seg):
        return ""
    return seg


def get_site_model_candidates(site_cfg: Optional[dict]) -> List[str]:
    """站点候选模型列表：主模型（entry_url 末段路径）+ FALLBACK_MODELS。

    主模型无路径段时为 ""（站点级）。用于积分耗尽时的同站点换模型。"""
    cfg = site_cfg or {}
    out: List[str] = []
    entry = str(cfg.get("entry_url") or "")
    parts = [p for p in entry.split("/") if p]
    # https://poe.com/Assistant → ['https:', 'poe.com', 'Assistant']，
    # 尾段 Assistant；无路径段（仅域名）→ ""
    if len(parts) > 2:
        out.append(parts[-1])
    for fb in (cfg.get("FALLBACK_MODELS") or []):
        if isinstance(fb, str) and fb.strip():
            out.append(fb.strip())
    seen = set()
    dedup = []
    for m in out:
        if m not in seen:
            seen.add(m)
            dedup.append(m)
    return dedup or [""]


def detect_tab_site_domain(tab_url: str, domains: List[str]) -> Optional[str]:
    """tab URL → 配置站点域名（子串命中，最长优先）；无命中 None。

    用于把未匹配任何 model id 的请求映射到"正在使用的站点"
    （活动 tab 当前所在的配置站点）。"""
    u = (tab_url or "").lower()
    if not u:
        return None
    hits = [d for d in domains if d and d.lower() in u]
    if not hits:
        return None
    return max(hits, key=len)


class SecureLogger:
    LOG_SENSITIVE = os.environ.get('BROWSER_LOG_SENSITIVE', 'false').lower() == 'true'

    def __init__(self, name: str, level: int = logging.INFO):
        self._logger = self._setup_logger(name, level)

    def _setup_logger(self, name: str, level: int) -> logging.Logger:
        logger = logging.getLogger(name)
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '[%(asctime)s] %(levelname)s %(message)s',
                datefmt='%H:%M:%S'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        logger.setLevel(level)
        return logger

    def info(self, msg: str, *args, **kwargs):
        self._logger.info(msg, *args, **kwargs)

    def debug(self, msg: str, *args, **kwargs):
        self._logger.debug(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        self._logger.error(msg, *args, **kwargs)

    def info_sensitive(self, msg: str, content: str = None,
                       max_preview: int = 50, *args, **kwargs):
        if content is None:
            self._logger.info(msg, *args, **kwargs)
            return
        if self.LOG_SENSITIVE:
            preview = content[:max_preview] + "..." if len(content) > max_preview else content
            self._logger.info(f"{msg} | preview='{preview}'", *args, **kwargs)
        else:
            self._logger.info(f"{msg} | len={len(content)}", *args, **kwargs)


logger = SecureLogger('browser')


class BrowserError(Exception):
    pass


class BrowserConnectionError(BrowserError):
    pass


class ElementNotFoundError(BrowserError):
    pass


class PageUnavailableError(ElementNotFoundError):
    """页面已不在可交互的站点会话上——不是选择器也不是站点控件的问题。

    kind: error_page（到站点的连接失败，机器级，换账号无用）
          need_login（落在登录/注册墙，该 profile 没有会话）
    """

    def __init__(self, kind: str, detail: str):
        super().__init__(f"[{kind}] {detail}")
        self.kind = kind
        self.detail = detail

    @property
    def machine_level(self) -> bool:
        """机器级故障：换账号/换实例都改变不了"""
        return self.kind == "error_page"


class TooLongError(BrowserError):
    """网页端拒收：提交的消息过长（直输超限且上传通道不可用）。"""


class WorkflowError(BrowserError):
    pass


class WorkflowCancelledError(WorkflowError):
    pass


class ConfigurationError(BrowserError):
    pass


class CreditExhaustedError(BrowserError):
    """站点账号积分耗尽（如 poe 免费积分不够），需切换账号"""
    pass


class CreditStateStore:
    """站点账号积分状态持久化记录（poe 免费积分耗尽等）。

    文件 credit_state.json，key 为 "<domain>:<port>:<model>"，
    model 为空串时退化为 "<domain>:<port>"（站点级记录，兼容旧格式），
    value: {"marked_at": ts, "exhausted_until": ts}。
    - 每个账号的每个模型分开记录（poe 的 model 即 bot slug）
    - 仅模型回复命中积分耗尽文案才记录；超时/报错绝不记录
    - 命中记录且未到期 → 该账号该模型跳过（不再轮询浪费请求）
    - 到期（按 poe 积分重置时间，默认每日 00:00 UTC = 08:00 北京，
      可用 browser_config.json POE_POINTS_RESET_UTC_HOUR 覆盖）→ 记录自动失效
    """

    @classmethod
    def _key(cls, domain: str, port, model: str = "") -> str:
        k = f"{(domain or '').lower()}:{port}"
        m = (model or '').strip()
        return f"{k}:{m}" if m else k
    _FILE = Path(__file__).resolve().parent / "credit_state.json"
    _lock = threading.Lock()
    _cache = None

    @classmethod
    def _load(cls) -> dict:
        try:
            with open(cls._FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    @classmethod
    def _save(cls, data: dict):
        try:
            with open(cls._FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Credit] 状态文件写入失败: {e}")

    @classmethod
    def _reset_hour_utc(cls) -> int:
        try:
            h = BrowserConstants.get('POE_POINTS_RESET_UTC_HOUR')
            if h is not None:
                return max(0, min(23, int(h)))
        except Exception:
            pass
        return 0

    @classmethod
    def _next_reset_ts(cls, now: float) -> float:
        """下一次积分重置时间戳（UTC 每日 reset_hour 点整）。"""
        import datetime as _dt
        h = cls._reset_hour_utc()
        day = _dt.datetime.utcfromtimestamp(now).replace(
            hour=h, minute=0, second=0, microsecond=0)
        ts = day.replace(tzinfo=_dt.timezone.utc).timestamp()
        if ts <= now:
            ts += 86400
        return ts

    @classmethod
    def is_exhausted(cls, domain: str, port, model: str = "") -> bool:
        """model 记录或站点级记录任一命中且未到期 → 耗尽。"""
        if not domain:
            return False
        keys = [cls._key(domain, port, model)]
        if (model or '').strip():
            keys.append(cls._key(domain, port, ""))  # 站点级记录对全部模型生效
        now = time.time()
        with cls._lock:
            data = cls._load()
            hit = False
            for key in keys:
                rec = data.get(key)
                if not rec:
                    continue
                if now >= float(rec.get('exhausted_until') or 0):
                    # 积分已重置：清除记录
                    del data[key]
                    cls._save(data)
                    logger.info(f"[Credit] {key} 积分重置时间已到，记录清除")
                else:
                    hit = True
            return hit

    @classmethod
    def mark_exhausted(cls, domain: str, port, model: str = "") -> float:
        """标记指定模型耗尽，返回重置时间戳。"""
        key = cls._key(domain, port, model)
        now = time.time()
        until = cls._next_reset_ts(now)
        with cls._lock:
            data = cls._load()
            data[key] = {"marked_at": now, "exhausted_until": until}
            cls._save(data)
        logger.warning(
            f"[Credit] {key} 积分耗尽已记录，重置时间 "
            f"{time.strftime('%m-%d %H:%M', time.localtime(until))}")
        return until

    @classmethod
    def exhausted_until(cls, domain: str, port, model: str = "") -> float:
        """有效重置时间：模型级记录优先，其次站点级记录；无记录返回 0。"""
        keys = [cls._key(domain, port, model)]
        if (model or '').strip():
            keys.append(cls._key(domain, port, ""))
        with cls._lock:
            data = cls._load()
            for key in keys:
                rec = data.get(key)
                if rec:
                    return float(rec.get('exhausted_until') or 0)
        return 0.0

    @classmethod
    def clear(cls, domain: str, port, model=None) -> bool:
        """手动清除积分耗尽记录（仪表盘账号管理用）。

        model 为 None 时清除该账号在该站点的全部模型记录（含旧格式）。"""
        base = f"{(domain or '').lower()}:{port}"
        with cls._lock:
            data = cls._load()
            if model is None:
                targets = [k for k in data
                           if k == base or k.startswith(base + ":")]
            else:
                targets = [cls._key(domain, port, model)]
            removed = False
            for key in targets:
                if key in data:
                    del data[key]
                    removed = True
                    logger.info(f"[Credit] 已手动清除积分记录: {key}")
            if removed:
                cls._save(data)
            return removed

    @classmethod
    def records_for_port(cls, port) -> List[Dict[str, Any]]:
        """列出某账号所有未到期的积分记录 [{domain, model, exhausted_until}]。"""
        now = time.time()
        out = []
        with cls._lock:
            data = cls._load()
        for key, rec in data.items():
            if key.startswith("toolong:"):
                continue
            parts = key.split(":")
            if len(parts) < 2 or parts[1] != str(port):
                continue
            until = float(rec.get('exhausted_until') or 0)
            if until <= now:
                continue
            out.append({
                "domain": parts[0],
                "model": parts[2] if len(parts) > 2 else "",
                "exhausted_until": until,
            })
        return out

    # ===== 消息过长拒收标记（chatgpt 免费账号上传额度耗尽 → 大文本
    # 直输超限被拒收："你提交的消息过长，请编辑后重新发送。"）=====
    # key: "toolong:<domain>:<port>"（站点级，与具体模型无关），
    # 到期自动失效（默认 17h，与 chatgpt 上传额度恢复窗口一致）。
    # 仅作标记/展示/手动清除，不参与路由跳过（避免全部标记后无号可用）。

    TOOLONG_RESET_SECONDS = 17 * 3600

    @classmethod
    def _too_long_key(cls, domain: str, port) -> str:
        return f"toolong:{(domain or '').lower()}:{port}"

    @classmethod
    def mark_logged_out(cls, domain: str, port, hours: float = 2.0) -> float:
        """标记站点页面已掉线（匿名会话），2h 内跳过该账号。

        2026-11-05 用户反馈"老是拉起新实例而且拉起的实例还没使用"：
        chatgpt 已掉线的账号没有持久标记，每个请求都会先路由过去、
        发送失败再切号——窗口反复拉起又弃用。复用积分跳过机制
        （is_exhausted 站点级记录），短过期窗口保证用户重新登录后
        最多 2h 自动恢复；登录后也可在仪表盘手动清除。"""
        if not domain:
            return 0.0
        key = cls._key(domain, port)
        now = time.time()
        until = now + max(0.5, hours) * 3600
        with cls._lock:
            data = cls._load()
            data[key] = {"marked_at": now, "exhausted_until": until,
                         "reason": "logged_out"}
            cls._save(data)
        logger.warning(
            f"[Credit] {key} 页面已掉线（匿名会话），{hours:.0f}h 内跳过该账号")
        return until

    @classmethod
    def mark_too_long(cls, domain: str, port) -> float:
        """标记某账号在某站点消息过长拒收，返回重置时间戳。"""
        key = cls._too_long_key(domain, port)
        now = time.time()
        until = now + cls.TOOLONG_RESET_SECONDS
        with cls._lock:
            data = cls._load()
            data[key] = {"marked_at": now, "exhausted_until": until}
            cls._save(data)
        logger.warning(
            f"[TooLong] {key} 消息过长拒收已标记，重置时间 "
            f"{time.strftime('%m-%d %H:%M', time.localtime(until))}")
        return until

    @classmethod
    def too_long_records_for_port(cls, port) -> List[Dict[str, Any]]:
        """列出某账号所有未到期的消息过长标记 [{domain, exhausted_until}]。"""
        now = time.time()
        out = []
        with cls._lock:
            data = cls._load()
        for key, rec in data.items():
            if not key.startswith("toolong:"):
                continue
            parts = key.split(":")
            if len(parts) < 3 or parts[2] != str(port):
                continue
            until = float(rec.get('exhausted_until') or 0)
            if until <= now:
                continue
            out.append({"domain": parts[1], "exhausted_until": until})
        return out

    @classmethod
    def clear_too_long(cls, domain: str, port) -> bool:
        """手动清除某账号在某站点的消息过长标记。"""
        key = cls._too_long_key(domain, port)
        with cls._lock:
            data = cls._load()
            if key in data:
                del data[key]
                cls._save(data)
                logger.info(f"[TooLong] {key} 标记已手动清除")
                return True
        return False


# poe 积分耗尽回复识别：官方中英文文案（短回复场景）
CREDIT_EXHAUSTED_RE = re.compile(
    r"需要更多积分|积分不足|积分已用完|积分不够|积分已耗尽|"
    r"need more points|not enough points|out of (free )?points|"
    r"ran? out of (points|free)|insufficient points",
    re.IGNORECASE)


class SSEFormatter:
    """SSE 响应格式化器，threading.local 统一 response_id"""

    _sequence = 0
    _sequence_lock = threading.Lock()
    _local = threading.local()

    @classmethod
    def _generate_id(cls) -> str:
        timestamp = int(time.time() * 1000)
        with cls._sequence_lock:
            cls._sequence += 1
            seq = cls._sequence
        short_uuid = uuid.uuid4().hex[:6]
        return f"chatcmpl-{timestamp}-{seq}-{short_uuid}"

    @classmethod
    def begin_response(cls) -> str:
        rid = cls._generate_id()
        cls._local.response_id = rid
        logger.debug(f"[SSE] begin_response id={rid}")
        return rid

    @classmethod
    def end_response(cls):
        rid = getattr(cls._local, 'response_id', None)
        if rid:
            logger.debug(f"[SSE] end_response id={rid}")
        cls._local.response_id = None

    @classmethod
    def _current_id(cls) -> str:
        rid = getattr(cls._local, 'response_id', None)
        if rid:
            return rid
        return cls._generate_id()

    @classmethod
    def pack_chunk(cls, content: str, model: str = "web-browser") -> str:
        data = {
            "id": cls._current_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None
            }]
        }
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    @classmethod
    def pack_reasoning_chunk(cls, reasoning: str, model: str = "web-browser") -> str:
        """深度思考增量块：双字段兼容——reasoning_content（DeepSeek）
        + reasoning（OpenRouter），正文 content 分离，IDE 按自身
        支持的字段独立展示/折叠思考过程。"""
        data = {
            "id": cls._current_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": reasoning,
                          "reasoning": reasoning},
                "finish_reason": None
            }]
        }
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    @classmethod
    def pack_role_chunk(cls, model: str = "web-browser") -> str:
        """首块 delta 携带 role:assistant；部分 IDE 的 SSE 解析器依赖它初始化消息。"""
        data = {
            "id": cls._current_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None
            }]
        }
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    @classmethod
    def pack_finish(cls, model: str = "web-browser", finish_reason: str = "stop") -> str:
        data = {
            "id": cls._current_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }],
            # 部分 IDE/网关在流末读取 usage；缺省会告警或计 0，这里始终携带
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\ndata: [DONE]\n\n"

    @classmethod
    def pack_usage_chunk(cls, usage: Dict, model: str = "web-browser") -> str:
        """stream_options.include_usage 终块：choices 为空、仅携带 usage。"""
        data = {
            "id": cls._current_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": usage,
        }
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    @classmethod
    def pack_tool_calls_chunk(cls, tool_calls: List[Dict], model: str = "web-browser") -> str:
        data = {
            "id": cls._current_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": i,
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"]
                                if isinstance(tc["function"].get("arguments"), str)
                                else json.dumps(
                                    tc["function"].get("arguments") or {},
                                    ensure_ascii=False),
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                },
                "finish_reason": None,
            }],
        }
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    @classmethod
    def pack_error(cls, message: str, error_type: str = "execution_error",
                   code: str = "workflow_failed") -> str:
        data = {
            "error": {
                "message": message,
                "type": error_type,
                "code": code
            }
        }
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    @staticmethod
    def pack_error_json(message: str, error_type: str = "execution_error",
                        code: str = "workflow_failed") -> Dict:
        return {
            "error": {
                "message": message,
                "type": error_type,
                "code": code
            }
        }

    @staticmethod
    def pack_non_stream(content: str, model: str = "web-browser") -> Dict:
        return {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }


class MessageValidator:
    VALID_ROLES = {'user', 'assistant', 'system', 'tool'}

    @classmethod
    def validate(cls, messages: Any) -> tuple:
        if messages is None:
            return False, "messages 不能为空", None
        if not isinstance(messages, list):
            return False, "messages 应该是列表", None
        if len(messages) == 0:
            return False, "messages 不能为空列表", None

        total_in = len(messages)
        max_count = int(BrowserConstants.get('MAX_MESSAGES_COUNT') or 100)
        if total_in > max_count:
            # 滑窗裁剪而非拒单（修复 invalid_messages/4028）：
            # 保留开头 system 消息 + 最近 keep 条消息
            head_system = []
            for m in messages:
                if isinstance(m, dict) and m.get('role') == 'system':
                    head_system.append(m)
                else:
                    break
            keep = max_count - len(head_system)
            if keep < 1:
                keep = 1
            start = total_in - keep
            # 窗口首条若是 tool 结果，向前扩到其所属 assistant 消息，保持调用/结果配对完整
            while start > 0 and isinstance(messages[start], dict) \
                    and messages[start].get('role') == 'tool':
                start -= 1
            messages = head_system + list(messages[start:])
            logger.warning(
                "[Validate] 消息数量 %d 超过上限 %d，已滑窗裁剪为 %d 条",
                total_in, max_count, len(messages),
            )

        sanitized = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return False, f"messages[{i}] 不是字典类型", None

            role = msg.get('role', 'user')
            if role not in cls.VALID_ROLES:
                role = 'user'

            content = msg.get('content', '')
            if not isinstance(content, str):
                content = str(content) if content is not None else ''

            max_len = int(BrowserConstants.get('MAX_MESSAGE_LENGTH') or 100000)
            if len(content) > max_len:
                # 首尾保真截断而非拒单；最终 prompt 长度仍由 LongPromptRouter 落盘/分片兜底
                head_len = int(max_len * 0.7)
                tail_len = max_len - head_len
                logger.warning(
                    "[Validate] messages[%d].content 长度 %d 超过 %d，已截断保留首尾",
                    i, len(content), max_len,
                )
                content = (content[:head_len]
                           + "\n\n...[内容过长，中间部分已截断]\n\n"
                           + content[-tail_len:])

            sanitized_msg = {'role': role, 'content': content}
            if role == 'tool':
                if 'tool_call_id' in msg:
                    sanitized_msg['tool_call_id'] = msg['tool_call_id']
                if 'name' in msg:
                    sanitized_msg['name'] = msg['name']
            if role == 'assistant' and 'tool_calls' in msg:
                sanitized_msg['tool_calls'] = msg['tool_calls']

            sanitized.append(sanitized_msg)

        return True, None, sanitized


class ToolSchemaConformer:
    """
    把 AI 输出的工具名/参数对齐到 IDE 声明的 tools schema。

    选择工具的是网页端 AI，其输出格式与参数命名不可控；中转层必须把调用
    改写成 IDE 声明的那份 schema，否则 IDE 侧校验失败会静默丢弃工具调用
    （表现为"IDE 收到答复后没下文"）。

    兼容点：
    - 工具定义：OpenAI function / Anthropic input_schema / Gemini parameters
      / 裸 {name, parameters} / parameters 为 JSON 字符串
    - 工具名：精确 -> 归一化（read_file / readFile / read-file）-> 唯一包含匹配
    - 参数名：精确 -> 归一化 -> 唯一包含匹配（file_path / filePath / path）
    - 参数类型：string / number / integer / boolean / array / object 双向转换
    - schema 未声明的参数丢弃（避免 IDE 严格校验报错）
    - 必填参数缺失时补空值占位，让 IDE 报出可见错误而不是静默卡死
    """

    @staticmethod
    def _norm(s: Any) -> str:
        if not isinstance(s, str):
            return ""
        return re.sub(r'[^a-z0-9]', '', s.lower())

    # ---------------- 工具定义归一化 ----------------

    @classmethod
    def _parse_schema(cls, v: Any) -> Dict:
        if isinstance(v, dict):
            return v
        if isinstance(v, str) and v.strip():
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return {}

    @classmethod
    def _lower_schema_types(cls, node: Any, depth: int = 0) -> Any:
        """Gemini 风格大写类型转小写；只递归 properties/items 并限深。"""
        if depth > 4 or not isinstance(node, dict):
            return node
        out = dict(node)
        if isinstance(out.get("type"), str):
            out["type"] = out["type"].strip().lower()
        props = out.get("properties")
        if isinstance(props, dict):
            out["properties"] = {
                k: cls._lower_schema_types(p, depth + 1) for k, p in props.items()
            }
        items = out.get("items")
        if isinstance(items, dict):
            out["items"] = cls._lower_schema_types(items, depth + 1)
        return out

    @classmethod
    def normalize_tools(cls, tools: Optional[List[Any]]) -> List[Dict]:
        """任意风格 tools -> OpenAI {type:function, function:{name,description,parameters}}。"""
        result: List[Dict] = []
        if not tools:
            return result
        if isinstance(tools, dict):
            tools = [tools]
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") if isinstance(t.get("function"), dict) else None
            src = fn or t
            name = src.get("name") or t.get("name")
            if not name or not isinstance(name, str):
                continue
            desc = src.get("description") or t.get("description") or ""
            params = src.get("parameters")
            if params is None:
                params = (src.get("input_schema") or src.get("inputSchema")
                          or t.get("input_schema") or t.get("inputSchema")
                          or t.get("parameters"))
            if params is None and isinstance(src.get("arguments"), dict):
                params = src.get("arguments")
            params = cls._lower_schema_types(cls._parse_schema(params))
            params = dict(params)
            if not params.get("type"):
                params["type"] = "object"
            if not isinstance(params.get("properties"), dict):
                params["properties"] = {}
            if not isinstance(params.get("required"), list):
                params["required"] = []
            result.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc or "",
                    "parameters": params,
                },
            })
        return result

    # ---------------- 工具名匹配 ----------------

    @classmethod
    def match_tool(cls, name: Optional[str], known_tools: List[Dict]) -> Optional[Dict]:
        """返回与 name 对齐的 function schema（name/parameters 均为 IDE 原始声明）。"""
        if not name or not isinstance(name, str) or not known_tools:
            return None
        fns = []
        for t in known_tools:
            fn = t.get("function") or {} if isinstance(t, dict) else {}
            if fn.get("name"):
                fns.append(fn)
        for fn in fns:
            if fn.get("name") == name:
                return fn
        target = cls._norm(name)
        if not target:
            return None
        for fn in fns:
            if cls._norm(fn.get("name", "")) == target:
                return fn
        cands = []
        for fn in fns:
            n = cls._norm(fn.get("name", ""))
            if not n:
                continue
            if n.startswith(target) or target.startswith(n) or target in n or n in target:
                cands.append(fn)
        if len(cands) == 1:
            return cands[0]
        return None

    # ---------------- 参数对齐 ----------------

    @classmethod
    def _coerce_type(cls, value: Any, prop: Dict) -> Any:
        ptype = (prop or {}).get("type")
        if ptype == "string":
            if isinstance(value, str):
                return value
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            if value is None:
                return ""
            return str(value)
        if ptype in ("number", "integer"):
            if isinstance(value, bool):
                return 1 if value else 0
            if isinstance(value, str):
                try:
                    return int(value) if ptype == "integer" else float(value)
                except ValueError:
                    return value
            return value
        if ptype == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "y", "on")
            return bool(value)
        if ptype == "array":
            if isinstance(value, str):
                try:
                    v = json.loads(value)
                    return v if isinstance(v, list) else [value]
                except Exception:
                    return [value]
            if not isinstance(value, list):
                return [value]
            return value
        if ptype == "object":
            if isinstance(value, str):
                try:
                    v = json.loads(value)
                    return v if isinstance(v, dict) else {"_raw": value}
                except Exception:
                    return {"_raw": value}
            return value
        return value

    @classmethod
    def _default_for(cls, prop: Dict) -> Any:
        ptype = (prop or {}).get("type")
        if ptype == "array":
            return []
        if ptype == "object":
            return {}
        if ptype in ("number", "integer"):
            return 0
        if ptype == "boolean":
            return False
        return ""

    @classmethod
    def conform_args(cls, fn_schema: Optional[Dict], args: Any) -> Any:
        """按 IDE schema 重写参数：改名、强转类型、丢弃多余字段、补必填占位。"""
        if fn_schema is None:
            return args
        params = fn_schema.get("parameters") or {}
        props = params.get("properties") or {}
        required = params.get("required") or []
        if not isinstance(required, list):
            required = []

        # 非字典参数兑底：列表 -> 单参数工具直接映射；标量 -> 尝试解析/单必填映射
        if not isinstance(args, dict):
            if isinstance(args, list):
                if len(props) == 1:
                    only = next(iter(props))
                    return {only: cls._coerce_type(args, props[only])}
                return args
            raw = args
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        args = parsed
                except Exception:
                    pass
            if not isinstance(args, dict):
                if len(required) == 1 and raw not in (None, ""):
                    return {required[0]: cls._coerce_type(raw, props.get(required[0]))}
                args = {} if raw in (None, "") else {"_raw": raw}

        if not props:
            return args

        # 只有一个 _raw 时先尝试二次解析
        if set(args.keys()) == {"_raw"}:
            raw = args["_raw"]
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        args = parsed
                except Exception:
                    pass

        norm_map = {cls._norm(k): k for k in props.keys()}
        out: Dict[str, Any] = {}
        for k, v in args.items():
            if k == "_raw":
                continue
            nk = cls._norm(k)
            target = k if k in props else norm_map.get(nk)
            if target is None and nk:
                for pn, pk in norm_map.items():
                    if pn and (pn in nk or nk in pn):
                        target = pk
                        break
            if target is None:
                continue
            out[target] = cls._coerce_type(v, props.get(target))

        for rk in required:
            if rk in out or rk not in props:
                continue
            logger.warning(
                f"[Conform] 必填参数 {rk} 缺失（工具 {fn_schema.get('name')}），补空值占位")
            out[rk] = cls._default_for(props.get(rk))
        return out


class ToolCallStreamParser:
    """
    流式工具调用解析器 v2。

    设计目标：AI 在网页端输出工具选择的格式不可控（DSML 全角/半角混排、
    标签畸形、跨 chunk 截断、缺闭合、JSON 带坏转义/尾逗号/单引号/裸 key），
    本解析器把它们全部解析为 OpenAI tool_calls 结构（IDE 兼容），
    并保证：标记文本永不泄漏为正文、能提取到工具名就绝不静默丢弃。

    支持：
    1. ```tool_call / ```json 围栏 + JSON 体（单个或数组）
    2. 裸 JSON（{"name"|"tool"|"function":{...}} 及"纯参数"按已知工具推断）
    3. XML 风格 <tool_call>…</tool_call>
    4. DSML：| 与 ｜ 任意组合；invoke/parameter 标签允许缺 tagword、
       单双引号、属性乱序、自闭合、缺闭合标签
    5. 流结束 flush()：未闭合块做 best-effort 提取并产出 tool_calls（不再是泄漏）
    6. _parse_json 多级修复（坏转义→尾逗号→裸 key→单引号→ast.literal_eval）
    """

    TRIGGER_RE = re.compile(
        r'(?:`{3}\s*)?(?:tool_call|json)\s*(?:`{3})?\s*', re.IGNORECASE)
    XML_OPEN_RE = re.compile(r'<(?:tool_call|call)\s*>', re.IGNORECASE)
    XML_CLOSE_RE = re.compile(r'</(?:tool_call|call)\s*>', re.IGNORECASE)

    # ===== 深度思考块：网页端模型会把思考过程直接写进正文，必须剥离 =====
    THINK_OPEN_RE = re.compile(
        r'<\s*(?:think|thinking|reason|reasoning|thought)\s*>', re.IGNORECASE)
    THINK_CLOSE_RE = re.compile(
        r'</\s*(?:think|thinking|reason|reasoning|thought)\s*>', re.IGNORECASE)

    DSML_OPEN_RE = re.compile(
        r'<[|｜]{1,8}\s*DSML\s*[|｜]{1,8}\s*calls\s*/?>', re.IGNORECASE)
    DSML_CLOSE_RE = re.compile(
        r'</[|｜]{1,8}\s*DSML\s*[|｜]{1,8}\s*calls\s*>', re.IGNORECASE)
    DSML_INVOKE_RE = re.compile(
        r'<[|｜]{1,8}\s*DSML\s*[|｜]{1,8}\s*invoke\b[^>]*>', re.IGNORECASE)
    DSML_INVOKE_CLOSE_RE = re.compile(
        r'</[|｜]{1,8}\s*DSML\s*[|｜]{1,8}\s*invoke\s*>', re.IGNORECASE)
    DSML_PARAM_RE = re.compile(
        r'<[|｜]{1,8}\s*DSML\s*[|｜]{1,8}\s*parameter\b[^>]*>', re.IGNORECASE)
    DSML_PARAM_CLOSE_RE = re.compile(
        r'</[|｜]{1,8}\s*DSML\s*[|｜]{1,8}\s*parameter\s*>', re.IGNORECASE)

    # ===== v2：通用 DSML 标签扫描（畸形兼容核心）=====
    DSML_ANY_TAG_RE = re.compile(
        r'<(/?)\s*[|｜]{1,8}\s*DSML\s*[|｜]{1,8}\s*([^<>]*?)\s*/?>',
        re.IGNORECASE)
    ATTR_RE = re.compile(
        r'([a-zA-Z_][\w\-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
    BARE_NAME_RE = re.compile(r'^([a-zA-Z_][\w\-]*)')
    TAGWORDS = ("calls", "invoke", "parameter")

    ILLEGAL_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')
    # \u 后跟非 4 位 hex（如 Windows 路径 C:\universalweb）：
    # 合法 unicode 前缀让 ILLEGAL_ESCAPE_RE 跳过，json.loads 又拒绝，必须先修
    BAD_UNICODE_ESCAPE_RE = re.compile(r'\\u(?![0-9a-fA-F]{4})')
    BARE_KEY_RE = re.compile(r'([{,]\s*)([A-Za-z_][\w\-]*)\s*:')
    TRAILING_COMMA_RE = re.compile(r',\s*([}\]])')
    SINGLE_QUOTE_RE = re.compile(r"(?<!\\)'")

    MAX_BUFFER_SIZE = 50000

    # 尾部出现"未闭合的 < 起始片段"（如 '<｜｜DSML｜｜calls'、'1<2'）时整体押后，
    # 等下一个 chunk 把标签闭合后再统一处理，避免开标签残片被当正文冲走
    TAG_OPEN_PREFIX_RE = re.compile(r'<[^<>]*$', re.DOTALL)

    # 独立成行的标签残片（跨 chunk 截断/复制残留），如 "<invoke>"、"calls>"、"</｜｜DSML｜｜ invoke>"
    TAG_DEBRIS_LINE_RE = re.compile(
        r'^[ \t]*(?:<?/?[ \t]*(?:[|｜]{1,8}[ \t]*DSML[ \t]*[|｜]{1,8}[ \t]*)?'
        r'(?:calls|invoke|parameter|call|tool_call|think|thinking|reasoning|thought)[ \t]*>[ \t]*)\r?$',
        re.MULTILINE)

    # 已解析出工具调用后，独立成行的"闭合尾巴"（如 `": 280}}`、`}}`、`残渣}}`）
    # 是标签/JSON 被页面重排後的残渣；仅在此前出现过工具调用时才剥离，避免误伤代码正文
    JSON_TAIL_DEBRIS_LINE_RE = re.compile(
        r'^[ \t]*(?:'
        r'["\']?[ \t]*[:,]?[ \t]*[\d.\-]*[ \t]*[}\]]{1,4}'
        r'|'
        r'[\w\u4e00-\u9fa5 "\',:._\-]{1,24}[}\]]{1,4}'
        r')[,;:]?[ \t]*\r?$',
        re.MULTILINE)

    # ===== v5.4 流头部净化 =====
    # 网页 AI 偶尔回显提示词角色标记（[user]/[assistant]…）或把
    # "已深度思考（用时X秒）"这类 UI 叙述行写在正文最前；
    # 仅对本流首段输出生效，首个正常行出现后立即停用，避免误伤正文。
    _HEAD_JUNK_ALTS = (
        r'\[(?:user|system|assistant|tool)\]'
        r'|\[tool_result[^\]]*\]'
        r'|已深度思考(?:（用时[^）]{0,10}秒）|\(\s*用时[^)]{0,10}s?\))?'
        r'|已联网搜索'
        r'|已搜索到\d{1,5}\s*个?(?:网页|结果|来源|页面)?'
        r'|正在?(?:深度?思考|搜索|检索)[^\n]{0,10}'
        r'|思考(?:过程|中)[:：]?'
        r'|Thought(?:\s+for[^)\n]{0,20}\)?)?[:：]?'
        r'|Thinking[^\n]{0,15}'
        r'|Reasoning[^\n]{0,15}'
    )
    HEAD_JUNK_LINE_RE = re.compile(r'(?:' + _HEAD_JUNK_ALTS + r')', re.IGNORECASE)
    HEAD_JUNK_INLINE_RE = re.compile(
        r'(?:' + _HEAD_JUNK_ALTS + r')[ \t]*', re.IGNORECASE)

    @classmethod
    def _is_head_junk_line(cls, line: str) -> bool:
        s = line.strip()
        if not s:
            return False
        return bool(cls.HEAD_JUNK_LINE_RE.fullmatch(s))

    @classmethod
    def _strip_inline_head(cls, s: str) -> str:
        """无换行长首段的内联剥离：反复吃掉行首的 UI 叙述/角色标记。"""
        while True:
            stripped = s.lstrip()
            m = cls.HEAD_JUNK_INLINE_RE.match(stripped)
            if not m:
                return stripped
            s = stripped[m.end():]

    @classmethod
    def _strip_tag_debris(cls, text: str) -> str:
        """把独立成行的标签残片从正文中剥掉（它们是标签截断残留垃圾）。"""
        if not text or ("<" not in text and ">" not in text):
            return text
        return cls.TAG_DEBRIS_LINE_RE.sub("", text)

    # ---------------- 深度思考剥离 ----------------

    @classmethod
    def _think_tail_hold(cls, s: str) -> int:
        """尾部疑似被截断的 <think / </think 前缀时押后，避免跨 chunk 漏剥。

        用完整模板（含 >）做前缀押后：'</think' 缺 '>' 时正则不匹配，
        若不押后整段会被误吞为思考正文（旧实现 max_check=len-1 漏掉完整前缀）。
        """
        if not s:
            return 0
        return max(cls._longest_suffix_prefix(s, "</think>"),
                   cls._longest_suffix_prefix(s, "<think>"))

    def _strip_think_stream(self, text: str) -> str:
        """增量剥离 <think>/<thinking>/<reasoning> 思考块（深度思考不进正文）。"""
        buf = self._think_carry + text if self._think_carry else text
        self._think_carry = ""
        out: List[str] = []
        pos = 0
        n = len(buf)
        while pos < n:
            if self.in_think:
                m = self.THINK_CLOSE_RE.search(buf, pos)
                if m:
                    pos = m.end()
                    self.in_think = False
                    continue
                hold = self._think_tail_hold(buf[pos:])
                emit_len = (n - pos) - hold
                if emit_len > 0:
                    pos += emit_len          # 思考正文直接丢弃
                if hold:
                    self._think_carry = buf[n - hold:]
                break
            mo = self.THINK_OPEN_RE.search(buf, pos)
            mc = self.THINK_CLOSE_RE.search(buf, pos)
            if mo and (not mc or mo.start() < mc.start()):
                out.append(buf[pos:mo.start()])
                pos = mo.end()
                self.in_think = True
                continue
            if mc:
                out.append(buf[pos:mc.start()])   # 无主闭合标签：静默剥除
                pos = mc.end()
                continue
            break
        rest = buf[pos:]
        if rest:
            hold = self._think_tail_hold(rest)
            if hold:
                self._think_carry = rest[len(rest) - hold:]
                rest = rest[:len(rest) - hold]
            if rest:
                out.append(rest)
        return "".join(out)

    def _filter_plain(self, plain: List[str]) -> List[str]:
        """正文净化：标签残渣 + （已出现工具调用后的）JSON 闭合残行。"""
        out = [self._strip_tag_debris(p) for p in plain]
        if self.saw_tool_call:
            out = [self.JSON_TAIL_DEBRIS_LINE_RE.sub("", p) for p in out]
        return out

    def _clean_head(self, plain_chunks: List[str]) -> List[str]:
        """v5.4 头部净化（仅本流首段生效）。

        网页 AI 有时会把提示词角色标记（[user]/[assistant]/[tool_result…]）
        或"已深度思考（用时X秒）"这类 UI 叙述行回显在正文最前，
        它们属于上游交互残留，不能混进正文。

        策略：首段文本押后到能判定（出现换行 / 累计 80 字符），
        逐行丢弃垃圾行；首个正常行出现后立即停用，避免误伤正文。"""
        if self.head_clean_done:
            return plain_chunks
        out: List[str] = []
        for chunk in plain_chunks:
            if self.head_clean_done:
                out.append(chunk)
                continue
            self._head_buf += chunk
            while not self.head_clean_done:
                nl = self._head_buf.find("\n")
                if nl == -1:
                    if len(self._head_buf) >= 80:
                        # 无换行的长首段：内联剥前缀后放行
                        rest = self._strip_inline_head(self._head_buf)
                        self._head_buf = ""
                        self.head_clean_done = True
                        if rest:
                            out.append(rest)
                    break
                line = self._head_buf[:nl + 1]
                s = line.strip()
                if s and not self._is_head_junk_line(s):
                    # 首个正常行：头部判定结束，剩余内容全部放行
                    rest = self._head_buf
                    self._head_buf = ""
                    self.head_clean_done = True
                    out.append(rest.lstrip())
                    break
                # 空行 / 垃圾行：丢弃，继续判定
                self._head_buf = self._head_buf[nl + 1:]
        return out

    def __init__(self, known_tools: Optional[List[Dict]] = None,
                 clean_head: bool = True):
        self.buffer = ""
        self.in_dsml = False
        self.dsml_payload = ""
        self.in_xml = False
        self.xml_payload = ""
        self.in_think = False      # 当前是否处于 <think> 块内
        self._think_carry = ""     # 跨 chunk 押后的疑似思考标签前缀
        self.saw_tool_call = False # 本流是否已解析出工具调用（残渣过滤开关）
        # v5.4 头部净化状态：跨 chunk 押后待判定的首段文本
        self._head_buf = ""
        self.head_clean_done = not clean_head
        # 工具块 JSON 已消费、收尾 ``` 尚未到达（跨 chunk）：置位后仅吞掉
        # 紧随的孤立围栏，不碰正文里的代码块围栏
        self._expect_closing_fence = False
        self.known_tools = ToolSchemaConformer.normalize_tools(known_tools)

    # ---------------- 流式入口 ----------------

    def feed(self, text: str):
        if not text:
            return [], []

        text = self._strip_think_stream(text)
        if not text:
            return [], []

        self.buffer += text
        plain: List[str] = []
        calls: List[Dict] = []

        while True:
            xml_m = self.XML_OPEN_RE.search(self.buffer)
            dsml_m = self.DSML_OPEN_RE.search(self.buffer)
            dsml_close_m = None if self.in_dsml else self.DSML_CLOSE_RE.search(self.buffer)
            dsml_inv_m = None if (self.in_dsml or dsml_m) else self.DSML_INVOKE_RE.search(self.buffer)
            trig_m = self.TRIGGER_RE.search(self.buffer)

            if self.in_xml:
                cm = self.XML_CLOSE_RE.search(self.buffer)
                if not cm:
                    self.xml_payload += self.buffer
                    self.buffer = ""
                    break
                self.xml_payload += self.buffer[:cm.start()]
                self.buffer = self.buffer[cm.end():]
                parsed = self._parse_xml_payload(self.xml_payload)
                if parsed:
                    calls.extend(parsed)
                else:
                    plain.append("\n" + self.xml_payload + "\n")
                self.xml_payload = ""
                self.in_xml = False
                continue

            if self.in_dsml:
                cm = self.DSML_CLOSE_RE.search(self.buffer)
                if not cm:
                    self.dsml_payload += self.buffer
                    self.buffer = ""
                    break
                self.dsml_payload += self.buffer[:cm.start()]
                self.buffer = self.buffer[cm.end():]
                parsed = self._parse_dsml(self.dsml_payload)
                if parsed:
                    calls.extend(parsed)
                else:
                    plain.append(self.dsml_payload)
                self.dsml_payload = ""
                self.in_dsml = False
                continue

            # 未进入任何块状态：选最早的候选标记
            candidates = []
            if xml_m:
                candidates.append((xml_m.start(), 0, "xml", xml_m))
            if dsml_m:
                candidates.append((dsml_m.start(), 1, "dsml", dsml_m))
            if dsml_close_m:
                candidates.append((dsml_close_m.start(), 2, "dsml_close", dsml_close_m))
            if dsml_inv_m:
                candidates.append((dsml_inv_m.start(), 3, "dsml_inv", dsml_inv_m))
            if trig_m:
                candidates.append((trig_m.start(), 4, "trig", trig_m))

            if not candidates:
                stripped_left = self.buffer.lstrip()
                leading_ws = len(self.buffer) - len(stripped_left)

                # 残留收尾围栏 + 紧跟 JSON：跳过围栏按 JSON 处理
                if stripped_left.startswith("```"):
                    raw_after = stripped_left[3:]
                    after = raw_after.lstrip()
                    if after[:1] in ("{", "["):
                        self.buffer = after
                        continue
                    if self._expect_closing_fence and (
                            not after or raw_after[:1] in ("\n", "\r", " ", "\t")):
                        # 工具块消费后迟到的孤立收尾围栏：静默吞掉，
                        # 避免 ``` 泄漏为正文（正文代码块的围栏不受影响）
                        self._expect_closing_fence = False
                        self.buffer = raw_after
                        continue

                if stripped_left[:1] in ("{", "["):
                    end = self._find_json_end(stripped_left)
                    if end == -1:
                        if len(self.buffer) > self.MAX_BUFFER_SIZE:
                            plain.append(self.buffer)
                            self.buffer = ""
                        break
                    if leading_ws > 0:
                        plain.append(self.buffer[:leading_ws])
                    json_str = stripped_left[:end + 1]
                    self.buffer = stripped_left[end + 1:]
                    parsed = self._parse_json_multi(json_str)
                    if parsed:
                        calls.extend(parsed)
                    else:
                        plain.append(json_str)
                    continue

                safe = len(self.buffer) - self._pending_suffix()
                if safe > 0:
                    emitted = self.buffer[:safe]
                    plain.append(emitted)
                    self.buffer = self.buffer[safe:]
                    if emitted.strip():
                        # 仅真实正文清除"期待收尾围栏"状态；
                        # 纯空白（工具 JSON 与 ``` 之间的换行）保持期待
                        self._expect_closing_fence = False
                break

            candidates.sort(key=lambda c: (c[0], c[1]))
            _, _, first_kind, m = candidates[0]

            if first_kind == "dsml_close":
                # 无主的闭合标签（流被截断后的残片）：静默吞掉，不泄漏
                self.buffer = self.buffer[m.end():]
                continue

            if m.start() > 0:
                plain.append(self.buffer[:m.start()])

            if first_kind == "xml":
                self.buffer = self.buffer[m.end():].lstrip()
                self.in_xml = True
                self.xml_payload = ""
                continue

            if first_kind == "dsml":
                self.buffer = self.buffer[m.end():]
                self.in_dsml = True
                self.dsml_payload = ""
                continue

            if first_kind == "dsml_inv":
                # 无 calls 包装的裸 invoke：把标签本体带入 payload 交给容错解析
                self.dsml_payload = m.group(0)
                self.buffer = self.buffer[m.end():]
                self.in_dsml = True
                continue

            # trig：```tool_call / ```json / 裸关键字
            lt = self.buffer.rfind("<", 0, m.start())
            if lt != -1 and ">" not in self.buffer[lt:m.start()]:
                # trig 匹配落在未闭合标签前缀内（如 "<tool_call" 的 ">" 未到）：
                # 押后整段等待标签闭合，避免 "<" 泄漏为正文、XML 块解析被抢跑
                if lt > 0:
                    plain.append(self.buffer[:lt])
                    self.buffer = self.buffer[lt:]
                break
            self.buffer = self.buffer[m.end():].lstrip()
            if self.buffer[:1] not in ("{", "["):
                if "`" in m.group(0) and not self.buffer:
                    # 围栏标记已到但 JSON 体尚未到达（跨 chunk 分离）：
                    # 押后等下个 chunk 拼齐再统一处理，否则标记会漏进正文
                    # 且工具调用丢失；流结束时 flush 会兑底放行
                    self.buffer = m.group(0)
                    break
                plain.append(m.group(0))
                continue
            end = self._find_json_end(self.buffer)
            if end == -1:
                self.buffer = m.group(0) + self.buffer
                break
            json_str = self.buffer[:end + 1]
            self.buffer = self.buffer[end + 1:]
            parsed = self._parse_json_multi(json_str)
            if parsed:
                calls.extend(parsed)
                # 消耗紧随的收尾围栏，避免残留 ``` 污染后续块
                stripped2 = self.buffer.lstrip()
                if stripped2.startswith("```"):
                    self.buffer = stripped2[3:]
                    self._expect_closing_fence = False
                else:
                    self._expect_closing_fence = True
            elif ("tool_call" in m.group(0).lower()
                  and self._loads_json_repair(json_str) is not None):
                # ```tool_call 围栏内 JSON 可解析但无有效调用（如未知工具被
                # schema 对齐层丢弃）：这是失败的工具调用尝试，静默丢弃。
                # ```json / 裸关键字围栏不在此列，避免误伤答案里的 JSON 示例
                logger.info("[ToolCall] tool_call 围栏内 JSON 无有效工具调用，已静默丢弃")
            else:
                plain.append(m.group(0) + json_str)

        if calls:
            self.saw_tool_call = True
        return self._clean_head(self._filter_plain(plain)), calls

    def flush(self):
        """流结束：未闭合块 best-effort 解析，能出工具调用就不泄漏。"""
        plain: List[str] = []
        calls: List[Dict] = []

        # 深度思考收尾：未闭合思考块 / 尾部标签残片一律丢弃，不进正文
        self.in_think = False
        self._think_carry = ""

        # 工具块后迟到的收尾围栏：流结束时还在缓冲里就静默吞掉
        if self.buffer and self._expect_closing_fence \
                and self.buffer.strip() == "```":
            self.buffer = ""
            self._expect_closing_fence = False

        if self.in_xml:
            payload = self.xml_payload
            self.xml_payload = ""
            self.in_xml = False
            parsed = self._parse_xml_payload(payload)
            if parsed:
                calls.extend(parsed)
            else:
                plain.append("\n" + payload)
        if self.in_dsml:
            payload = self.dsml_payload
            self.dsml_payload = ""
            self.in_dsml = False
            parsed = self._parse_dsml(payload)
            if parsed:
                calls.extend(parsed)
            else:
                plain.append(payload)
        if self.buffer:
            buf = self.buffer
            self.buffer = ""
            stripped = buf.strip()
            xm = self.XML_OPEN_RE.search(stripped)
            if xm:
                # 未闭合 <tool_call> 块：best-effort 提取标签后的 JSON
                head = stripped[:xm.start()]
                if head.strip():
                    plain.append(head)
                parsed = self._parse_xml_payload(stripped[xm.end():])
                if parsed:
                    calls.extend(parsed)
                else:
                    plain.append(buf)
            elif stripped[:1] in ("{", "["):
                parsed = self._parse_json_multi(stripped)
                if parsed:
                    calls.extend(parsed)
                else:
                    plain.append(buf)
            elif self.DSML_ANY_TAG_RE.search(stripped):
                parsed = self._parse_dsml(stripped)
                if parsed:
                    calls.extend(parsed)
                else:
                    plain.append(buf)
            else:
                plain.append(buf)
        if calls:
            self.saw_tool_call = True
        if self._head_buf and not self.head_clean_done:
            # 流结束时头部仍在押后：就地判定，避免首段内容被吞
            pending, self._head_buf = self._head_buf, ""
            self.head_clean_done = True
            s = pending.strip()
            if s and not self._is_head_junk_line(s):
                plain.insert(0, self._strip_inline_head(pending))
        return self._clean_head(self._filter_plain(plain)), calls

    # ---------------- DSML 容错解析 ----------------

    def _parse_dsml(self, block: str) -> List[Dict]:
        return self._parse_dsml_tolerant(block)

    def _parse_dsml_tolerant(self, block: str) -> List[Dict]:
        if not block or not block.strip():
            return []
        tags = list(self.DSML_ANY_TAG_RE.finditer(block))
        if not tags:
            parsed = self._parse_json_multi(block.strip())
            return parsed if parsed else []

        results: List[Dict] = []
        cur_name = None
        cur_args: Dict[str, Any] = {}
        body_parts: List[str] = []
        param_name = None
        param_str = True
        param_val: List[str] = []
        pos = 0

        def finish_param():
            nonlocal param_name, param_val
            if param_name is not None:
                val = "".join(param_val).strip()
                cur_args[param_name] = val if param_str else self._loads_value(val)
            param_name = None
            param_val = []

        def finish_invoke():
            nonlocal cur_name, cur_args
            finish_param()
            if cur_name:
                if not cur_args and body_parts:
                    body = "".join(body_parts).strip()
                    if body.startswith("{"):
                        end = self._find_json_end(body)
                        if end != -1:
                            obj = self._loads_value(body[:end + 1])
                            if isinstance(obj, dict):
                                if "name" in obj or "tool" in obj or "function" in obj:
                                    pass  # 由 _parse_json_multi 场景处理，这里仅参数体
                                cur_args = {
                                    k: v for k, v in obj.items()
                                    if k not in ("name", "tool", "function")
                                }
                results.append({
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": cur_name,
                        "arguments": json.dumps(cur_args or {}, ensure_ascii=False),
                    },
                })
            cur_name = None
            cur_args = {}
            body_parts.clear()

        for m in tags:
            seg = block[pos:m.start()]
            pos = m.end()
            if param_name is not None:
                param_val.append(seg)
            elif cur_name is not None and seg.strip():
                body_parts.append(seg)

            is_close = m.group(1) == "/"
            tail = (m.group(2) or "").strip()
            attrs = {}
            for a in self.ATTR_RE.finditer(tail):
                attrs[a.group(1).lower()] = (
                    a.group(2) if a.group(2) is not None else a.group(3))
            head = self.ATTR_RE.sub("", tail).strip()
            low = head.lower()
            tagword = ""
            for tw in self.TAGWORDS:
                if low.startswith(tw):
                    tagword = tw
                    break

            if tagword == "calls":
                continue  # payload 内的外层 calls 标签：忽略
            if tagword == "invoke":
                if is_close:
                    finish_invoke()
                else:
                    finish_invoke()  # 上一个未闭合的 invoke 就地收口
                    # 剩余头部去掉 tagword 再提名字，否则 `<｜｜DSML｜｜ invoke>`
                    # 会造出名为 "invoke" 的幽灵工具调用（IDE 静默拒绝 -> 没下文）
                    cur_name = (attrs.get("name")
                                or self._bare_name(head[len(tagword):].strip()))
            elif tagword == "parameter":
                if is_close:
                    finish_param()
                else:
                    finish_param()
                    pname = (attrs.get("name")
                             or self._bare_name(head[len(tagword):].strip()))
                    if pname:
                        param_name = pname
                        param_str = str(attrs.get("string", "true")).lower() != "false"
                        if tail.rstrip().endswith("/"):
                            finish_param()
            else:
                # 畸形标签：无 tagword
                if is_close:
                    if param_name is not None:
                        finish_param()
                    elif cur_name is not None:
                        finish_invoke()
                elif param_name is not None:
                    # 参数值里又出现标签：不可能是值，视为参数收口
                    finish_param()
                elif cur_name is not None:
                    # 形如 <｜｜DSML｜｜ file_path" string="true"> 的缺 tagword 参数标签
                    pname = attrs.get("name") or self._bare_name(head)
                    if pname:
                        finish_param()
                        param_name = pname
                        param_str = str(attrs.get("string", "true")).lower() != "false"
                        if tail.rstrip().endswith("/"):
                            finish_param()
                # cur_name 为空时的畸形标签：无法归属，忽略

        # 流/payload 结束：未闭合的参数与 invoke 就地收口（best-effort）
        if param_name is not None:
            param_val.append(block[pos:])
            finish_param()
        elif cur_name is not None and block[pos:].strip():
            body_parts.append(block[pos:])
        if cur_name is not None:
            finish_invoke()

        if results:
            logger.info(f"[ToolCall] DSML 容错解析到 {len(results)} 个调用")
        return [c for c in (self._conform_call(r) for r in results) if c]

    @staticmethod
    def _bare_name(head: str) -> str:
        h = head.strip().strip('"\'')
        m = ToolCallStreamParser.BARE_NAME_RE.match(h)
        return m.group(1) if m else ""

    # ---------------- XML / JSON ----------------

    def _parse_xml_payload(self, payload: str) -> List[Dict]:
        payload = payload.strip()
        if not payload:
            return []

        results: List[Dict] = []
        if payload.startswith("{"):
            end = self._find_json_end(payload)
            if end != -1:
                json_str = payload[:end + 1]
                results.extend(self._parse_json_multi(json_str))
                rest = payload[end + 1:].strip()
                if not rest:
                    return results
                payload = rest

        while payload and payload.startswith("{"):
            end = self._find_json_end(payload)
            if end == -1:
                break
            json_str = payload[:end + 1]
            results.extend(self._parse_json_multi(json_str))
            payload = payload[end + 1:].lstrip()
        return results

    def _parse_json(self, s: str) -> Optional[Dict]:
        calls = self._parse_json_multi(s)
        return calls[0] if calls else None

    def _parse_json_multi(self, s: str) -> List[Dict]:
        obj = self._loads_json_repair(s)
        if obj is None:
            return []
        items = obj if isinstance(obj, list) else [obj]
        calls: List[Dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # 解包 {"tool_calls":[...]} / {"tool_call":{...}} / {"calls":[...]} 包装
            has_ident = any(k in item for k in ("name", "tool", "function"))
            wrapped = None
            if not has_ident:
                for wrap_key in ("tool_calls", "tool_call", "calls", "function_calls"):
                    v = item.get(wrap_key)
                    if isinstance(v, (list, dict)):
                        wrapped = v
                        break
            if wrapped is not None:
                inner_items = wrapped if isinstance(wrapped, list) else [wrapped]
                for sub in inner_items:
                    if isinstance(sub, dict):
                        call = self._coerce_tool_call(sub)
                        if call:
                            calls.append(call)
            else:
                call = self._coerce_tool_call(item)
                if call:
                    calls.append(call)
        return calls

    def _loads_json_repair(self, s: str) -> Any:
        s = s.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
        u_fixed = self.BAD_UNICODE_ESCAPE_RE.sub(r'\\\\u', s)
        candidates = [
            self.ILLEGAL_ESCAPE_RE.sub(r'\\\\', u_fixed),
            u_fixed,
            self.TRAILING_COMMA_RE.sub(r'\1', s),
            self.BARE_KEY_RE.sub(r'\1"\2":', s),
            self.SINGLE_QUOTE_RE.sub('"', s),
        ]
        for cand in candidates:
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                pass
            try:
                import ast
                return ast.literal_eval(cand)
            except (ValueError, SyntaxError):
                continue
        logger.warning(f"[ToolCall] JSON 修复失败: {s[:120]}")
        return None

    def _loads_value(self, v: str) -> Any:
        v = v.strip()
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
        try:
            import ast
            return ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return v

    def _coerce_tool_call(self, obj: Dict) -> Optional[Dict]:
        name = obj.get("name") or obj.get("tool")
        fn = obj.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            name = fn["name"]
            args = fn.get("arguments")
        else:
            args = obj.get("arguments")
        if not name or not isinstance(name, str):
            inferred = self._infer_tool_from_args(obj)
            if not inferred:
                return None
            name, args = inferred
        else:
            if args is None:
                args = obj.get("parameters")
            if args is None:
                args = obj.get("args")
            if args is None:
                args = {}
            if isinstance(args, str) and args.strip():
                parsed = self._loads_value(args)
                args = parsed if isinstance(parsed, (dict, list)) else {"_raw": str(args)}
            if not isinstance(args, (dict, list)):
                args = {"_raw": str(args)}
        return self._conform_call({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        })

    def _conform_call(self, tc: Dict) -> Dict:
        """把已解析出的 tool_call 按 IDE schema 对齐（工具名/参数名/类型）。"""
        try:
            fn = tc.get("function") or {}
            name = fn.get("name")
            if not name:
                return tc
            args = fn.get("arguments")
            if isinstance(args, str) and args.strip():
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_raw": args} if args is not None else {}
            fn_schema = ToolSchemaConformer.match_tool(name, self.known_tools)
            if self.known_tools and fn_schema is None:
                # IDE 声明了工具清单而该名字对不上（如幽灵 "invoke"）：
                # 发过去 IDE 也会静默丢弃（表现为收到答复后没下文），不如中层拦下
                logger.warning(f"[ToolCall] 丢弃未知工具调用: {name!r}（不在 IDE 工具清单内）")
                return None
            if fn_schema is None:
                return tc
            canonical = fn_schema.get("name")
            if canonical and canonical != name:
                logger.info(f"[ToolCall] 工具名对齐: {name} -> {canonical}")
                name = canonical
            args = ToolSchemaConformer.conform_args(fn_schema, args)
            return {
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        except Exception as e:
            logger.warning(f"[ToolCall] schema 对齐失败，保留原样: {e}")
            return tc

    # ---------------- 通用工具 ----------------

    @staticmethod
    def _find_json_end(s: str) -> int:
        """返回首个未闭合 JSON 对象/数组的结束下标；未闭合返回 -1。"""
        if not s or s[0] not in "{[":
            return -1
        depth = 0
        in_str = False
        esc = False
        for i, ch in enumerate(s):
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i
        return -1

    @staticmethod
    def _longest_suffix_prefix(s: str, prefix: str) -> int:
        max_check = min(len(s), len(prefix) - 1)
        for k in range(max_check, 0, -1):
            if s[-k:] == prefix[:k]:
                return k
        return 0

    def _pending_suffix(self) -> int:
        # v2：加入完整标签模板，跨 chunk 截断的标签不再被当正文冲走
        markers = [
            "```tool_call", "```json", "tool_call", "json",
            "<｜｜DSML", "<||DSML",
            "<｜｜DSML｜｜ invoke name=\"", "<｜｜DSML｜｜ invoke",
            "<｜｜DSML｜｜ parameter name=\"", "<｜｜DSML｜｜ parameter",
            "<｜｜DSML｜｜ calls", "<｜｜DSML｜｜ ", "<||DSML|| ",
            "<｜", "<|",
            "<tool_call", "</tool_call",
            "invoke", "parameter", "name", "string",
            "`", "``", "```",
        ]
        best = 0
        for mk in markers:
            best = max(best, self._longest_suffix_prefix(self.buffer, mk))
        m = self.TAG_OPEN_PREFIX_RE.search(self.buffer)
        if m:
            best = max(best, len(self.buffer) - m.start())
        return best

    @staticmethod
    def _norm_key(k: str) -> str:
        return k.lower().replace("_", "").replace("-", "").replace(" ", "")

    def _infer_tool_from_args(self, obj: Dict):
        if not obj or not self.known_tools:
            return None
        obj_norm = {self._norm_key(k): k for k in obj.keys()}
        obj_norm_keys = set(obj_norm.keys())
        candidates: List[tuple] = []
        for t in self.known_tools:
            fn = t.get("function") or {}
            n = fn.get("name")
            if not n:
                continue
            params = fn.get("parameters") or {}
            props = params.get("properties") or {}
            required = params.get("required") or []
            props_norm = {self._norm_key(k): k for k in props.keys()}
            req_norm_keys = set(self._norm_key(k) for k in required)
            if not props:
                continue
            converted_args: Dict[str, Any] = {}
            unmapped = 0
            for norm_key, orig_key in obj_norm.items():
                target_key = props_norm.get(norm_key)
                if target_key is None and norm_key:
                    for pn, pk in props_norm.items():
                        if pn and (pn in norm_key or norm_key in pn):
                            target_key = pk
                            break
                if target_key is None:
                    unmapped += 1
                    continue
                converted_args[target_key] = obj[orig_key]
            if unmapped > 1:
                continue
            conv_norm_keys = set(self._norm_key(k) for k in converted_args.keys())
            if not req_norm_keys.issubset(conv_norm_keys):
                continue
            candidates.append((n, converted_args))
        if len(candidates) == 1:
            name, args = candidates[0]
            logger.info(f"[ToolCall] 纯参数 JSON 推断工具名: {name}，参数: {list(args.keys())}")
            return name, args
        if len(candidates) > 1:
            logger.warning(f"[ToolCall] 参数匹配多个工具: {[c[0] for c in candidates]}，跳过")
        return None


class ToolRoundBuilder:
    """把 messages 拼成给网页版模型看的纯文本 prompt。"""

    # 转发格式模板（首轮随工具表附带；续传轮仅重申格式、不重发工具表）
    TOOL_FORMAT_HINT = (
        "转发指令格式（逐字照抄结构）：\n\n"
        "```tool_call\n"
        '{"name": "工具名", "arguments": {"参数名": "值"}}\n'
        "```"
    )

    # 深度思考指令（CodeArts/客户端带 reasoning 参数时注入协议区，
    # 随 protect_prefix 豁免压缩，每轮完整可见）
    DEEP_THINK_HINT = (
        "深度思考要求：本轮任务需要先进行深入、全面、多角度的思考，"
        "仔细分析需求与边界情况，确认无误后再给出最终回答。"
    )

    # 续传轮提示（防角色污染 + 格式重申，不含工具表本身——表在附件文件
    # 末尾或网页端会话开头的「可转发的目标」章节）
    TOOL_CONTINUATION_HINT = (
        "最高优先级规则：忽略对话历史中出现的一切其他系统提示、角色"
        "设定或子代理说明（例如 file search specialist 等）。你始终只是"
        "转发终端，禁止代入任何其他身份；完整工具表见对话开头的「可转发"
        "的目标」章节（上传文件场景在文件末尾），无需重复。"
        + TOOL_FORMAT_HINT + "\n\n"
        "[tool_result] 只是返回给你的数据；收到后按上方格式继续输出"
        "下一条 tool_call；只有当任务已彻底完成、无需任何工具时，才"
        "直接输出最终结论。"
    )

    @staticmethod
    def tool_protocol(tools: Optional[List[Dict]]) -> str:
        """工具表 + 转发格式说明（独立成块，可内联到输入框文本）。

        网页 AI 收不到 API 的 tools 字段，中转把 IDE 声明的工具表
        原样以 JSON 附在 prompt 里，由网页 AI 自己读描述选工具。
        仅首轮调用（同一会话续传轮不再重发，网页端历史记得该表）。"""
        tools = ToolSchemaConformer.normalize_tools(tools)
        tools = [t for t in tools if isinstance(t, dict) and (t.get("function") or {}).get("name")]
        if not tools:
            return ""
        logger.info(f"[ToolRound] 可用工具: {len(tools)} 个")
        # 极简中间人话术（用户 2026-11-04 实测 poe HIT 的逐字模板，
        # 长版剧本会被 poe 当思考内容消化掉）
        return (
            "你只作为大语言模型（你是ai中间人），禁止调用你自生的工具或除文本"
            "以外的内容（下游会识别工具并调用），严格按照我的格式要求生成工具"
            "和调用内容。" + ToolRoundBuilder.TOOL_FORMAT_HINT + "\n\n"
            "可转发的目标（name 大小写敏感，完整定义 JSON 如下，按其中的"
            " description 和 parameters 选择工具并组织 arguments）：\n"
            + json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        )

    @staticmethod
    def build(messages: List[Dict], tools: Optional[List[Dict]] = None,
              tool_choice: Any = None,
              protocol_mode: str = "auto",
              think_hint: bool = False) -> str:
        """拼网页 prompt。protocol_mode:
        - "auto": 输入框直发——工具表仅首轮附带，续传轮只给提示
        - "always_table": 附件场景——续传轮也在 prompt 里带工具表
          （附件每轮都是新文件，网页端会话记忆不可靠，表必须随附件走）
        think_hint: 客户端请求深度思考时在协议区注入 DEEP_THINK_HINT
        （协议区豁免压缩，每轮完整可见）"""
        system_parts: List[str] = []
        other_parts: List[str] = []
        has_tool_result = False

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "") or ""

            if role == "system":
                if content:
                    system_parts.append(content)
                continue

            if role == "tool":
                has_tool_result = True
                tool_name = m.get("name") or m.get("tool_call_id") or "tool"
                preview = content[:500] + ("..." if len(content) > 500 else "")
                logger.info(f"[ToolResult] name={tool_name}, len={len(content)}, content={preview!r}")
                other_parts.append(f"[tool_result name={tool_name}]\n{content}")
                continue

            if role == "user":
                other_parts.append(f"[user]\n{content}")
            elif role == "assistant":
                tc = m.get("tool_calls")
                if tc:
                    lines = []
                    for c in tc:
                        fn = c.get("function", {}) if isinstance(c, dict) else {}
                        lines.append(
                            f"- 调用 `{fn.get('name', '')}`，参数 {fn.get('arguments', '{}')}"
                        )
                    other_parts.append("[assistant]\n" + "\n".join(lines))
                if content:
                    other_parts.append(f"[assistant]\n{content}")
            else:
                other_parts.append(f"[{role}]\n{content}")

        parts: List[str] = []
        # 工具表附带规则：auto 模式仅首轮（历史中无 assistant 回复）；
        # always_table（附件场景）续传轮也带，防网页端会话记忆丢失后
        # AI 见不到工具表（上游实测复现："没有找到可调用入口"死循环）
        has_history = any((m.get("role") == "assistant") for m in messages)
        # 协议放最前（用户实测 poe HIT 的结构：话术在前，[user] 消息在后）
        if tools and (not has_history or protocol_mode == "always_table"):
            protocol = ToolRoundBuilder.tool_protocol(tools)
        else:
            protocol = ""
        if protocol:
            parts.append(protocol)
        if think_hint:
            parts.append(ToolRoundBuilder.DEEP_THINK_HINT)
        if has_tool_result:
            # 续传轮防角色污染：CodeArts 等上游的对话历史里带子代理系统
            # 提示（"You are a file search specialist..."），网页 AI 会代入
            # 该角色自答而不再输出转发指令，造成 tool_call 死循环。
            parts.append(ToolRoundBuilder.TOOL_CONTINUATION_HINT)
        if system_parts:
            parts.append("\n\n".join(system_parts))
        parts.extend(other_parts)
        prompt = "\n\n".join(parts)

        if has_tool_result:
            prompt += (
                "\n\n上一轮工具调用结果已在上方 [tool_result]。"
                "若任务完成，直接给最终回答；若需继续，重新输出 tool_call。"
            )

        return prompt


class FileUploadHelper:
    @staticmethod
    def _ensure_dir(cfg: dict) -> Path:
        upload_dir = Path(cfg.get("upload_dir", "uploads"))
        upload_dir.mkdir(parents=True, exist_ok=True)
        return upload_dir

    @classmethod
    def prepare_single_file(cls, prompt: str, cfg: dict) -> tuple:
        upload_dir = cls._ensure_dir(cfg)
        ext = cfg.get("file_ext", "txt")
        filename = f"prompt_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}"
        filepath = upload_dir / filename
        filepath.write_text(prompt, encoding="utf-8")
        logger.info(f"[Upload] 单文件已准备: {filepath} ({len(prompt)} 字符)")
        return str(filepath.resolve()), filename

    @classmethod
    def prepare_chunked_files(cls, prompt: str, cfg: dict) -> list:
        upload_dir = cls._ensure_dir(cfg)
        ext = cfg.get("file_ext", "txt")
        chunk_size = int(cfg.get("chunk_size", 50000))
        max_files = int(cfg.get("max_upload_files", 5))

        chunks = [prompt[i:i + chunk_size] for i in range(0, len(prompt), chunk_size)]
        if len(chunks) > max_files:
            logger.warning(f"[Upload] 分块数 {len(chunks)} 超过上限 {max_files}，合并尾部")
            head = chunks[:max_files - 1]
            tail = "\n".join(chunks[max_files - 1:])
            chunks = head + [tail]

        stamp = int(time.time())
        uid = uuid.uuid4().hex[:6]
        total = len(chunks)
        files = []
        for i, chunk in enumerate(chunks):
            name = f"prompt_{stamp}_{uid}_part{i + 1:02d}of{total:02d}.{ext}"
            path = upload_dir / name
            path.write_text(chunk, encoding="utf-8")
            files.append((str(path.resolve()), name))
        logger.info(f"[Upload] 分块文件已准备: {len(files)} 个 (chunk_size={chunk_size})")
        return files


class LongPromptRouter:
    STRATEGY_DIRECT = "direct"
    STRATEGY_FILE = "file"
    STRATEGY_CHUNKED = "chunked"

    @classmethod
    def decide(cls, prompt: str, cfg: dict) -> tuple:
        if not prompt:
            return cls.STRATEGY_DIRECT, None
        if not cfg.get("enabled", True):
            return cls.STRATEGY_DIRECT, None

        max_len = int(cfg.get("max_prompt_length", 2000))
        max_file = int(cfg.get("max_file_size", 5 * 1024 * 1024))

        if len(prompt) <= max_len:
            return cls.STRATEGY_DIRECT, None

        size_bytes = len(prompt.encode("utf-8"))
        if size_bytes <= max_file:
            path, name = FileUploadHelper.prepare_single_file(prompt, cfg)
            return cls.STRATEGY_FILE, {"file_path": path, "filename": name}

        files = FileUploadHelper.prepare_chunked_files(prompt, cfg)
        return cls.STRATEGY_CHUNKED, {"files": files}


@dataclass
class CachedElement:
    element: Any
    selector: str
    cached_at: float
    content_hash: str

    def is_stale(self, max_age: float = None) -> bool:
        max_age = max_age or BrowserConstants.ELEMENT_CACHE_MAX_AGE
        return time.time() - self.cached_at > max_age


class ElementFinder:
    FALLBACK_SELECTORS: Dict[str, List[str]] = {
        "input_box": [
            'tag:textarea',
            'css:textarea',
            'css:textarea[name="message"]',
            'css:textarea[placeholder]',
            'tag:div@@contenteditable=true',
            'css:[contenteditable="true"]',
        ],
        "send_btn": [
            'css:button[type="submit"]',
            'tag:button@@type=submit',
            'css:form button[type="submit"]',
            'css:[role="button"][type="submit"]',
        ],
        "result_container": [
            'css:div[class*="message"]',
            'css:div[class*="response"]',
            'css:div[class*="answer"]',
        ],
    }

    def __init__(self, tab):
        self.tab = tab
        self._cache: Dict[str, CachedElement] = {}

    def _compute_element_hash(self, ele) -> str:
        try:
            identity_parts = []
            for attr in ['id', 'data-testid', 'data-message-id']:
                try:
                    val = ele.attr(attr)
                    if val:
                        identity_parts.append(f"{attr}={val}")
                except Exception:
                    pass
            try:
                rect = ele.rect
                if rect:
                    identity_parts.append(f"pos={rect.get('x', 0)},{rect.get('y', 0)}")
            except Exception:
                pass
            try:
                text_len = len(ele.text or "")
                identity_parts.append(f"len={text_len}")
            except Exception:
                pass
            identity_str = "|".join(identity_parts)
            return hashlib.md5(identity_str.encode()).hexdigest()[:8]
        except Exception:
            return ""

    def _validate_cached_element(self, cached: CachedElement) -> bool:
        if cached.is_stale():
            return False
        ele = cached.element
        try:
            if not (hasattr(ele, 'states') and ele.states.is_displayed):
                return False
            current_hash = self._compute_element_hash(ele)
            if current_hash and cached.content_hash:
                if current_hash != cached.content_hash:
                    return False
            return True
        except Exception:
            return False

    def invalidate_cache(self, selector: str = None):
        if selector:
            self._cache.pop(selector, None)
        else:
            self._cache.clear()

    def find(self, selector: str, timeout: int = None,
             use_cache: bool = False) -> Optional[Any]:
        if not selector:
            return None
        if timeout is None:
            timeout = BrowserConstants.DEFAULT_ELEMENT_TIMEOUT

        if use_cache and selector in self._cache:
            cached = self._cache[selector]
            if self._validate_cached_element(cached):
                return cached.element
            else:
                del self._cache[selector]

        try:
            ele = self._find_with_syntax(selector, timeout)
            if use_cache and ele:
                self._cache[selector] = CachedElement(
                    element=ele,
                    selector=selector,
                    cached_at=time.time(),
                    content_hash=self._compute_element_hash(ele)
                )
            return ele
        except Exception as e:
            if timeout > 1:
                logger.debug(f"元素查找失败 [{selector}]: {e}")
            return None

    def find_all(self, selector: str, timeout: int = None) -> List[Any]:
        if not selector:
            return []
        if timeout is None:
            timeout = BrowserConstants.DEFAULT_ELEMENT_TIMEOUT
        try:
            return self._find_all_with_syntax(selector, timeout)
        except Exception as e:
            logger.debug(f"元素批量查找失败 [{selector}]: {e}")
            return []

    def _find_with_syntax(self, selector: str, timeout: int) -> Optional[Any]:
        if selector.startswith(('tag:', '@', 'xpath:', 'css:')) or '@@' in selector:
            ele = self.tab.ele(selector, timeout=timeout)
        else:
            ele = self.tab.ele(f'css:{selector}', timeout=timeout)
        return ele if ele else None

    def _find_all_with_syntax(self, selector: str, timeout: int) -> List[Any]:
        try:
            if selector.startswith(('tag:', '@', 'xpath:', 'css:')) or '@@' in selector:
                eles = self.tab.eles(selector, timeout=timeout)
            else:
                eles = self.tab.eles(f'css:{selector}', timeout=timeout)
            return list(eles) if eles else []
        except Exception:
            return []

    def find_with_fallback(self, primary_selector: str,
                           target_key: str,
                           timeout: int = None) -> Optional[Any]:
        if primary_selector:
            ele = self.find(primary_selector, timeout)
            if ele:
                return ele

        fallback_list = self.FALLBACK_SELECTORS.get(target_key, [])
        if not fallback_list:
            return None

        logger.debug(f"主选择器失败，尝试回退: {target_key}")
        fallback_timeout = BrowserConstants.FALLBACK_ELEMENT_TIMEOUT
        for fb_selector in fallback_list:
            ele = self.find(fb_selector, fallback_timeout)
            if ele:
                logger.debug(f"回退选择器成功: {fb_selector}")
                return ele
        return None


class StreamContext:
    def __init__(self):
        self.max_seen_text = ""
        self.sent_content_length = 0
        self.baseline_snapshot = None
        self.active_turn_started = False
        self.stable_text_count = 0
        self.last_stable_text = ""
        self.active_turn_baseline_len = 0
        self.sent_text_hash = None
        self.content_version = 0

        self.instant_baseline = None
        self.user_baseline = None

        self.content_ever_changed = False
        self.user_msg_confirmed = False
        self.output_target_anchor = None
        self.output_target_count = 0
        self.pending_new_anchor = None
        self.pending_new_anchor_seen = 0

        self.continue_clicked_count = 0

        # v5.4 深度思考（reasoning）独立通道：
        # 思考区文本与正文分开始计增量；折叠/消失即视为思考结束
        self.think_baseline_len = 0
        self.think_sent_len = 0
        self.think_done = False

    def reset_for_new_target(self):
        self.max_seen_text = ""
        self.sent_content_length = 0
        self.stable_text_count = 0
        self.last_stable_text = ""
        self.active_turn_baseline_len = 0
        self.content_ever_changed = False
        self.think_baseline_len = 0
        self.think_sent_len = 0
        self.think_done = False

    def calculate_diff(self, current_text: str) -> tuple:
        if not current_text:
            return "", False, None
        effective_start = self.active_turn_baseline_len + self.sent_content_length
        if len(current_text) > effective_start:
            diff = current_text[effective_start:]
            return diff, False, None
        if len(current_text) >= self.active_turn_baseline_len:
            current_active_text = current_text[self.active_turn_baseline_len:]
            if len(current_active_text) < self.sent_content_length:
                shrink_amount = self.sent_content_length - len(current_active_text)
                if shrink_amount <= BrowserConstants.STREAM_CONTENT_SHRINK_TOLERANCE:
                    return "", False, None
                else:
                    return "", False, f"内容缩短 {shrink_amount} 字符"
        if self.max_seen_text and len(self.max_seen_text) > effective_start:
            diff = self.max_seen_text[effective_start:]
            return diff, True, "使用历史快照"
        return "", False, None

    def update_after_send(self, diff: str, current_text: str):
        self.sent_content_length += len(diff)
        self.last_stable_text = current_text
        self.stable_text_count = 0
        if len(current_text) > len(self.max_seen_text):
            self.max_seen_text = current_text

    def calc_think_diff(self, think_text: str) -> str:
        """思考区增量：从 think_baseline_len + think_sent_len 之后切片；
        思考区长度回落（网页端折叠思考块/切节点）即视为思考结束。"""
        if self.think_done or not think_text:
            return ""
        effective_start = self.think_baseline_len + self.think_sent_len
        if len(think_text) > effective_start:
            diff = think_text[effective_start:]
            self.think_sent_len += len(diff)
            return diff
        if len(think_text) < effective_start:
            self.think_done = True
        return ""


class GeneratingStatusCache:
    def __init__(self, tab):
        self.tab = tab
        self._last_check_time = 0
        self._last_result = False
        self._check_interval = 0.5
        self._found_selector = None

    def is_generating(self) -> bool:
        now = time.time()
        if now - self._last_check_time < self._check_interval:
            return self._last_result
        self._last_check_time = now

        if self._found_selector:
            try:
                ele = self.tab.ele(self._found_selector, timeout=0.1)
                if ele and ele.states.is_displayed:
                    self._last_result = True
                    return True
            except Exception:
                pass
            self._found_selector = None

        indicator_selectors = [
            'css:button[aria-label*="Stop"]',
            'css:button[aria-label*="stop"]',
            'css:button[aria-label*="停止"]',
            'css:[data-state="streaming"]',
            # z.ai 新 UI 生成中光束动画（shimmer）标记
            'css:[data-beam][data-active]',
            'css:.stop-generating',
        ]
        for selector in indicator_selectors:
            try:
                ele = self.tab.ele(selector, timeout=0.05)
                if ele and ele.states.is_displayed:
                    self._found_selector = selector
                    self._last_result = True
                    return True
            except Exception:
                pass

        self._last_result = False
        return False


class TabManager:
    def __init__(self, page):
        self.page = page
        self._pinned_tab = None
        self._pinned_tab_id = None

    def get_active_tab(self, pin: bool = True):
        if self._pinned_tab is not None:
            try:
                if self._is_tab_valid(self._pinned_tab):
                    return self._pinned_tab
            except Exception:
                pass
            logger.debug("固定的标签页已失效，重新获取")
            self._pinned_tab = None
            self._pinned_tab_id = None

        tab = self.page.latest_tab
        if pin:
            self._pinned_tab = tab
            self._pinned_tab_id = self._get_tab_id(tab)
        return tab

    def release_pinned_tab(self):
        self._pinned_tab = None
        self._pinned_tab_id = None

    def _is_tab_valid(self, tab) -> bool:
        try:
            _ = tab.url
            return True
        except Exception:
            return False

    def _get_tab_id(self, tab) -> str:
        try:
            return f"{tab.url}#{id(tab)}"
        except Exception:
            return str(id(tab))


class WorkflowExecutor:
    def __init__(self, tab, stealth_mode: bool = False,
                 should_stop_checker: Callable[[], bool] = None):
        self.tab = tab
        self.stealth_mode = stealth_mode
        self.finder = ElementFinder(tab)
        self.formatter = SSEFormatter()

        self._should_stop = should_stop_checker or (lambda: False)

        self._stream_ctx: Optional[StreamContext] = None
        self._final_complete_text = ""
        self._generating_checker: Optional[GeneratingStatusCache] = None

        self._active_tools: Optional[List[Dict]] = None
        self._active_tool_choice: Any = None
        self._already_finished: bool = False
        self._role_sent: bool = False

    def _check_cancelled(self) -> bool:
        return self._should_stop()

    def _smart_delay(self, min_sec: float = None, max_sec: float = None):
        if not self.stealth_mode:
            return
        min_sec = min_sec or BrowserConstants.STEALTH_DELAY_MIN
        max_sec = max_sec or BrowserConstants.STEALTH_DELAY_MAX
        total_delay = random.uniform(min_sec, max_sec)
        elapsed = 0
        step = 0.05
        while elapsed < total_delay:
            if self._check_cancelled():
                return
            time.sleep(min(step, total_delay - elapsed))
            elapsed += step

    def _try_click_continue(self) -> bool:
        """
        尝试点击 DeepSeek 的"继续"按钮。
        用户给出的是 <div class="ds-button__background"></div>，通常是 button 的内部装饰。
        优先匹配文本含"继续"/"Continue"的 button；再兜底扫描 ds-button__background。
        """
        if self._check_cancelled():
            return False

        text_xpaths = [
            'xpath://button[contains(normalize-space(.), "继续")]',
            'xpath://button[contains(normalize-space(.), "Continue")]',
            'xpath://button[contains(normalize-space(.), "continue")]',
            'xpath://div[contains(@class, "ds-button")][contains(normalize-space(.), "继续")]',
            'xpath://div[contains(@class, "ds-button")][contains(normalize-space(.), "Continue")]',
        ]
        for xp in text_xpaths:
            try:
                btn = self.tab.ele(xp, timeout=0.05)
                if btn and btn.states.is_displayed:
                    text = (btn.text or "").strip()
                    logger.info(f"[Continue] 检测到继续按钮: text='{text[:20]}'")
                    try:
                        btn.click()
                        return True
                    except Exception as ce:
                        try:
                            self.tab.actions.move_to(btn)
                            time.sleep(0.1)
                            btn.click()
                            return True
                        except Exception as ce2:
                            logger.warning(f"[Continue] 点击失败: {ce} / {ce2}")
                            continue
            except Exception:
                continue

        try:
            bgs = self.tab.eles('css:div.ds-button__background', timeout=0.05)
            if bgs:
                for bg in bgs:
                    try:
                        parent = None
                        for anc_xp in [
                            'xpath:ancestor::button[1]',
                            'xpath:ancestor::div[contains(@class,"ds-button")][1]',
                        ]:
                            try:
                                parent = bg.ele(anc_xp, timeout=0.05)
                                if parent:
                                    break
                            except Exception:
                                continue
                        if parent and parent.states.is_displayed:
                            text = (parent.text or "").strip()
                            if "继续" in text or "continue" in text.lower():
                                logger.info(f"[Continue] 通过 ds-button__background 定位: '{text[:20]}'")
                                try:
                                    parent.click()
                                    return True
                                except Exception as ce:
                                    logger.warning(f"[Continue] 通过背景点击失败: {ce}")
                    except Exception:
                        continue
        except Exception:
            pass

        return False

    def execute_step(self, action: str, selector: str,
                     target_key: str, value: str = None,
                     optional: bool = False,
                     context: Dict = None) -> Generator[str, None, None]:
        if self._check_cancelled():
            logger.debug(f"步骤 {action} 跳过（已取消）")
            return

        logger.debug(f"执行: {action} -> {target_key}")

        try:
            if action == "WAIT":
                wait_time = float(value or 0.5)
                elapsed = 0
                while elapsed < wait_time:
                    if self._check_cancelled():
                        return
                    time.sleep(min(0.1, wait_time - elapsed))
                    elapsed += 0.1

            elif action == "KEY_PRESS":
                self._execute_keypress(target_key or value)

            elif action == "NAVIGATE":
                url = (value or "").strip()
                if url:
                    # 已在目标页时不再整页重载：poe 等站点走代理重载要
                    # 10-20s。Route 阶段可能刚导航完页面还在渲染，此时
                    # 只等元素就绪即可（元素迟迟不出现才走重载恢复）
                    def _wait_ele(wait_s: float) -> bool:
                        if not selector:
                            return True
                        deadline = time.time() + wait_s
                        while time.time() < deadline:
                            if self._check_cancelled():
                                return False
                            try:
                                if _drission_find(self.tab, selector, 1):
                                    return True
                            except Exception:
                                pass
                            time.sleep(0.3)
                        return False

                    _same_page = False
                    try:
                        _cur = (self.tab.url or "").split('?')[0].rstrip('/')
                        _target = url.split('?')[0].rstrip('/')
                        _same_page = bool(_cur) and _cur == _target
                    except Exception:
                        _same_page = False
                    if _same_page and _wait_ele(25):
                        logger.info(f"[Nav] 已在目标页且元素就绪，跳过导航: {url}")
                        return
                    logger.info(f"[Nav] 工作流导航: {url}")
                    self.tab.get(url)
                    time.sleep(1.5)
                    # 等待关键元素就绪（如输入框），poe 等站点首屏慢，
                    # 固定 sleep 会找不到元素直接中断工作流
                    _wait_ele(25)
            elif action == "CLICK":
                self._execute_click(selector, target_key, optional)

            elif action == "FILL_INPUT":
                prompt = context.get("prompt", "") if context else ""
                self._execute_fill(selector, prompt, target_key, optional)

            elif action == "UPLOAD_FILE":
                file_path = context.get("upload_file") if context else None
                upload_btn_sel = (context or {}).get("upload_btn_selector", "")
                wait = float(value or BrowserConstants.get("UPLOAD_WAIT_AFTER") or 1.5)
                self._execute_upload_file(
                    file_input_selector=selector,
                    upload_btn_selector=upload_btn_sel,
                    file_path=file_path,
                    optional=optional,
                    wait_after=wait,
                )

            elif action in ("STREAM_WAIT", "STREAM_OUTPUT"):
                yield from self._stream_monitor(selector)

            else:
                logger.debug(f"未知动作: {action}")

        except ElementNotFoundError as e:
            if not optional:
                yield self.formatter.pack_error(f"元素未找到: {str(e)}")
                raise
        except Exception as e:
            logger.error(f"步骤执行失败 [{action}]: {e}")
            if not optional:
                yield self.formatter.pack_error(f"执行失败: {str(e)}")
                raise

    def _execute_keypress(self, key: str):
        if self._check_cancelled():
            return
        # CDP 可信按键事件：actions 链在部分页面（Vue 受控组件）不生效
        _CDP_KEYS = {
            'Enter': ('Enter', 'Enter', 13, '\r'),
            'Escape': ('Escape', 'Escape', 27, ''),
            'Tab': ('Tab', 'Tab', 9, ''),
        }
        try:
            if key in _CDP_KEYS:
                k, c, vk, txt = _CDP_KEYS[key]
                self.tab.run_cdp('Input.dispatchKeyEvent', type='keyDown',
                                 key=k, code=c, windowsVirtualKeyCode=vk,
                                 text=txt)
                self.tab.run_cdp('Input.dispatchKeyEvent', type='keyUp',
                                 key=k, code=c, windowsVirtualKeyCode=vk)
            else:
                self.tab.actions.key_down(key).key_up(key)
        except Exception:
            self.tab.actions.key_down(key).key_up(key)
        self._smart_delay(0.1, 0.2)

    def _execute_click(self, selector: str, target_key: str, optional: bool):
        if self._check_cancelled():
            return
        # "新聊天"类按钮：chatgpt 等页面存在多个同名不可见节点（收起侧栏
        # 的模板按钮，w=0），DrissionPage 按文档序返回第一个 → 点到隐藏
        # 节点无效且 optional 静默跳过 → 永远停在旧会话。改用 JS 点第一个
        # 可见（宽高>0）的匹配元素。
        if target_key == "new_chat_btn" and selector:
            try:
                sel = selector.strip()
                if sel.lower().startswith(('css:', 'tag:')):
                    sel = sel.split(':', 1)[1]
                js = ("var bs=document.querySelectorAll(" + json.dumps(sel) + ");"
                      "for(var i=0;i<bs.length;i++){"
                      "var r=bs[i].getBoundingClientRect();"
                      "if(r.width>0&&r.height>0){bs[i].click();return true;}}"
                      "return false")
                before_url = ""
                try:
                    before_url = self.tab.url or ""
                except Exception:
                    pass
                if self.tab.run_js(js):
                    self._smart_delay(
                        BrowserConstants.ACTION_DELAY_MIN,
                        BrowserConstants.ACTION_DELAY_MAX)
                    # chatgpt 点新聊天后 SPA 切换页面（URL /c/xxx → /），
                    # 输入框与 file input 全部重渲染；不等切换完成就注入
                    # 附件会打进已脱离 DOM 的旧 input（附件静默丢失）。
                    deadline = time.time() + 5
                    while time.time() < deadline:
                        try:
                            if (self.tab.url or "") != before_url:
                                break
                        except Exception:
                            pass
                        time.sleep(0.2)
                    time.sleep(1.0)  # 切换后留出渲染时间
                    logger.info("[Click] 已点击可见的新聊天按钮（新会话）")
                    return
                logger.warning("[Click] 新聊天按钮无可见实例，回退普通点击")
            except Exception as e:
                logger.warning(f"[Click] JS 点击新聊天失败，回退普通点击: {e}")
        ele = self.finder.find_with_fallback(selector, target_key)
        if target_key == "send_btn":
            # 2026-11-05 poe 事故取证：文档序第一个匹配按钮可能处于
            # disabled/隐藏态（React 尚未启用或模板节点），DP ele.click()
            # 点上去静默无效（无异常）→ 校验误判发送失败。改为 JS 点击
            # 第一个可见且启用的匹配按钮，并记录点击瞬间状态。
            sel_css = _plain_css(selector)
            try:
                _st = self.tab.run_js(
                    "const bs=document.querySelectorAll(%s);"
                    "const out=[];"
                    "for(let i=0;i<bs.length&&i<4;i++){"
                    "const b=bs[i],r=b.getBoundingClientRect();"
                    "out.push({i:i,dis:b.disabled,w:Math.round(r.width),"
                    "h:Math.round(r.height)});}"
                    "return JSON.stringify(out);"
                    % json.dumps(sel_css))
                logger.info(f"[Click] send_btn 候选状态: {_st}")
                _r = self.tab.run_js(
                    "const bs=document.querySelectorAll(%s);"
                    "for(let i=0;i<bs.length;i++){"
                    "const b=bs[i],r=b.getBoundingClientRect();"
                    "if(!b.disabled&&r.width>0&&r.height>0){b.click();"
                    "return 'js-clicked:'+i;}}"
                    "return 'none-enabled';"
                    % json.dumps(sel_css))
                self._smart_delay(
                    BrowserConstants.ACTION_DELAY_MIN,
                    BrowserConstants.ACTION_DELAY_MAX)
                logger.info(f"[Click] send_btn JS 可见启用点击: {_r}")
                if _r and str(_r).startswith("js-clicked"):
                    return
                # 没有可见启用的按钮 → 保留原有元素点击/回车兜底
                logger.warning(f"[Click] send_btn 无可见启用实例（{_r}），"
                               "回退元素点击")
            except Exception as _e:
                logger.warning(f"[Click] send_btn JS 点击异常，回退元素点击: {_e}")
        if ele:
            try:
                if self.stealth_mode:
                    try:
                        self.tab.actions.move_to(ele)
                        self._smart_delay(0.1, 0.25)
                    except Exception:
                        pass
                if self._check_cancelled():
                    return
                ele.click()
                self._smart_delay(
                    BrowserConstants.ACTION_DELAY_MIN,
                    BrowserConstants.ACTION_DELAY_MAX
                )
            except Exception as click_err:
                logger.debug(f"点击异常: {click_err}")
                if target_key == "send_btn":
                    self._execute_keypress("Enter")
        elif target_key == "send_btn":
            self._execute_keypress("Enter")
        elif not optional:
            raise ElementNotFoundError(f"点击目标未找到: {selector}")

    # ---------- 页面可用性守卫 ----------
    # 页面上所有 file input 的"能不能吃下这个文件"事实，交由 Python 侧评分
    _FILE_INPUT_FACTS_JS = """
function visible(e) {
  return !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
}
var act = document.activeElement;
return Array.from(document.querySelectorAll('input[type="file"]')).map(
  function (el, i) {
    var f = el.closest('form');
    var eds = f ? Array.from(f.querySelectorAll(
      '.ProseMirror,[contenteditable="true"],textarea')) : [];
    var visEds = eds.filter(visible);
    return {
      i: i,
      accept: (el.accept || '').toLowerCase().trim(),
      multiple: !!el.multiple,
      in_editor_form: !!f && eds.length > 0,
      in_visible_editor_form: visEds.length > 0,
      // 点"新聊天"后 SPA 会重挂输入区，旧 composer 的上传口还在 DOM 里
      // 但已废弃：注入它不报错、附件却永远不出现。以"当前聚焦的那组"为准。
      in_focused_editor_form: !!(act && f && f.contains(act)
                                 && visEds.indexOf(act) >= 0),
      editor_top: visEds.length ? Math.round(
        visEds[0].getBoundingClientRect().top) : -1,
      hidden: !visible(el)
    };
  });
"""

    # 常见扩展名 → MIME/子类型，用于按 accept 判断可用性
    _UPLOAD_MIME = {
        "txt": ("text/plain",), "md": ("text/plain", "text/markdown"),
        "csv": ("text/csv",), "tsv": ("text/tab-separated-values",),
        "json": ("application/json",), "xml": ("application/xml",),
        "pdf": ("application/pdf",), "doc": ("application/msword",),
        "docx": ("application/vnd.openxmlformats-officedocument."
                 "wordprocessingml.document",),
        "xls": ("application/vnd.ms-excel",),
        "xlsx": ("application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.sheet",),
        "pptx": ("application/vnd.openxmlformats-officedocument."
                 "presentationml.presentation",),
        "zip": ("application/zip", "application/x-zip-compressed"),
        "png": ("image/png",), "jpg": ("image/jpeg",),
        "jpeg": ("image/jpeg",), "gif": ("image/gif",),
        "webp": ("image/webp",), "svg": ("image/svg+xml",),
        "bmp": ("image/bmp",), "mp3": ("audio/mpeg",),
        "wav": ("audio/wav",), "mp4": ("video/mp4",),
    }

    def _page_state(self) -> dict:
        return probe_page_state(self.tab)

    def _guard_page_usable(self, where: str):
        """输入/上传前的硬守卫：错误页与登录墙没有任何成功可能，
        立刻带真实原因失败，别再走"清空重写→分块→回退直输"的无用功。"""
        st = self._page_state()
        if st["state"] in ("error_page", "need_login"):
            logger.error(f"[Page] {where} 中止：{st['detail']}")
            raise PageUnavailableError(st["state"], st["detail"])
        return st

    # 上传失败时收集站点自己的说法（免费版限制、格式拒绝等）与上传口状态，
    # 用来区分"账号没额度"和"注到了别的口"——两者处置完全不同
    _UPLOAD_REJECTION_JS = """
var msgs = [];
var nodes = document.querySelectorAll(
  '[role="alert"],[aria-live="assertive"],[data-testid*="error"],[class*="error" i]');
for (var i = 0; i < nodes.length && msgs.length < 3; i++) {
  var t = (nodes[i].innerText || '').replace(/\\s+/g, ' ').trim();
  if (t && t.length > 3 && t.length < 200) { msgs.push(t); }
}
var fs = document.querySelectorAll('input[type="file"]');
var ports = [];
for (var k = 0; k < fs.length && k < 6; k++) {
  ports.push((fs[k].accept || '空') + (fs[k].disabled ? '(禁用)' : '')
             + (fs[k].isEnabled === false ? '(不可用)' : ''));
}
return {msgs: msgs, ports: ports};
"""

    def _collect_upload_rejection(self) -> str:
        try:
            d = self.tab.run_js(self._UPLOAD_REJECTION_JS)
        except Exception:
            return ""
        if not isinstance(d, dict):
            return ""
        parts = []
        if d.get("msgs"):
            parts.append("站点提示：" + " / ".join(d["msgs"])[:220])
        if d.get("ports"):
            parts.append("上传口状态：" + ", ".join(d["ports"]))
        return ("；" + "；".join(parts)) if parts else ""

    def _pick_upload_input(self, file_path: str):
        """在页面所有 file input 中挑真正能接收该文件的那个。

        兜底"取第一个 input[type=file]"在 ChatGPT 上必然取错：composer 的
        form 内有 4 个隐藏口，前几个 accept=image/*|video/*，只有 accept
        为空的那个吃文档。往图片口注入 .txt 不报错但被站点静默丢弃 →
        "文件已注入"而附件永不出现（2026-11-05 实锤）。"""
        ext = (Path(file_path).suffix or "").lower().lstrip(".")
        try:
            els = self.finder.find_all('input[type="file"]') or []
        except Exception as e:
            logger.debug(f"[Upload] find_all 失败: {e}")
            els = []
        if not els:
            return None
        try:
            facts = self.tab.run_js(self._FILE_INPUT_FACTS_JS) or []
        except Exception as e:
            logger.debug(f"[Upload] accept 探测失败: {e}")
            facts = []
        if not isinstance(facts, list) or len(facts) != len(els):
            facts = []

        def _score(f: dict) -> float:
            acc = str(f.get("accept") or "")
            parts = [p.strip() for p in acc.split(",") if p.strip()]
            if not parts:
                s = 3.0  # 无 accept 限制 = 通用文档通道
            elif "*/*" in parts:
                s = 3.0
            else:
                mimes = self._UPLOAD_MIME.get(ext, ())
                s = -3.0
                for p in parts:
                    if p.startswith(".") and p[1:] == ext:
                        s = max(s, 3.0)
                    elif p.endswith("/*"):
                        major = p.split("/")[0]
                        if any(m.startswith(major + "/") for m in mimes):
                            s = max(s, 2.0)
                    elif mimes and p in mimes:
                        s = max(s, 2.0)
            if f.get("in_focused_editor_form"):
                s += 3.5   # 当前聚焦的 composer —— 唯一能让附件真正上屏的
            elif f.get("in_visible_editor_form"):
                s += 2.0
            elif f.get("in_editor_form"):
                s -= 1.5   # 属于已废弃/隐藏的 composer：注入不报错但附件不出现
            if f.get("multiple"):
                s += 0.5   #  composer 文档口多为 multiple
            return s

        scored = []
        for i, el in enumerate(els):
            f = facts[i] if facts else {}
            scored.append((_score(f), i, f))
        scored.sort(key=lambda x: (-x[0], x[1]))
        best, idx, info = scored[0]
        if best <= 0:
            logger.warning(
                f"[Upload] 页面上 {len(els)} 个 file input 都无法接收 "
                f".{ext}（accept={[str(x[2].get('accept') or '') for x in scored]}）")
            return None
        logger.info(
            f"[Upload] 选用 file input #{idx} "
            f"(accept='{info.get('accept') or ''}', "
            f"聚焦composer={bool(info.get('in_focused_editor_form'))}, "
            f"可见composer={bool(info.get('in_visible_editor_form'))}, "
            f"编辑器top={info.get('editor_top', -1)}) 承载 .{ext}")
        return els[idx]

    def _execute_fill(self, selector: str, text: str,
                      target_key: str, optional: bool,
                      msg_selector: str = ""):
        if self._check_cancelled():
            return
        # 错误页/登录墙下填充不可能成功：带真实原因立即失败，
        # 不要浪费 30s 走"清空重写→分块 execCommand→回退直输"
        self._guard_page_usable("填充")
        # 写入与读回必须落在同一个元素上（多实例 composer，见 __pickEditor）
        try:
            self.tab.run_js(_EDITOR_RESOLVE_INSTALL_JS)
        except Exception as e:
            logger.debug(f"[Fill] 元素解析器安装失败: {e}")
        # 填充期间可能把消息直接发出去（Enter 发送型站点：fill 的
        # ele.input 遇文本换行触发提交，输入框随即被清空）。基线对比
        # 能把这种情况和"写入失败"区分开——后者才需要清空重写。
        # 2026-11-04 poe 实锤：分片头含 \n\n → fill 即发送 → 读回 0 →
        # 误判残留重写 → 幽灵文本 + 后续点击无效 + 校验误判失败。
        self._fill_submitted = False
        _base_msgs = -1
        if msg_selector:
            try:
                _base = self._capture_send_state(selector, msg_selector)
                _base_msgs = int(_base.get("msg_count", -1))
            except Exception:
                _base_msgs = -1
        ele = self.finder.find_with_fallback(selector, target_key)
        if not ele:
            if not optional:
                raise ElementNotFoundError("找不到输入框")
            return

        if self.stealth_mode:
            try:
                self.tab.actions.move_to(ele)
                self._smart_delay(0.1, 0.2)
                ele.click()
                self._smart_delay(0.15, 0.25)
            except Exception:
                pass

        if self._check_cancelled():
            return

        # 窗口前置：后台失焦时部分站点（z.ai 首页）输入/发送状态异常
        try:
            self.tab.set.activate()
        except Exception:
            pass

        try:
            ele.clear()
        except Exception:
            pass

        # 主路径：CDP Input.insertText（与粘贴同路径的 trusted 输入链）。
        # 2026-11-05 poe 实测：ele.input 填长文本（3500+）会直接触发
        # 站点发送（文本完整上屏、输入框清空）→ 读回 0 被误判残留 →
        # 幽灵重写 → 点击失效 + 校验误判。insertText 落框干净：
        # 文本完整、不触发提交、React 正常注册、发送按钮亮起。
        _inserted = False
        try:
            ele.focus()
        except Exception:
            pass
        try:
            self.tab.run_cdp("Input.insertText", text=text)
            _inserted = True
            self._smart_delay(0.3, 0.6)
        except Exception:
            pass
        if not _inserted:
            try:
                ele.input(text)
                logger.debug(f"文本已输入 ({len(text)} 字符)")
            except Exception:
                # contenteditable 站点 fallback：execCommand 走原生输入事件链
                try:
                    self.tab.run_js(
                        "try{const sel=%s;"
                        "let el=__pickEditor(sel);"
                        "if(el){el.focus();"
                        "document.execCommand('selectAll',false,null);"
                        "document.execCommand('delete',false,null);"
                        "document.execCommand('insertText',false,%s);}}catch(e){}"
                        % (json.dumps(_plain_css(selector)), json.dumps(text)))
                except Exception:
                    pass
        self._smart_delay(0.1, 0.2)

        # 内容校验：输入框应恰好等于本次文本（防历史文本叠加）
        try:
            # ProseMirror 把 \n 渲染成段落，textContent 丢换行（28K 文本
            # 读回少 703 = 换行数）→ contenteditable 用 innerText 保留换行
            _chk_js = ("try{"
                       "const sel=%s;"
                       "let el=__pickEditor(sel);"
                       "if(!el)return '';"
                       "return el.isContentEditable ? "
                       "(el.innerText||'') : (el.value||el.textContent||'');"
                       "}catch(e){return '';}")
            # 归一化比较：ProseMirror 段落在 textContent 丢 \n、innerText
            # 又变 \n\n——去全部空白后比对（内容密度不变，丢内容仍能抓到）
            _norm = lambda s: re.sub(r"\s+", "", s or "")
            cur = self.tab.run_js(_chk_js % json.dumps(_plain_css(selector)))
            if isinstance(cur, str) and _norm(cur) != _norm(text):
                # 读回为空但消息数比填充前基线增长 → 文本已随 fill 被发出
                # （Enter 发送型站点遇换行提交），绝不是"残留旧文本"，
                # 重写只会制造幽灵文本 → 双倍粘贴/点击失效
                if msg_selector and not _norm(cur) and _base_msgs >= 0:
                    try:
                        _now = self._capture_send_state(selector, msg_selector)
                        if int(_now.get("msg_count", -1)) > _base_msgs:
                            self._fill_submitted = True
                            logger.info(
                                f"[Fill] 输入框已空且消息数增长"
                                f"（{_base_msgs}→{_now.get('msg_count')}）"
                                "——文本已在填充阶段被发出，跳过重写")
                            return True
                    except Exception:
                        pass
                logger.warning(
                    f"[Fill] 输入框内容不符（{len(cur)} != {len(text)}，"
                    "可能残留旧文本），强制清空重写")
                try:
                    ele.clear()
                except Exception:
                    pass
                # ProseMirror 类编辑器对超长 ele.input()/execCommand 注入
                # 会重置内容（读回 0）；改用 CDP Input.insertText——trusted
                # 输入事件链，编辑器与粘贴同路径，超长文本可用。
                try:
                    self.tab.run_js(
                        "try{const sel=%s;"
                        "let el=__pickEditor(sel);"
                        "if(el){el.focus();"
                        "document.execCommand('selectAll',false,null);"
                        "document.execCommand('delete',false,null);}}catch(e){}"
                        % json.dumps(_plain_css(selector)))
                    self.tab.run_cdp('Input.insertText', text=text)
                    self._smart_delay(0.5, 0.8)
                    try:
                        _after = self.tab.run_js(
                            _chk_js % json.dumps(_plain_css(selector)))
                        logger.info(
                            f"[Fill] CDP insertText 后读回 "
                            f"{len(_after) if isinstance(_after, str) else '?'}"
                            f"/{len(text)} 字符")
                    except Exception:
                        pass
                    # 后台窗口收不到 trusted 键盘输入（读回 0）——
                    # 改分块 execCommand（纯 JS，不依赖窗口 focus）
                    _cur = self.tab.run_js(
                        _chk_js % json.dumps(_plain_css(selector)))
                    if not (isinstance(_cur, str)
                            and _norm(_cur) == _norm(text)):
                        _chunks = [text[i:i + 4000]
                                   for i in range(0, len(text), 4000)]
                        _chunk_js = (
                            "try{const sel=%s;"
                            "let el=__pickEditor(sel);"
                            "if(el){el.focus();"
                            "document.execCommand('selectAll',false,null);"
                            "document.execCommand('delete',false,null);"
                            "let n=0;"
                            "for(const c of %s){"
                            "document.execCommand('insertText',false,c);n++;}"
                            "return 'chunks='+n;}"
                            "return 'no-el';}catch(e){return 'err:'+e.message}"
                            % (json.dumps(_plain_css(selector)),
                               json.dumps(_chunks)))
                        _cr = self.tab.run_js(_chunk_js)
                        logger.info(f"[Fill] 分块 execCommand: {_cr}")
                except Exception:
                    logger.warning("[Fill] CDP insertText 异常，回退 ele.input")
                    ele.input(text)
                # 终验：仍不符则抛错——空输入框点击发送必然"发送未生效"，
                # fail fast 让上游走账号轮转，比卡死强。
                cur2 = self.tab.run_js(_chk_js % json.dumps(_plain_css(selector)))
                if isinstance(cur2, str) and _norm(cur2) != _norm(text):
                    # 复核页面状态：填充过程中被踢到登录墙/错误页时，
                    # "编辑器拒绝注入"会把人带去改选择器，真因却在会话或网络
                    _st = self._page_state()
                    _why = ("" if _st["state"] in ("ok", "unknown")
                            else f"；页面状态：{_st['detail']}")
                    self._fill_verify_failed = (
                        f"输入框写入失败（读回 {len(cur2)} != {len(text)}，"
                        f"编辑器拒绝注入{_why}）")
        except Exception:
            pass
        # 终验在 except Exception: pass 之外执行，异常必须穿透
        fail = getattr(self, "_fill_verify_failed", None)
        if fail:
            self._fill_verify_failed = None
            raise ElementNotFoundError(fail)

    def _execute_upload_file(self, file_input_selector: str,
                             upload_btn_selector: str,
                             file_path: str,
                             optional: bool = False,
                             wait_after: float = 1.5):
        if self._check_cancelled():
            return

        if not file_path or not Path(file_path).exists():
            if optional:
                logger.warning(f"上传文件不存在，跳过: {file_path}")
                return
            raise ElementNotFoundError(f"上传文件不存在: {file_path}")

        # 错误页/登录墙上没有 composer，站点也不会有文档上传口：
        # 此时"注入成功"只是 CDP 写掉了某个隐藏 input，附件永不出现
        self._guard_page_usable("上传")

        # 上传前清理残留：此前发送失败会在输入区堆附件（z.ai 上限 5 个，
        # 超限后发送按钮不生效），先移除旧的附件 chip
        self._clear_stale_attachments()

        file_input = None
        if file_input_selector:
            file_input = self.finder.find(file_input_selector, timeout=2)

        if not file_input and upload_btn_selector:
            try:
                upload_btn = self.finder.find(upload_btn_selector, timeout=2)
                if upload_btn:
                    logger.debug("点击上传按钮触发文件选择器")
                    upload_btn.click()
                    time.sleep(0.4)
                    if file_input_selector:
                        file_input = self.finder.find(file_input_selector, timeout=2)
            except Exception as e:
                logger.debug(f"点击上传按钮失败: {e}")

        if not file_input:
            # 不再"取第一个 input[type=file]"：ChatGPT 的 composer form 里
            # 前几个口 accept=image/*|video/*，取第一个必然把文档注进图片口
            file_input = self._pick_upload_input(file_path)

        if not file_input:
            msg = (f"找不到可接收 .{Path(file_path).suffix.lstrip('.') or '该文件'}"
                   " 的上传口（站点未配置 file_input/upload_btn，"
                   "且页面 input[type=file] 的 accept 均不匹配）")
            if optional:
                logger.warning(f"跳过上传: {msg}")
                return
            raise ElementNotFoundError(msg)

        try:
            # 新会话切换后旧 file input 可能已脱离 DOM（finder 缓存会拿到
            # detached 元素，input() 静默无效 → 附件丢失）。注入前校验挂载。
            try:
                attached = self.tab.run_js(
                    "var i=document.querySelector('input[type=file]');"
                    "return !!(i&&i.isConnected)")
                if not attached:
                    logger.warning("[Upload] file input 未挂载，重新查找")
                    file_input = self._pick_upload_input(file_path)
            except Exception:
                pass
            try:
                file_input.input(file_path)
            except TypeError:
                try:
                    file_input.input(files=[file_path])
                except TypeError:
                    file_input.input(files=file_path)
            logger.info(f"[Upload] 文件已注入: {Path(file_path).name}")
        except Exception as e:
            logger.error(f"[Upload] 注入失败: {e}")
            if optional:
                return
            raise ElementNotFoundError(f"文件上传失败: {e}")

        if wait_after > 0:
            time.sleep(wait_after)
        self._smart_delay(0.3, 0.6)
        # 真实验证：注入调用无异常 ≠ 上传成功。
        # chatgpt 免费版上传额度耗尽时不提供上传入口、隐藏模板 input
        # 接受注入但 React 不接管——必须确认附件 chip 真的出现，
        # 否则抛错触发"回退直接输入"（全文打进输入框），任务不丢上下文。
        fname = Path(file_path).name
        # 记录文件名供回退分支复查：验证假阴性时（chip 实际已渲染）
        # 可凭此确认上传已成功，避免误刷新毁掉附件、误回退直输分片
        self._last_upload_name = fname
        deadline = time.time() + 6.0
        ok = False
        while time.time() < deadline:
            if self._verify_attachment_visible(fname):
                ok = True
                break
            time.sleep(0.6)
        if not ok:
            _st = self._page_state()
            _why = {"error_page": "站点页面已不可达",
                    "need_login": "账号未登录（该 profile 无会话）",
                    "no_editor": "composer 已消失（页面被跳转/重绘）"}.get(
                        _st["state"],
                        "可能额度耗尽/被站点拒绝，或注入到了非文档上传口")
            raise ElementNotFoundError(
                f"附件未出现在页面（上传假阳性：{_why}"
                f"{self._collect_upload_rejection()}）: {fname}")
        logger.info(f"[Upload] 附件已验证可见: {fname}")

    def _verify_attachment_visible(self, file_name: str) -> bool:
        """上传后真实验证附件 chip 是否真的出现在页面上。

        金标准是"输入区容器内、编辑器文本之外"出现文件名（或去扩展名
        前缀）——输入框里的指令文本本身就含文件名，必须排除编辑器节点。
        2026-11-05 附件+分片共存复现实锤：站点 UI 变体并存多个
        ProseMirror（第 0 个是隐藏空实例），querySelector 撞隐藏实例
        → 永远找不到可见编辑器容器里的 chip → 假阴性 → 误回退分片。
        改为遍历所有编辑器实例只认可见的；且不再提前 return——可见
        容器全部未命中时继续走 input.files/body 兜底。
        无 ProseMirror 的站点（poe/z.ai）退而用 file input.files>0 或
        全文文本命中判定（这些站点 input 注入路径真实有效）。"""
        stem = re.sub(r'\.[^.]+$', '', file_name)
        # 注意：DrissionPage run_js 会把脚本包进 function(){...} 执行，
        # 必须用裸 return（2026-11-05 实锤：IIFE 包裹 (function(){...})()
        # 的 return 被内层函数吃掉，外层无 return 恒返回 None →
        # 本方法恒 False → 上传验证从未真正生效 → 附件假阴性回退分片）。
        # 同理 _CHIP_REMOVE_JS/_LOGIN_MODAL_JS 均为裸 return 写法。
        js = ("var name=" + json.dumps(file_name) + ";"
              "var stem=" + json.dumps(stem) + ";"
              "var eds=document.querySelectorAll("
              "'div.ProseMirror[contenteditable=\"true\"]');"
              "for(var k=0;k<eds.length;k++){"
              "var e=eds[k];"
              "if(!(e.offsetWidth||e.offsetHeight))continue;"
              "var box=e.closest('form')||e.parentElement;"
              "if(!box)continue;"
              "var clone=box.cloneNode(true);"
              "var pm=clone.querySelector('div.ProseMirror');"
              "if(pm)pm.remove();"
              "var clean=(clone.innerText||'');"
              "if(clean.indexOf(name)>=0||clean.indexOf(stem)>=0)"
              "return 'chip';}"
              "var inputs=document.querySelectorAll('input[type=file]');"
              "for(var i=0;i<inputs.length;i++){"
              "if(inputs[i].files&&inputs[i].files.length>0)return 'input';}"
              "var t=document.body.innerText||'';"
              "return (t.indexOf(name)>=0||t.indexOf(stem)>=0)?'body':'none';")
        try:
            return (self.tab.run_js(js) or 'none') != 'none'
        except Exception:
            return False

    def _confirm_upload_false_negative(self) -> bool:
        """验证超时后的假阴性复查：页面若已能找到最后注入的附件，
        说明验证误判（chip 渲染慢/在验证容器外/文件名被站点截断）、
        上传实际已成功。信任附件直接继续，避免刷新毁掉已附上的
        附件后回退直输/分片，造成"附件已附带完整却还在分片"。

        再给约 4s 渲染宽限（3 次尝试）；复查失败返回 False，
        调用方走原有"刷新 + 回退直输"逻辑（真失败场景不变）。"""
        fname = getattr(self, "_last_upload_name", "")
        if not fname:
            return False
        for _ in range(3):
            if self._verify_attachment_visible(fname):
                logger.warning(
                    f"[Upload] 验证超时但复查到附件已在页面: {fname}，"
                    "判定为假阴性，按上传成功继续")
                return True
            time.sleep(1.2)
        return False

    _CHIP_REMOVE_JS = (
        "let n = 0;"
        "const seen = new Set();"
        "const inp = document.querySelector('#chat-input');"
        "const region = inp ? (inp.closest('form') || inp.parentElement) : document.body;"
        "region.querySelectorAll('svg path').forEach(p => {"
        "  const d = p.getAttribute('d') || '';"
        "  if (!/M6 18 18 6/.test(d)) return;"
        "  const b = p.closest('button');"
        "  if (!b || seen.has(b)) return;"
        "  seen.add(b);"
        "  try { b.click(); n++; } catch (e) {}"
        "});"
        "return n;")

    def _clear_stale_attachments(self) -> int:
        """清理输入区残留附件 chip（z.ai 上限 5 个，超限发送按钮失效）。

        chip 的移除按钮无 aria-label，是右上角含 X 图标
        （svg path M6 18 18 6）的按钮。注意 chip 外层也是 button
        （点击会打开附件预览），必须只点 X 图标最近的内层 button，
        否则会误点 chip 本体把附件内容点开。"""
        try:
            n = self.tab.run_js(self._CHIP_REMOVE_JS)
            n = int(n or 0)
            if n:
                logger.warning(f"[Attach] 清理输入区残留附件 {n} 项")
                time.sleep(0.8)
            return n
        except Exception:
            return 0

    def _page_busy(self) -> bool:
        """z.ai 页面是否仍在生成（停止按钮可见 = 生成中）。"""
        try:
            return bool(self.tab.run_js(
                "return !!document.querySelector("
                "'[aria-label*=\"停止\"] button,"
                "button[aria-label*=\"停止\"],"
                "button[aria-label*=\"Stop\"],"
                "button[aria-label*=\"stop\"]');"))
        except Exception:
            return False

    def _ensure_page_idle(self, max_wait: float = 30.0):
        """上一个生成还在跑时先点停止再发送。

        OpenClaw 提前 abort 后 z.ai 仍在生成孤儿任务，此时发新消息
        会被页面忽略（发送按钮不生效），必须先停止上一次生成。"""
        if not self._page_busy():
            return
        logger.warning("[Busy] 检测到上一次生成仍在运行，点击停止")
        try:
            self.tab.run_js(
                "const w = document.querySelector('[aria-label*=\"停止\"]');"
                "const b = (w && w.querySelector('button')) || w;"
                "if (b) b.click();"
                "return true;")
        except Exception:
            pass
        deadline = time.time() + max_wait
        while time.time() < deadline:
            time.sleep(0.5)
            if not self._page_busy():
                logger.info("[Busy] 已停止，页面空闲")
                time.sleep(1.0)
                return
        logger.warning(f"[Busy] 等待生成结束超时（{max_wait:.0f}s），继续尝试发送")

    def _capture_send_state(self, input_selector: str,
                            msg_selector: str = "") -> dict:
        """点击发送前采样：输入框文本长度 + 消息节点数。

        附件超限（z.ai 上限 5 个）时发送按钮不生效——输入不清空、
        无新消息节点，若不校验就进流式监听会无限卡死。
        输入框兼容 contenteditable（ProseMirror 等）：无 .value 时读
        textContent；消息计数优先用站点 message_wrapper/result_container，
        兜底 z.ai 的 div[id^="message-"]。"""
        state = {"input_len": -1, "msg_count": -1, "turn_count": -1,
                 "article_count": -1, "body_len": -1, "url": ""}
        try:
            state["url"] = (self.tab.url or "").split('?')[0]
        except Exception:
            pass
        try:
            r = self.tab.run_js(send_verify.capture_send_state_js(
                _plain_css(input_selector), _plain_css(msg_selector)))
            if isinstance(r, str) and r.startswith('{'):
                d = json.loads(r)
                state["input_len"] = int(d.get("i", -1))
                state["msg_count"] = int(d.get("m", -1))
                state["turn_count"] = int(d.get("t", -1))
                state["article_count"] = int(d.get("a", -1))
                state["body_len"] = int(d.get("b", -1))
        except Exception:
            pass
        return state

    # 登录弹窗检测 JS：模态遮罩（如 z.ai 的 _modal-overlay）+ 登录文案。
    # 登出后遮罩带焦点陷阱，吞掉一切真实/合成点击，发送按钮永远 disabled
    _LOGIN_MODAL_JS = (
        "let ov=document.querySelector('[class*=modal-overlay]');"
        "if(!ov) return false;"
        "let t=((ov.parentElement&&ov.parentElement.innerText)||'')+' '+(ov.innerText||'');"
        "return /登录|请登录|扫码登录|sign ?in|log ?in/i.test(t);")

    def _detect_login_modal(self, tab=None) -> bool:
        """检测当前 tab 是否被"需要登录"模态弹窗挡住。"""
        try:
            t = tab if tab is not None else self.tab
            return bool(t.run_js(self._LOGIN_MODAL_JS))
        except Exception:
            return False

    def _verify_and_recover_send(self, executor, selectors: dict,
                                 pre_state: dict):
        """发送后校验消息真的提交了（生成器：失败时 yield 错误块）。

        成功判据（窗口内任一）：输入框被清空 / 消息节点数增加 /
        生成指示器激活 / URL 变化（z.ai 发送后跳转新会话导致页面
        重载，此时输入框暂时消失，必须靠 URL 判定）。
        失败则清理残留并报错退出——发送动作只有一个出口（首次 CLICK），
        绝不做第二次发送尝试（二次发送会造成重复消息，用户已实测），
        绝不进入无限等待。"""
        input_sel = selectors.get("input_box", "") or ""
        msg_sel = (selectors.get("message_wrapper")
                   or selectors.get("result_container") or "")
        pre_url = pre_state.get("url") or ""

        # 登录弹窗检测：站点登出后模态遮罩+焦点陷阱锁死全页，
        # 所有输入/点击失效——重试无意义，必须重新登录
        if self._detect_login_modal():
            logger.error("[Send] 检测到站点登录弹窗（会话已登出），终止发送")
            try:
                _dom = (urlparse(self.tab.url or "").hostname or "").lower()
                _dom = _dom[4:] if _dom.startswith("www.") else _dom
                if _dom and getattr(self, "browser_port", 0):
                    CreditStateStore.mark_logged_out(_dom, self.browser_port)
            except Exception:
                pass
            yield self.formatter.pack_error(
                "站点会话已登出（检测到登录弹窗），消息无法发送。"
                "请在浏览器中重新登录该站点后重试",
                error_type="auth_error", code="site_logged_out")
            raise WorkflowError("站点需要重新登录")

        def _poll(seconds: float) -> bool:
            deadline = time.time() + seconds
            while time.time() < deadline:
                time.sleep(0.5)
                try:
                    cur_url = (self.tab.url or "").split('?')[0]
                    if pre_url and cur_url and cur_url != pre_url:
                        return True
                except Exception:
                    pass
                try:
                    r = self.tab.run_js(
                        send_verify.poll_signal_js(
                            _plain_css(input_sel)))
                    if r == 'generating':
                        return True
                    # 无论输入框是否已清空都对比计数：2026-11-05 chatgpt
                    # 实测——生成端报"Unknown error"时消息已上屏但草稿
                    # 留在输入框，'input_not_empty' 会短路整个轮询，
                    # msg/turn 增长这个金标准永远轮不到
                    now = self._capture_send_state(input_sel, msg_sel)
                    # 金标准：会话轮数/消息数增加，或输入框清空
                    #（基准取 fill 后值——fill 前采样 input_len
                    # 本来就是 0，清空判据永不成立）
                    if send_verify.sent_confirmed(pre_state, now):
                        return True
                except Exception:
                    pass
            return False

        if _poll(15.0):
            logger.info("[Send] 发送校验通过")
            return

        # 诊断：打印前后状态，便于定位是哪个信号失效
        try:
            now = self._capture_send_state(input_sel, msg_sel)
            logger.info(
                f"[Send] 校验失败详情 pre={pre_state} now={now} "
                f"url={(self.tab.url or '').split('?')[0]}")
        except Exception:
            pass

        # 站点生成端错误（如 chatgpt "Unknown error"）：消息已上屏但
        # 草稿留在输入框，页面出现"重试"按钮——自动点站点自己的重试，
        # 生成恢复后由流监听接手（2026-11-05 用户反馈"没有自动重试
        # 导致失败"）。判据：消息计数确实增长（不是发送失败）。
        try:
            _now = self._capture_send_state(input_sel, msg_sel)
            _grew = send_verify.sent_confirmed(pre_state, _now)
        except Exception:
            _grew = False
        if _grew:
            try:
                _rc = self.tab.run_js(
                    "const bs=[...document.querySelectorAll('button')];"
                    "const b=bs.find(x=>{const r=x.getBoundingClientRect();"
                    "return r.width>0&&r.height>0&&"
                    "/^(重试|Retry)$/.test((x.innerText||'').trim());});"
                    "if(b){b.click();return 'clicked';}"
                    "return 'no-retry';")
                if _rc == 'clicked':
                    logger.info(
                        "[Send] 消息已上屏但站点生成报错（Unknown error），"
                        "已自动点击站点重试按钮")
                    time.sleep(2.0)
                    return
                logger.warning(
                    "[Send] 消息已上屏但站点生成报错，页面无重试按钮——"
                    "交由上层重试策略（新会话重放）")
                yield self.formatter.pack_error(
                    "消息已发送但站点生成失败（页面报 Unknown error 且无"
                    "重试按钮），请稍后重试",
                    error_type="send_error", code="site_generation_error")
                raise WorkflowError("站点生成错误且无重试按钮")
            except WorkflowError:
                raise
            except Exception:
                pass

        # 发送动作只有一个出口：首次 CLICK。这里绝不做第二次发送尝试
        # （补点/补 Enter）——校验误判时二次发送会把同样的内容再发一遍
        # （用户实测会话里出现重复分片）。失败就清残留并报错，由上层
        # worker 统一决定重试策略。
        removed = self._clear_stale_attachments()
        # 同步清空输入框残留文本：残留会与下次请求叠加，且站点的
        # React 状态可能已与 DOM 脱钩（发送按钮 disabled）
        try:
            self.tab.run_js(
                "try{let el=document.querySelector("
                "'#chat-input,textarea,[contenteditable=true]');"
                "if(el){el.focus();"
                "document.execCommand('selectAll',false,null);"
                "document.execCommand('delete',false,null);}}catch(e){}")
        except Exception:
            pass
        # 账号状态甄别：匿名会话/页面引导卡死时发送不可能成功，
        # 报 auth_error 精确引导重新登录（而不是笼统"发送未生效"）。
        # 2026-11-04 实测：acc1 登录态丢失 → composer 不受理任何输入，
        # 四信号全灭，此检查把 15s 盲等变成明确结论。
        try:
            acc = send_verify.interpret_account_check(
                self.tab.run_js(send_verify.account_check_js()))
        except Exception:
            acc = {"anon": False, "stuck": False}
        # 掉线标记联动：确认已登录 → 清掉旧的 logged_out 跳过标记；
        # 确认匿名 → 打标记（2h 内不再路由到这个掉线账号）
        try:
            _dom = (urlparse(self.tab.url or "").hostname or "").lower()
            _dom = _dom[4:] if _dom.startswith("www.") else _dom
            if _dom and getattr(self, "browser_port", 0):
                if acc.get("sess"):
                    if CreditStateStore.clear(_dom, self.browser_port):
                        logger.info(
                            f"[Send] 账号已恢复登录，清除 {_dom}:"
                            f"{self.browser_port} 掉线标记")
                elif acc.get("anon"):
                    CreditStateStore.mark_logged_out(_dom, self.browser_port)
        except Exception:
            pass
        if acc.get("anon") or acc.get("stuck"):
            why = []
            if acc.get("anon"):
                why.append("页面已是匿名会话（登录态丢失）")
            if acc.get("stuck"):
                why.append("页面卡在加载个人资料（初始化未完成）")
            logger.error(
                f"[Send] 账号状态异常：{'；'.join(why)}，终止发送")
            yield self.formatter.pack_error(
                "该站点页面账号状态异常（" + "，".join(why) + "），"
                "消息无法发送。请在浏览器中重新登录该站点后重试",
                error_type="auth_error", code="site_anonymous")
            raise WorkflowError("站点页面未登录或初始化卡死")
        logger.error(
            f"[Send] 发送失败：疑似附件超限或页面异常，"
            f"已清理残留 {removed} 项，终止本次请求")
        yield self.formatter.pack_error(
            "消息未发送成功：页面附件可能超过上限或输入区状态异常，"
            "已在浏览器清理残留附件，请重试",
            error_type="send_error", code="send_not_effective")
        raise WorkflowError("发送未生效")

    def _read_visible_text(self, ele) -> tuple:
        """读取消息节点可见文本。

        v5.4：返回 (main, think) 二元组——
        - main：正文文本（跳过 script/style/svg 及"思考"容器子树）
        - think：思考容器（class/id 含 think/thought/reason 的子树）内文本，
          交给 reasoning_content 独立通道，不再混入正文
        """
        if not ele:
            return "", ""

        js = """
            function extract(element) {
                const acc = { m: '', t: '' };
                const SKIP = { script: 1, style: 1, noscript: 1, template: 1,
                               iframe: 1, svg: 1 };
                const BLOCK = { div: 1, p: 1, h1: 1, h2: 1, h3: 1, h4: 1, h5: 1,
                                h6: 1, li: 1, tr: 1, blockquote: 1, pre: 1,
                                section: 1, article: 1, ul: 1, ol: 1, table: 1 };
                const THINK_RE = /think|thought|reason/i;
                function processNode(node, isRoot, inThink) {
                    if (node.nodeType === 3) {
                        if (inThink) { acc.t += node.nodeValue; }
                        else { acc.m += node.nodeValue; }
                        return;
                    }
                    if (node.nodeType !== 1) { return; }
                    const tag = node.tagName.toLowerCase();
                    if (tag === 'br') {
                        if (inThink) { acc.t += '\\n'; } else { acc.m += '\\n'; }
                        return;
                    }
                    if (SKIP[tag]) { return; }
                    // 纯时间戳叶子（poe 消息底部的 0:38 等）：跳过，
                    // 正常回复不会是纯 H:MM 文本
                    if (!isRoot && node.children && node.children.length === 0) {
                        const tt = (node.textContent || '').trim();
                        if (/^\\d{1,2}:\\d{2}$/.test(tt)) { return; }
                    }
                    let style = null;
                    try { style = window.getComputedStyle(node); } catch (e) {}
                    if (style && (style.display === 'none' ||
                                  style.visibility === 'hidden')) { return; }
                    let cls = '';
                    try {
                        cls = (node.getAttribute('class') || '') + ' ' +
                              (node.id || '');
                    } catch (e) {}
                    const myThink = inThink || (!isRoot && THINK_RE.test(cls));
                    const isBlock = BLOCK[tag] === 1 ||
                                    (style && style.display === 'block');
                    const target = myThink ? 't' : 'm';
                    if (isBlock && acc[target] &&
                        acc[target].slice(-1) !== '\\n') {
                        acc[target] += '\\n';
                    }
                    const kids = node.childNodes;
                    for (let i = 0; i < kids.length; i++) {
                        processNode(kids[i], false, myThink);
                    }
                    if (isBlock && acc[target] &&
                        acc[target].slice(-1) !== '\\n') {
                        acc[target] += '\\n';
                    }
                }
                processNode(element, true, false);
                return JSON.stringify(acc);
            }
            function expandCollapsedThink(root) {
                try {
                    const arrows = root.querySelectorAll('svg[class*="-rotate-90"]');
                    for (let i = 0; i < arrows.length; i++) {
                        let anc = arrows[i].parentElement;
                        while (anc) {
                            if (anc.tagName === 'BUTTON'
                                || anc.getAttribute('role') === 'button'
                                || /cursor-pointer/.test(String(anc.className || ''))) {
                                try { anc.click(); } catch (e) {}
                                break;
                            }
                            if (anc === root) break;
                            anc = anc.parentElement;
                        }
                    }
                } catch (e) {}
            }
            expandCollapsedThink(this);
            // 工具调用 trace chip（z.ai：探索 N 次文件/读取文件/文本搜索）
            // 插在段落之间且计数会刷新，混入正文会造成重复交叠文本。
            // 在克隆上移除后再抽取，不改动真实页面。
            let __root = this;
            try {
                __root = this.cloneNode(true);
                __root.querySelectorAll('.toolCallTrace, [class*="toolCallTrace"]')
                    .forEach(function (n) { n.remove(); });
            } catch (e) {}
            return extract(__root);
        """
        try:
            raw = ele.run_js(js)
            data = None
            if isinstance(raw, dict):
                data = raw
            elif isinstance(raw, str) and raw.strip().startswith("{"):
                try:
                    data = json.loads(raw)
                except Exception:
                    data = None
            if isinstance(data, dict) and "m" in data:
                main = re.sub(r'\n{3,}', '\n\n', str(data.get('m') or '')).strip()
                think = re.sub(r'\n{3,}', '\n\n', str(data.get('t') or '')).strip()
                return main, think
        except Exception:
            pass

        try:
            if hasattr(ele, 'text') and ele.text:
                return str(ele.text), ""
        except Exception:
            pass
        return "", ""

    def _get_message_anchor(self, element) -> str:
        if not element:
            return ""
        try:
            stable_attrs = ['data-message-id', 'data-turn-id', 'data-testid', 'id']
            for attr in stable_attrs:
                try:
                    val = element.attr(attr)
                    if val:
                        return f"{attr}={val}"
                except Exception:
                    pass
            try:
                tag = element.tag if hasattr(element, 'tag') else 'unknown'
                cls = element.attr('class') or ''
                classes = cls.split()[:2]
                if classes:
                    return f"tag:{tag}|class:{'.'.join(classes)}"
                return f"tag:{tag}"
            except Exception:
                pass
            return ""
        except Exception:
            return ""

    def _expand_collapsed_thinks(self) -> None:
        """页面级展开折叠的思考块（z.ai 折叠时思考内容不渲染）。

        每次抓取快照前调用；展开后箭头 class 变化，天然幂等。
        与 last_ele 无关：AI 消息容器尚未成为抓取目标时（正在思考）
        也能扫到思考块。"""
        try:
            self.tab.run_js("""
                document.querySelectorAll('svg[class*="-rotate-90"]').forEach(svg => {
                    let anc = svg.parentElement;
                    while (anc) {
                        if (anc.tagName === 'BUTTON'
                            || anc.getAttribute('role') === 'button'
                            || /cursor-pointer/.test(String(anc.className || ''))) {
                            try { anc.click(); } catch (e) {}
                            break;
                        }
                        anc = anc.parentElement;
                    }
                });
            """)
        except Exception:
            pass

    def _fast_ai_pre_arrived(self, snapshot: Dict[str, Any]) -> bool:
        """监听启动前 AI 回复是否已上屏（AI 秒回场景）。

        需同时满足：发送前计数已知且已被超过；基线最后一条消息文本
        不含本次 payload 开头（说明最后上屏的不是我们的消息）。
        poe 长消息折叠后也保留头部，比头部即可。"""
        _pre = getattr(self, '_pre_stream_count', -1)
        if _pre < 0 or not snapshot:
            return False
        if snapshot.get('groups_count', -1) <= _pre:
            return False
        _payload = (getattr(self, '_last_send_payload', '') or '')
        if not _payload:
            return False
        _head = re.sub(r"\s+", "", _payload)[:40]
        if not _head:
            return False
        _last_txt = re.sub(r"\s+", "", snapshot.get('text') or '')
        return _head not in _last_txt

    def _capture_pre_stream_count(self, sel: str = "") -> None:
        """发送前用流监听同款选择器（result_container）采基线计数。

        AI 秒回检测（_fast_ai_pre_arrived）的判据之一。"""
        try:
            self._pre_stream_count = (len(self.finder.find_all(sel))
                                      if sel else -1)
        except Exception:
            self._pre_stream_count = -1

    def _get_latest_message_snapshot(self, selector: str) -> dict:
        result = {
            'groups_count': 0, 'anchor': None, 'text': '',
            'text_len': 0, 'is_generating': False,
            'think_text': '', 'think_len': 0,
        }
        try:
            self._expand_collapsed_thinks()
            eles = self.finder.find_all(selector, timeout=0.5)
            if not eles:
                return result
            result['groups_count'] = len(eles)
            last_ele = eles[-1]
            main_text, think_text = self._read_visible_text(last_ele)
            result['text'] = main_text
            result['text_len'] = len(main_text)
            result['think_text'] = think_text
            result['think_len'] = len(think_text)
            result['anchor'] = self._get_message_anchor(last_ele)
            if self._generating_checker is None:
                self._generating_checker = GeneratingStatusCache(self.tab)
            result['is_generating'] = self._generating_checker.is_generating()
        except Exception as e:
            logger.debug(f"Snapshot 异常: {e}")
        return result

    def _get_active_turn_text(self, selector: str) -> str:
        try:
            eles = self.finder.find_all(selector, timeout=1)
            if not eles:
                return ""
            for i in range(len(eles) - 1, -1, -1):
                ele = eles[i]
                text, _ = self._read_visible_text(ele)
                if text and text.strip():
                    return text.strip()
            return ""
        except Exception:
            return ""

    def _stream_monitor(self, selector: str) -> Generator[str, None, None]:
        logger.info("========== 流式监听启动 (v4) ==========")

        ctx = StreamContext()
        self._stream_ctx = ctx
        self._generating_checker = GeneratingStatusCache(self.tab)
        # 发送前 URL（工作流发送时记录）：poe 等站点发送后跳转会话 URL，
        # AI Start 增长判定失效时用它识别"回复已生成"
        ctx.pre_send_url = getattr(self, '_sent_from_url', '') or ''

        logger.debug("[阶段0] 抓取 instant baseline")
        ctx.instant_baseline = self._get_latest_message_snapshot(selector)
        logger.info(f"[Instant] count={ctx.instant_baseline['groups_count']}, "
                    f"text_len={ctx.instant_baseline['text_len']}, "
                    f"generating={ctx.instant_baseline['is_generating']}")

        logger.debug(f"[阶段1] 等待用户消息上屏（最多 8s，含页面重载等待）")
        user_msg_wait_start = time.time()
        user_msg_wait_max = 8.0
        ctx.user_baseline = None
        # 发送瞬间见过生成指示器 → 指示器可靠，产出哨兵可用短阈值。
        # Page Reload 重置 instant_baseline 会丢掉该信息，单独记录。
        ctx._ever_generating = bool(
            (ctx.instant_baseline or {}).get('is_generating'))

        while time.time() - user_msg_wait_start < user_msg_wait_max:
            if self._check_cancelled():
                logger.info("等待用户消息时被取消")
                return

            current_snapshot = self._get_latest_message_snapshot(selector)
            current_count = current_snapshot['groups_count']
            instant_count = ctx.instant_baseline['groups_count']

            # [Fast AI Pre] 监听启动前 AI 回复已上屏：poe 等站点 AI 秒回
            # （实测 ~2s），而发送校验 + 拒收检查耗时更长——监听采基线时
            # 完整回复已渲染完毕，计数不会再增长，按"增长"判定会白等
            # 180s 误判超时。判据：发送前计数已知(_pre_stream_count)且
            # 当前已超过它，且基线最后一条消息不含本次 payload 开头
            # （不是我们的消息，poe 长消息折叠也保留头部）→ 回复已上屏。
            if not ctx.user_msg_confirmed and self._fast_ai_pre_arrived(
                    current_snapshot):
                logger.info("[Fast AI Pre] 监听启动前 AI 回复已上屏，"
                            "直接进入活跃 turn 流式")
                ctx.user_baseline = current_snapshot
                ctx.user_msg_confirmed = True
                ctx.active_turn_started = True
                ctx.active_turn_baseline_len = 0
                ctx.think_baseline_len = 0
                break

            if current_count < instant_count:
                logger.info(
                    f"[Page Reload] 节点数下降({instant_count} -> {current_count})，"
                    "页面重载中（新会话跳转），重置 baseline 继续等待")
                if current_snapshot.get('is_generating'):
                    ctx._ever_generating = True
                ctx.instant_baseline = current_snapshot
                time.sleep(0.3)
                continue
            if current_count == instant_count + 1:
                logger.info(f"[User Msg] 检测到用户消息上屏 (count: {instant_count} -> {current_count})")
                ctx.user_baseline = current_snapshot
                ctx.user_msg_confirmed = True
                break
            elif current_count >= instant_count + 2:
                logger.info(f"[Fast AI] 检测到 AI 秒回！(count: {instant_count} -> {current_count})")
                ctx.user_baseline = current_snapshot
                ctx.user_msg_confirmed = True
                ctx.active_turn_started = True
                ctx.active_turn_baseline_len = 0
                ctx.think_baseline_len = 0
                break
            elif current_count == instant_count:
                baseline_len = ctx.instant_baseline['text_len']
                growth = current_snapshot['text_len'] - baseline_len
                if baseline_len < 20:
                    logger.debug(f"[Same Node] baseline 过短({baseline_len})，跳过")
                    time.sleep(0.2)
                    continue
                if growth < 50:
                    time.sleep(0.2)
                    continue
                time.sleep(0.6)
                recheck = self._get_latest_message_snapshot(selector)
                recheck_growth = recheck['text_len'] - current_snapshot['text_len']
                still_alive = (
                    recheck['text_len'] > current_snapshot['text_len']
                    or recheck['is_generating']
                )
                if not still_alive:
                    logger.info(
                        f"[Same Node] 疑似上屏抖动(baseline={baseline_len}, "
                        f"growth={growth}, recheck+{recheck_growth})，继续等待"
                    )
                    time.sleep(0.2)
                    continue
                logger.info(
                    f"[Same Node] 确认 AI 回复 (baseline={baseline_len}, "
                    f"growth={growth}, recheck+{recheck_growth})"
                )
                ctx.user_baseline = recheck
                ctx.user_msg_confirmed = True
                ctx.active_turn_started = True
                ctx.active_turn_baseline_len = baseline_len
                ctx.think_baseline_len = ctx.instant_baseline.get('think_len', 0) or 0
                break
            time.sleep(0.2)

        if ctx.user_baseline is None:
            logger.warning("[Timeout] 未检测到用户消息上屏，使用 instant baseline")
            ctx.user_baseline = ctx.instant_baseline

        if not ctx.active_turn_started:
            logger.debug("[阶段2] 等待 AI 开始回复")
            baseline = ctx.user_baseline
            start_time = time.time()
            _last_dbg = 0.0
            # 产出哨兵：全程零产出且无生成指示器持续超阈值 → 本轮生成
            # 失败（chatgpt 收到消息后静默卡死/报错形态），提前判死交给
            # 上层换账号重试，不再死等 STREAM_INITIAL_WAIT（180s）。
            # 见过生成指示器（说明指示器可靠）用短阈值，没见过用长阈值。
            ctx._saw_generating = bool(
                getattr(ctx, '_ever_generating', False)
                or (baseline or {}).get('is_generating'))
            ctx._dead_since = None
            while True:
                if self._check_cancelled():
                    logger.info("等待AI开始时被取消")
                    return
                elapsed = time.time() - start_time
                current = self._get_latest_message_snapshot(selector)
                if time.time() - _last_dbg >= 10:
                    _last_dbg = time.time()
                    try:
                        _u = (self.tab.url or '')
                    except Exception:
                        _u = '<url err>'
                    logger.info(
                        f"[Dbg] 等待AI开始 elapsed={elapsed:.0f}s "
                        f"count={current['groups_count']} "
                        f"text_len={current['text_len']} "
                        f"gen={current['is_generating']} "
                        f"anchor={current.get('anchor')!r} "
                        f"url=...{_u[-45:]}")
                if not getattr(self, "_role_sent", False) \
                        and (current.get('think_len') or 0) \
                        > (ctx.think_baseline_len or 0):
                    self._role_sent = True
                    yield self.formatter.pack_role_chunk()
                # 积分耗尽兜底检测：poe 积分耗尽时弹订阅墙，消息容器
                # text_len 恒为 0，常规内容流抓不到耗尽文案 → 改对页面
                # 全文跑耗尽正则（每 5s 一次），命中即下发耗尽文案并
                # 正常结束流，main 层会标记该账号并自动切账号/站点
                _credit_now = time.time()
                if (_credit_now - getattr(self, '_credit_last_check', 0) >= 5
                        and not (current.get('text_len') or 0)):
                    self._credit_last_check = _credit_now
                    try:
                        _page_text = (self.tab.ele('tag:body').text or '')
                    except Exception:
                        _page_text = ''
                    _m = CREDIT_EXHAUSTED_RE.search(_page_text)
                    if _m:
                        logger.warning(
                            "[Credit] 等待AI开始阶段页面全文命中积分耗尽文案"
                            "（消息容器零输出，订阅墙场景）")
                        if not getattr(self, "_role_sent", False):
                            self._role_sent = True
                            yield self.formatter.pack_role_chunk()
                        _line = ''
                        for _ln in _page_text.split('\n'):
                            if CREDIT_EXHAUSTED_RE.search(_ln):
                                _line = _ln.strip()
                                break
                        # 去掉 poe 行内 "Assistant" 前缀（"Assistant需要更多积分…"）
                        _line = re.sub(r'^Assistant\s*', '', _line)
                        yield self.formatter.pack_chunk(_line or _m.group(0))
                        yield self.formatter.pack_finish()
                        return
                    # 消息过长拒收（chatgpt："你提交的消息过长，请编辑后
                    # 重新发送。"）：发送校验只看输入框/发送键，会被假阳性
                    # 通过，实际消息没发出去，死等只会卡到超时。快速失败
                    # 交给上层处理。
                    if re.search(r'提交的消息过长|消息过长',
                                 _page_text[:20000]):
                        logger.warning(
                            "[TooLong] 页面命中消息过长拒收文案（直输超限）")
                        raise TooLongError(
                            "网页端拒收：提交的消息过长（长文本直输超限，"
                            "且上传通道不可用），请缩短输入或等上传额度恢复")
                if (current.get('think_len') or 0) \
                        > (ctx.think_baseline_len or 0):
                    think_now = current.get('think_text') or ''
                    sent = ctx.think_baseline_len or 0
                    yield self.formatter.pack_reasoning_chunk(
                        think_now[sent:])
                    ctx.think_baseline_len = len(think_now)
                    if not getattr(ctx, '_think_start_logged', False):
                        ctx._think_start_logged = True
                        logger.info(
                            f"[Think] 思考开始（AI Start 等待期间），"
                            f"累计下发 {len(think_now)} 字符")
                # 会话跳转检测（poe: /bot -> /chat/x）：跳转后是全新会话页，
                # baseline 来自旧页面/生成占位符（如 poe 的 "..."），
                # 与新页面内容不可比——任何变化甚至长度缩短都说明回复已产出
                _jumped = False
                sent_url = getattr(ctx, 'pre_send_url', '') or ''
                if sent_url:
                    try:
                        cur_url = self.tab.url or ''
                    except Exception:
                        cur_url = ''
                    _jumped = bool(cur_url) and cur_url != sent_url
                is_started, reason = self._detect_ai_start(baseline, current)
                if not is_started and _jumped:
                    _b_len = baseline.get('text_len', 0) or 0
                    _c_len = current.get('text_len') or 0
                    if (_c_len != _b_len
                            or current['groups_count']
                            != baseline['groups_count']):
                        is_started = True
                        reason = f'会话跳转+内容变化({_b_len}->{_c_len})'
                    elif (baseline.get('is_generating')
                            and not current.get('is_generating')
                            and _c_len > 0):
                        is_started = True
                        reason = '会话跳转+生成结束'
                if is_started:
                    logger.info(f"[AI Start] 检测到 AI 开始回复：{reason}")
                    ctx.active_turn_started = True
                    if _jumped or current['groups_count'] > baseline['groups_count']:
                        # 会话页是新渲染的，最后一行就是本轮回复，
                        # baseline 不再代表"旧内容"，归零以完整输出全文
                        ctx.active_turn_baseline_len = 0
                        ctx.think_baseline_len = min(
                            ctx.think_baseline_len or 0,
                            current.get('think_len') or 0)
                    else:
                        ctx.active_turn_baseline_len = baseline.get('text_len', 0)
                        ctx.think_baseline_len = max(
                            baseline.get('think_len', 0) or 0,
                            ctx.think_baseline_len or 0)
                    break
                # ===== 产出哨兵 =====
                if current.get('is_generating'):
                    ctx._saw_generating = True
                    ctx._dead_since = None
                else:
                    _no_growth = (
                        (current.get('text_len') or 0)
                        <= (baseline.get('text_len') or 0)
                        and (current.get('think_len') or 0)
                        <= (ctx.think_baseline_len or 0)
                        and current['groups_count']
                        <= baseline['groups_count'])
                    if _no_growth:
                        if ctx._dead_since is None:
                            ctx._dead_since = time.time()
                        _dead_secs = time.time() - ctx._dead_since
                        _dead_limit = (30 if ctx._saw_generating else 120)
                        # 回复异常分类：错误面板点重试 / 无回复重发一次
                        # / 重发过仍死 → 交 worker 换其他 chrome 实例
                        _act = self._dead_recovery_action(
                            _dead_secs, _dead_limit,
                            self._detect_retry_button(),
                            getattr(ctx, '_resent_once', False),
                            bool(getattr(self, '_last_send_payload', '')))
                        if _act is None:
                            pass
                        elif _act == 'click_retry':
                            if self._click_page_retry_button():
                                logger.warning(
                                    "[Dead] 页面错误面板，已点击重试按钮，"
                                    "重置等待计时")
                                ctx._dead_since = None
                                time.sleep(2.0)
                        elif _act == 'resend':
                            ctx._resent_once = True
                            if self._resend_last_message():
                                ctx._dead_since = None
                                time.sleep(1.5)
                        else:
                            logger.warning(
                                f"[Dead] 零产出且无生成指示器持续 "
                                f"{_dead_secs:.0f}s"
                                f"（见过指示器={ctx._saw_generating}，"
                                f"重发过={getattr(ctx, '_resent_once', False)}），"
                                "判定本轮生成失败，提前结束等待")
                            try:
                                _pt = (self.tab.ele('tag:body').text
                                       or '').replace('\n', ' ')[:300]
                                logger.warning(
                                    f"[Dead] 页面文本快照: {_pt!r}")
                            except Exception:
                                pass
                            break
                    else:
                        ctx._dead_since = None
                if elapsed > BrowserConstants.STREAM_INITIAL_WAIT:
                    logger.warning(f"[Timeout] 等待 AI 开始超时（{elapsed:.1f}s）")
                    break
                time.sleep(0.3)

        if ctx.active_turn_started:
            logger.debug("[阶段3] 进入增量输出阶段")
            yield from self._stream_output_phase(selector, ctx)
        else:
            logger.warning("[Exit] 未检测到 AI 回复，退出监控")

    # ===== 回复异常分类处理 =====

    @staticmethod
    def _dead_recovery_action(dead_secs: float, dead_limit: float,
                              retry_btn_found: bool, resent_once: bool,
                              has_payload: bool) -> Optional[str]:
        """[Dead] 判死后的恢复决策（纯函数，便于单测）。

        - 页面有错误面板的重试按钮 → 点重试（AI 回复未知错误的
          官方恢复入口）
        - 无回复（静默零产出）→ 同会话重发一次（仅一次）
        - 重发过仍死 → break 交上层 worker 换其他 chrome 实例
        """
        if dead_secs < dead_limit:
            return None
        if retry_btn_found:
            return 'click_retry'
        if not resent_once and has_payload:
            return 'resend'
        return 'break'

    def _detect_retry_button(self) -> bool:
        """检测页面错误面板的"重试/Retry"按钮（排除"重新生成"）。"""
        try:
            return bool(self.tab.run_js(
                'var bs = document.querySelectorAll("button");'
                'for (var i = 0; i < bs.length; i++) {'
                '  var b = bs[i];'
                '  var t = (b.innerText || "").trim();'
                '  var a = (b.getAttribute("aria-label") || "").trim();'
                '  if ((t === "重试" || t === "Retry" || a === "重试"'
                '      || a === "Retry") && b.offsetParent) return true;'
                '}'
                'return false;'))
        except Exception:
            return False

    def _click_page_retry_button(self) -> bool:
        """点击页面错误面板的重试按钮。"""
        try:
            return bool(self.tab.run_js(
                'var bs = document.querySelectorAll("button");'
                'for (var i = 0; i < bs.length; i++) {'
                '  var b = bs[i];'
                '  var t = (b.innerText || "").trim();'
                '  var a = (b.getAttribute("aria-label") || "").trim();'
                '  if ((t === "重试" || t === "Retry" || a === "重试"'
                '      || a === "Retry") && b.offsetParent)'
                '    { b.click(); return true; }'
                '}'
                'return false;'))
        except Exception:
            return False

    def _resend_last_message(self) -> bool:
        """同会话重发最后一次发送的内容（仅一次，无回复兜底）。"""
        payload = getattr(self, '_last_send_payload', '')
        input_sel = getattr(self, '_last_send_input_sel', '')
        if not payload or not input_sel:
            return False
        try:
            self._execute_fill(input_sel, payload, target_key='input_box')
            self._execute_click(
                getattr(self, '_last_send_btn_sel', ''),
                target_key='send_btn', optional=True)
            logger.info(f"[Resend] 无回复，同会话重发一次"
                        f"（{len(payload)} 字符）")
            return True
        except Exception as e:
            logger.warning(f"[Resend] 重发失败: {e}")
            return False

    def _detect_ai_start(self, baseline: dict, current: dict) -> tuple:
        if current['groups_count'] > baseline['groups_count']:
            increase = current['groups_count'] - baseline['groups_count']
            return True, f"节点数增加 {increase}"
        if current['is_generating']:
            return True, "生成指示器激活"
        if current['text_len'] > baseline['text_len'] + 3:
            growth = current['text_len'] - baseline['text_len']
            return True, f"文本增长 {growth} 字符"
        # baseline 容器为空（chatgpt 新版发送时预创建空回复容器）时，
        # 出现任何正文（哪怕 2 字符的"北京"）都说明回复已开始
        if baseline['text_len'] == 0 and current['text_len'] > 0:
            return True, f"空容器出现正文 {current['text_len']} 字符"
        # 思考折叠/结束（think_len 回落）且正文出现任意增长：
        # 短答案场景（如"1+1 等于 2。"约 10 字符）靠文本阈值会漏判
        b_think = baseline.get('think_len') or 0
        c_think = current.get('think_len') or 0
        if (b_think > 0 and c_think < b_think
                and current['text_len'] > baseline['text_len']):
            return True, "思考结束折叠且正文出现"
        return False, ""

    def _stream_output_phase(self, selector: str,
                             ctx: StreamContext) -> Generator[str, None, None]:
        yield from self._stream_output_phase_with_tools(selector, ctx)
        return

    def _stream_output_phase_with_tools(self, selector: str,
                                        ctx: StreamContext) -> Generator[str, None, None]:
        logger.info("========== _stream_output_phase_with_tools 进入 ==========")
        if not getattr(self, "_role_sent", False):
            self._role_sent = True
            yield self.formatter.pack_role_chunk()
        parser = ToolCallStreamParser(known_tools=self._active_tools or [])
        all_tool_calls: List[Dict] = []

        silence_start = time.time()
        last_output_time = None
        current_interval = BrowserConstants.STREAM_CHECK_INTERVAL_DEFAULT
        min_interval = BrowserConstants.STREAM_CHECK_INTERVAL_MIN
        max_interval = BrowserConstants.STREAM_CHECK_INTERVAL_MAX
        element_missing_count = 0
        max_element_missing = 10
        current_text = ""
        last_text_len = 0

        initial_snap = self._get_last_message_snapshot(selector)
        ctx.output_target_count = initial_snap['groups_count']
        ctx.output_target_anchor = initial_snap['anchor']

        peak_text_len = 0
        content_shrink_count = 0

        while True:
            if self._check_cancelled():
                break

            snap = self._get_last_message_snapshot(selector)
            current_count = snap['groups_count']
            current_anchor = snap['anchor']
            current_text = snap['text'] or ""
            still_generating = snap['is_generating']
            current_text_len = len(current_text)

            # v5.4：思考区增量先行下发（reasoning_content 独立通道），
            # 网页端思考在前、正文在后，顺序天然正确
            # SEND_REASONING_CONTENT=False 时直接丢弃，兼容 OpenClaw 等
            # 不识别 delta.reasoning_content 的客户端
            think_diff = ctx.calc_think_diff(snap.get('think_text') or "")
            if think_diff and SEND_REASONING_CONTENT:
                yield self.formatter.pack_reasoning_chunk(think_diff)
            think_len_now = len(snap.get('think_text') or "")
            if think_len_now > 0:
                ctx._think_total = think_len_now
            if think_diff and not getattr(ctx, '_think_start_logged', False):
                ctx._think_start_logged = True
                logger.info(
                    f"[Think] 思考开始，流式下发（累计 {think_len_now} 字符）")

            if current_text_len > peak_text_len:
                peak_text_len = current_text_len
                content_shrink_count = 0
            elif peak_text_len > 100 and current_text_len < peak_text_len * 0.5:
                if still_generating:
                    # z.ai agent 运行终端/命令面板时正文被临时替换：
                    # 生成指示器仍在 → 保持已发送状态等正文恢复，
                    # 不重置不重复下发（恢复后文本以已发前缀开头，
                    # calculate_diff 只取增量）
                    if not getattr(ctx, '_panel_wait_logged', False):
                        ctx._panel_wait_logged = True
                        logger.info(
                            f"[Panel] 正文被工具面板临时替换"
                            f"（{peak_text_len} -> {current_text_len}），"
                            "生成仍在进行，等待恢复")
                    time.sleep(0.3)
                    continue
                content_shrink_count += 1
                if content_shrink_count >= 2:
                    logger.info(f"[Collapse] 内容折叠：{peak_text_len} -> {current_text_len}")
                    ctx.reset_for_new_target()
                    peak_text_len = current_text_len
                    content_shrink_count = 0
                    silence_start = time.time()
                    last_output_time = None
                    last_text_len = current_text_len
                    time.sleep(0.2)
                    continue
            else:
                content_shrink_count = 0

            if current_count > ctx.output_target_count:
                if current_anchor != ctx.output_target_anchor:
                    if ctx.pending_new_anchor == current_anchor:
                        ctx.pending_new_anchor_seen += 1
                    else:
                        ctx.pending_new_anchor = current_anchor
                        ctx.pending_new_anchor_seen = 1
                    if ctx.pending_new_anchor_seen >= 2:
                        logger.info(f"[Switch] 切换节点: {ctx.output_target_anchor} -> {current_anchor}")
                        ctx.output_target_count = current_count
                        ctx.output_target_anchor = current_anchor
                        ctx.pending_new_anchor = None
                        ctx.pending_new_anchor_seen = 0
                        ctx.reset_for_new_target()
                        peak_text_len = 0
                        silence_start = time.time()
                        last_output_time = None
                        last_text_len = 0
                        if not current_text:
                            time.sleep(0.2)
                            continue
            else:
                ctx.pending_new_anchor = None
                ctx.pending_new_anchor_seen = 0

            if not current_text:
                if still_generating:
                    # 正文完全消失但生成指示器仍在：工具面板（终端/命令
                    # 执行）临时接管页面。等待恢复，上限 300s 防真死锁。
                    hidden_for = time.time() - getattr(ctx, '_hidden_since', 0)
                    if not getattr(ctx, '_hidden_since', 0):
                        ctx._hidden_since = time.time()
                        logger.info(
                            "[Panel] 正文临时消失（工具面板运行中），等待恢复")
                    elif hidden_for > 300:
                        logger.warning(
                            f"[Panel] 正文消失超 {hidden_for:.0f}s，放弃等待")
                        break
                elif ctx.sent_content_length > 0:
                    element_missing_count += 1
                    if element_missing_count >= max_element_missing:
                        break
                time.sleep(0.2)
                continue
            else:
                element_missing_count = 0
                ctx._hidden_since = 0

            if len(current_text) > len(ctx.max_seen_text):
                ctx.max_seen_text = current_text

            diff, _, _ = ctx.calculate_diff(current_text)
            if diff:
                if self._check_cancelled():
                    break
                ctx.update_after_send(diff, current_text)
                silence_start = time.time()
                if last_output_time is None:
                    last_output_time = time.time()
                current_interval = min_interval
                ctx.content_ever_changed = True

                plain_chunks, tool_calls = parser.feed(diff)
                logger.info(
                    f"[ToolCall] feed diff={len(diff)} chars, "
                    f"plain={len(plain_chunks)}, calls={len(tool_calls)}"
                )
                for pc in plain_chunks:
                    if pc:
                        yield self.formatter.pack_chunk(pc)
                if tool_calls:
                    logger.info(f"[ToolCall] 解析到 {len(tool_calls)} 个工具调用")
                    all_tool_calls.extend(tool_calls)
            else:
                if current_text == ctx.last_stable_text:
                    ctx.stable_text_count += 1
                else:
                    ctx.stable_text_count = 0
                    ctx.last_stable_text = current_text
                current_interval = min(current_interval * 1.5, max_interval)

            if current_text_len != last_text_len:
                ctx.content_ever_changed = True
                last_text_len = current_text_len

            silence_duration = time.time() - silence_start

            # ===== 检测并点击"继续"按钮 =====
            # 触发条件：生成已停止 + 曾有内容 + 沉默超过阈值
            if (not still_generating
                    and ctx.content_ever_changed
                    and silence_duration > BrowserConstants.get("CONTINUE_CHECK_SILENCE")):
                if self._try_click_continue():
                    ctx.continue_clicked_count += 1
                    logger.info(
                        f"[Continue] 已点击继续按钮（第 {ctx.continue_clicked_count} 次），"
                        f"重置沉默计时"
                    )
                    silence_start = time.time()
                    last_output_time = None
                    time.sleep(0.8)
                    continue

            # ===== 退出判定 =====
            if ctx.content_ever_changed:
                if not still_generating:
                    # 工具面板刚结束（正文曾被清空）：chatgpt 面板完成后
                    # 会把最终回复写回主容器，指示器先熄灭、文本后恢复。
                    # 立即退出会把面板残留短文本当最终回复（实测 12 字符
                    # 垃圾 → IDE 判失败重试 → 70K 重放 → 死循环切账号）。
                    if (getattr(ctx, '_hidden_since', 0)
                            and not current_text
                            and time.time() - ctx._hidden_since < 60):
                        time.sleep(0.5)
                        continue
                    if silence_duration > BrowserConstants.STREAM_SILENCE_THRESHOLD:
                        logger.info(f"[Exit] 生成结束（+{silence_duration:.1f}s）")
                        if getattr(ctx, '_think_total', 0):
                            logger.info(
                                f"[Think] 思考结束，共 {ctx._think_total} 字符")
                        break
                else:
                    if (ctx.stable_text_count >= BrowserConstants.STREAM_STABLE_COUNT_THRESHOLD and
                            silence_duration > BrowserConstants.STREAM_SILENCE_THRESHOLD_FALLBACK):
                        logger.info(f"[Exit] 内容稳定退出")
                        break
            else:
                if not still_generating and last_output_time is None:
                    if current_text_len > ctx.active_turn_baseline_len + 5:
                        break

            sleep_elapsed = 0
            while sleep_elapsed < current_interval:
                if self._check_cancelled():
                    break
                time.sleep(min(0.1, current_interval - sleep_elapsed))
                sleep_elapsed += 0.1

        full_text = ctx.max_seen_text or ""
        # 流式累计文本可能被页面重渲染截断/交叠（模型输出 DSML 时
        # DeepSeek/z.ai 会把整段重排成代码块），救援前重新抓一次
        # 页面最终文本，长者优先、另一份后备——上游明明给了命令却
        # 被截掉的场景由这里兜底。
        _final_text = None
        try:
            final_snap = self._get_last_message_snapshot(selector)
            _final_text = final_snap.get("text") or ""
            if _final_text and len(_final_text) > len(full_text):
                logger.info(
                    f"[ToolCall] 页面最终文本（{len(_final_text)} 字符）"
                    f"长于累计文本（{len(full_text)}），优先救援最终文本")
        except Exception:
            pass
        _rescue_candidates = [t for t in (_final_text, full_text) if t]
        # 去重保序，长者在前
        _rescue_candidates = sorted(
            dict.fromkeys(_rescue_candidates), key=len, reverse=True)
        for _rt in _rescue_candidates:
            if all_tool_calls:
                break
            logger.info(f"[ToolCall] 全文救援解析（{len(_rt)} 字符）")
            rescue = ToolCallStreamParser(known_tools=self._active_tools or [])
            _, rescue_calls = rescue.feed(_rt)
            _, _rescue_flush_calls = rescue.flush()
            rescue_calls.extend(_rescue_flush_calls)
            if rescue_calls:
                logger.info(f"[ToolCall] 全文救援解析到 {len(rescue_calls)} 个工具调用")
                all_tool_calls.extend(rescue_calls)

        tail_plain, tail_calls = parser.flush()
        for pc in tail_plain:
            if pc:
                yield self.formatter.pack_chunk(pc)
        if tail_calls:
            logger.info(f"[ToolCall] 流结束兜底解析到 {len(tail_calls)} 个工具调用")
            all_tool_calls.extend(tail_calls)

        if all_tool_calls:
            # 去重：同名同参的重复调用（流式+救援双路解析/页面重排）只发一次，
            # 否则 IDE 会重复执行同一工具
            seen_calls = set()
            unique_calls = []
            for tc in all_tool_calls:
                fn = tc.get("function", {})
                key = (fn.get("name"), fn.get("arguments"))
                if key in seen_calls:
                    continue
                seen_calls.add(key)
                unique_calls.append(tc)
            all_tool_calls = unique_calls
            for tc in all_tool_calls:
                fn = tc.get("function", {})
                logger.info(
                    f"[ToolCall] 即将发送: name={fn.get('name')}, "
                    f"args={fn.get('arguments')}"
                )
            logger.info(f"[ToolCall] 发送 {len(all_tool_calls)} 个工具调用，finish_reason=tool_calls")
            yield self.formatter.pack_tool_calls_chunk(all_tool_calls)
            self._already_finished = True
            yield self.formatter.pack_finish(finish_reason="tool_calls")
            return

        if not self._check_cancelled():
            yield from self._final_settle_and_output(selector, ctx)

        if (not getattr(self, "_already_finished", False)
                and ctx.sent_content_length == 0):
            logger.warning("[ToolRound] 本轮无任何输出")
            yield self.formatter.pack_error(
                "模型本轮没有输出任何内容，请重试",
                error_type="empty_response",
                code="empty_stream",
            )

    def _final_settle_and_output(self, selector: str,
                                 ctx: StreamContext) -> Generator[str, None, None]:
        settle_time = 1.5
        hardcap = 5.0
        start = time.time()
        stable_start = time.time()
        last_snap = self._get_last_message_snapshot(selector)

        logger.debug(f"[Settle] 开始最终稳定等待，当前 count={last_snap['groups_count']}")

        while True:
            if self._check_cancelled():
                break
            now = time.time()
            if now - start > hardcap:
                break
            if now - stable_start >= settle_time:
                break
            time.sleep(0.15)
            snap = self._get_last_message_snapshot(selector)
            changed = False
            if snap['groups_count'] > last_snap['groups_count']:
                logger.info(f"[Settle] 检测到新节点: count {last_snap['groups_count']} -> {snap['groups_count']}")
                changed = True
                if snap['anchor'] != ctx.output_target_anchor:
                    logger.info(f"[Settle] 切换到新节点: {snap['anchor']}")
                    ctx.output_target_anchor = snap['anchor']
                    ctx.output_target_count = snap['groups_count']
                    ctx.reset_for_new_target()
            if snap['text_len'] != last_snap['text_len']:
                changed = True
            if snap['anchor'] != last_snap['anchor']:
                changed = True
            if changed:
                stable_start = time.time()
            last_snap = snap

        final_snap = self._get_last_message_snapshot(selector)
        final_text = final_snap.get('text', "") or ""

        # v5.4：收尾前补发思考区剩余增量（若思考仍未闭合）
        think_tail = ctx.calc_think_diff(final_snap.get('think_text') or "")
        if think_tail.strip():
            logger.debug(f"[Settle] 补发思考增量: {len(think_tail)} 字符")
            yield self.formatter.pack_reasoning_chunk(think_tail)

        logger.debug(f"[Settle] 最终快照: count={final_snap['groups_count']}, text_len={len(final_text)}")

        if final_text:
            final_effective_start = ctx.active_turn_baseline_len + ctx.sent_content_length
            if len(final_text) > final_effective_start:
                remaining = final_text[final_effective_start:]
                if remaining.strip():
                    logger.debug(f"[Final] 发送剩余内容: {len(remaining)} 字符")
                    yield from self._emit_remaining(remaining, ctx)
            self._final_complete_text = final_text[ctx.active_turn_baseline_len:]
        else:
            fallback_text = self._get_active_turn_text(selector)
            if fallback_text:
                final_effective_start = ctx.active_turn_baseline_len + ctx.sent_content_length
                if len(fallback_text) > final_effective_start:
                    remaining = fallback_text[final_effective_start:]
                    if remaining.strip():
                        logger.debug(f"[Final Fallback] 发送剩余内容: {len(remaining)} 字符")
                        yield from self._emit_remaining(remaining, ctx)
                self._final_complete_text = fallback_text[ctx.active_turn_baseline_len:]
            else:
                self._final_complete_text = ctx.max_seen_text[ctx.active_turn_baseline_len:] if ctx.max_seen_text else ""

        logger.info(f"========== 流式监听结束，总输出: {ctx.sent_content_length} 字符 ==========")

    def _emit_remaining(self, remaining: str,
                        ctx: StreamContext) -> Generator[str, None, None]:
        """收尾文本必须过解析器：剥离思考块/标签残渣，并兜出漏网的工具调用。

        此前这里直接把剩余文本 pack_chunk 原样发出，绕过了流式解析器，
        是"脏内容"与"工具调用泄漏为正文"的来源之一。若剩余文本里
        兜出完整工具调用，则发送 tool_calls + finish 并标记已收尾。
        """
        parser = ToolCallStreamParser(known_tools=self._active_tools or [],
                                      clean_head=False)
        plain_chunks, calls = parser.feed(remaining)
        tail_plain, tail_calls = parser.flush()
        plain_chunks.extend(tail_plain)
        calls.extend(tail_calls)

        for pc in plain_chunks:
            if pc:
                yield self.formatter.pack_chunk(pc)
        if calls:
            logger.info(f"[Final] 收尾兜出 {len(calls)} 个工具调用")
            yield self.formatter.pack_tool_calls_chunk(calls)
            self._already_finished = True
            yield self.formatter.pack_finish(finish_reason="tool_calls")

        ctx.sent_content_length += sum(len(pc) for pc in plain_chunks if pc)

    def _get_last_message_snapshot(self, selector: str) -> dict:
        result = {
            'groups_count': 0, 'anchor': None, 'text': '',
            'text_len': 0, 'is_generating': False,
            'think_text': '', 'think_len': 0,
        }
        try:
            eles = self.finder.find_all(selector, timeout=0.5)
            if not eles:
                return result
            result['groups_count'] = len(eles)
            last_ele = eles[-1]
            main_text, think_text = self._read_visible_text(last_ele)
            result['text'] = main_text
            result['text_len'] = len(main_text)
            result['think_text'] = think_text
            result['think_len'] = len(think_text)
            result['anchor'] = self._get_message_anchor(last_ele)
            if self._generating_checker is None:
                self._generating_checker = GeneratingStatusCache(self.tab)
            result['is_generating'] = self._generating_checker.is_generating()
        except Exception as e:
            logger.debug(f"Last snapshot 异常: {e}")
        return result


class BrowserCore:
    _instance: Optional['BrowserCore'] = None
    _lock = threading.Lock()

    def __new__(cls, port: int = None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self, port: int = None):
        if self._initialized:
            return
        self.port = port or BrowserConstants.DEFAULT_PORT
        self.instance_name = "main"
        self.page: Optional[ChromiumPage] = None
        self.stealth_mode = False
        self._connected = False
        self._should_stop_checker: Callable[[], bool] = lambda: False
        self.formatter = SSEFormatter()
        self.config_engine = None
        self._tab_manager: Optional[TabManager] = None
        self._initialized = True

    def _get_upload_config(self, site_config: dict) -> dict:
        try:
            from data_models import merge_upload_config
            return merge_upload_config(site_config.get("upload_config"))
        except Exception:
            return site_config.get("upload_config") or {
                "enabled": True,
                "max_prompt_length": 2000,
                "max_file_size": 5 * 1024 * 1024,
                "chunk_size": 50000,
                "upload_dir": "uploads",
                "file_ext": "txt",
                "short_prompt_template": "请阅读我上传的文件 {filename}，并按照文件中的要求完成任务。",
                "chunked_prompt_template": "我上传了 {count} 个分片文件，请按文件名顺序依次阅读后完成任务。",
                "wait_after_upload": 1.5,
                "max_upload_files": 10,
            }

    def set_stop_checker(self, checker: Callable[[], bool]):
        self._should_stop_checker = checker or (lambda: False)

    @property
    def tab_manager(self) -> TabManager:
        if self._tab_manager is None:
            if not self.ensure_connection():
                raise BrowserConnectionError("无法连接到浏览器")
            self._tab_manager = TabManager(self.page)
        return self._tab_manager

    def _get_config_engine(self):
        if self.config_engine is None:
            from config_engine import config_engine
            self.config_engine = config_engine
        return self.config_engine

    def _port_open(self, port: int) -> bool:
        """调试端口是否有浏览器在监听（避免 DrissionPage 连不上时
        自动拉起一个全新空白浏览器）。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.6)
                return s.connect_ex(('127.0.0.1', port)) == 0
        except Exception:
            return False

    def _launch_instance_chrome(self, port: int) -> bool:
        """按 INSTANCES 配置启动该账号专属 Chrome（独立 profile，
        登录态跨重启保留）。无 profile 配置的实例不启动。"""
        try:
            cfg_path = Path(__file__).resolve().parent / "browser_config.json"
            cfg = (json.loads(cfg_path.read_text(encoding='utf-8'))
                   if cfg_path.exists() else {})
            inst = next((i for i in (cfg.get('INSTANCES') or [])
                         if int(i.get('port') or 0) == port), None)
            udd = _resolve_profile_dir((inst or {}).get('user_data_dir') or '')
            if not udd:
                return False
            chrome = None
            for c in (cfg.get('CHROME_PATH'),
                      r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                      r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
                if c and os.path.exists(c):
                    chrome = c
                    break
            if not chrome:
                logger.error("[Instance] 未找到 Chrome/Edge 可执行文件")
                return False
            os.makedirs(udd, exist_ok=True)
            # 直接打开默认站点（不留 about:blank 空白页，
            # 与 Dashboard 新增账号行为一致，窗口弹出即可登录）
            default_site = cfg.get('DEFAULT_SITE') or 'chatgpt.com'
            args = [chrome, f"--remote-debugging-port={port}",
                    f"--user-data-dir={udd}",
                    "--no-first-run", "--no-default-browser-check"]
            # 必须显式传代理：服务以 LocalSystem 运行时子进程 Chrome 读不到
            # HKCU 的系统代理设置（ProxyServer=127.0.0.1:7897），会退回直连，
            # 对 chatgpt.com 只会得到 ERR_CONNECTION_TIMED_OUT，而页面一旦被
            # 判成"无输入框/上传假阳性"，排查方向就被带去怀疑站点与额度。
            proxy = (cfg.get('PROXY_SERVER') or "").strip()
            if proxy:
                args.append(f"--proxy-server={proxy}")
                logger.info(f"[Instance] 显式使用代理 {proxy}"
                            "（服务上下文读不到用户系统代理）")
            args.append(f"https://{default_site}")
            subprocess.Popen(
                args,
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP)
            logger.info(f"[Instance] 已自动启动账号实例 :{port} "
                        f"profile={udd}")
            for _ in range(30):
                time.sleep(1)
                if self._port_open(port):
                    return True
            return False
        except Exception as e:
            logger.warning(f"[Instance] 自动启动实例 :{port} 失败: {e}")
            return False

    def _connect(self) -> bool:
        try:
            if not self._port_open(self.port):
                # 端口无浏览器：优先按账号 profile 自动启动（登录态保留）；
                # 绝不允许 DrissionPage 静默拉起全新空白浏览器
                logger.warning(
                    f"[Instance] 端口 {self.port} 无浏览器，"
                    f"尝试按账号配置自动启动")
                if not self._launch_instance_chrome(self.port):
                    logger.error(
                        f"浏览器未启动且该账号无 user_data_dir 配置，"
                        f"拒绝拉起空白浏览器。请在 Dashboard 启动实例，"
                        f"或手动启动带 --remote-debugging-port="
                        f"{self.port} 的 Chrome")
                    self._connected = False
                    return False
            logger.debug(f"连接浏览器 127.0.0.1:{self.port}")
            self.page = ChromiumPage(addr_or_opts=f"127.0.0.1:{self.port}")
            self._connected = True
            logger.info("浏览器连接成功")
            return True
        except Exception as e:
            logger.error(f"浏览器连接失败: {e}")
            self._connected = False
            return False

    def current_tab(self):
        """当前实例的活动标签页。

        main.py 此前用 getattr(browser, 'tab', None) 取标签页，而本类没有
        tab 属性 → 恒为 None → "零输出刷新页面自恢复"从来没真正执行过。"""
        try:
            return self.page.latest_tab
        except Exception:
            return None

    def page_state(self) -> dict:
        """当前实例页面可用性（main 侧决定是否换账号前先问这个）。"""
        return probe_page_state(self.current_tab())

    def health_check(self) -> Dict[str, Any]:
        result = {
            "status": "unhealthy", "connected": False,
            "port": self.port, "tab_url": None, "tab_title": None, "error": None,
        }
        try:
            if not self.page:
                # 只探测不拉起：/health 被仪表盘/监控周期轮询，自动拉起
                # 默认实例会造成"反复拉起又不用"（2026-11-05 用户反馈根因）
                try:
                    with socket.create_connection(
                            ("127.0.0.1", int(self.port)), timeout=1):
                        _alive = True
                except Exception:
                    _alive = False
                if not _alive:
                    result["error"] = "浏览器未启动"
                    return result
                if not self._connect():
                    result["error"] = "无法连接到浏览器"
                    return result
            tab = self.page.latest_tab
            if not tab:
                result["error"] = "无可用标签页"
                return result
            result["status"] = "healthy"
            result["connected"] = True
            result["tab_url"] = tab.url
            result["tab_title"] = tab.title
        except Exception as e:
            result["error"] = str(e)
            self._connected = False
        return result

    def ensure_connection(self) -> bool:
        if self._connected:
            try:
                _ = self.page.latest_tab
                return True
            except Exception:
                self._connected = False
        return self._connect()

    @staticmethod
    def list_instances() -> List[Dict[str, Any]]:
        """browser_config.json 里的浏览器实例列表（多账号窗口）。

        格式：{"INSTANCES": [{"name": "acc1", "port": 9222},
                             {"name": "acc2", "port": 9223,
                              "user_data_dir": "C:\\poe_acc2"}, ...]}
        无配置时回退为单实例 [{name: main, port: DEFAULT_PORT}]。"""
        try:
            inst = BrowserConstants.get('INSTANCES')
            if isinstance(inst, list) and inst:
                return [i for i in inst if isinstance(i, dict) and i.get('port')]
        except Exception:
            pass
        return [{"name": "main", "port": BrowserConstants.get('DEFAULT_PORT')}]

    def switch_instance(self, exclude_port: Optional[int] = None,
                        site_domain: Optional[str] = None,
                        exclude_ports: Optional[set] = None,
                        credit_models: Optional[List[str]] = None) -> bool:
        """切换到下一个可用浏览器实例（多账号故障自动转移）。

        依次尝试实例列表中除 exclude_port 外的每个端口，第一个能
        连上的即启用。全部失败返回 False（保持当前实例）。
        site_domain 指定时跳过积分耗尽的账号：credit_models 给出时
        （同站点候选模型列表），所有候选模型都耗尽才跳过该账号；
        未给出时沿用站点级记录判断。
        切换成功后关闭原账号的浏览器窗口：不可用的账号不再留着窗口
        占位（profile 保留，之后需要时可按 profile 再次自动拉起）。"""
        old_port = self.port
        old_page = self.page
        for inst in self.list_instances():
            port = int(inst.get('port') or 0)
            if not port or port == exclude_port:
                continue
            if exclude_ports and port in exclude_ports:
                logger.info(f"[Instance] 实例 {inst.get('name')} "
                            f"(:{port}) 本次请求已尝试过，跳过")
                continue
            if site_domain:
                models = credit_models if credit_models else [""]
                if all(CreditStateStore.is_exhausted(site_domain, port, m)
                       for m in models):
                    logger.info(f"[Instance] 实例 {inst.get('name')} "
                                f"(:{port}) {site_domain} 候选模型积分均未重置，跳过")
                    continue
            # 先用 socket 探测端口：DrissionPage 连不上时会自作主张
            # 拉起一个新 Chrome（空 profile、未登录），必须避免；
            # 端口不通但实例配置了 user_data_dir → 按账号 profile
            # 自动启动（登录态保留），实现积分耗尽自动切号
            import socket as _socket
            _s = _socket.socket()
            _s.settimeout(0.8)
            _alive = False
            try:
                _s.connect(("127.0.0.1", port))
                _alive = True
            except Exception:
                _alive = False
            finally:
                _s.close()
            if not _alive:
                udd = inst.get('user_data_dir')
                if udd:
                    logger.info(f"[Instance] 实例 {inst.get('name')} "
                                f"(:{port}) 未启动，按账号 profile 自动启动")
                    if self._launch_instance_chrome(port):
                        _alive = True
                if not _alive:
                    logger.info(f"[Instance] 实例 {inst.get('name')} "
                                f"(:{port}) 端口不通且无 profile 配置，跳过")
                    continue
            try:
                probe = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
                _ = probe.latest_tab  # 可达性探测
            except Exception:
                logger.info(f"[Instance] 实例 {inst.get('name')} "
                            f"(:{port}) 连接失败，跳过")
                continue
            # 切过去之前先看候选账号页面状态。2026-11-05 的整条链路挂死就是
            # 这里没查：一次瞬时"发送按钮未启用 + 消息未上屏"造成零输出，
            # 于是切到未登录的 acc2，又把唯一登录着的 acc3 窗口 quit 掉，
            # 此后每个请求都停在登录墙上。
            try:
                _cand = probe_page_state(probe.latest_tab)
            except Exception:
                _cand = {"state": "unknown", "detail": ""}
            if _cand.get("state") in ("need_login", "error_page"):
                logger.warning(
                    f"[Instance] 候选 {inst.get('name')} (: {port}) 不可用："
                    f"{_cand.get('detail') or _cand['state']}，跳过")
                continue
            logger.warning(
                f"[Instance] 切换浏览器实例: {self.instance_name}:"
                f"{self.port} -> {inst.get('name')}:{port}")
            self.port = port
            self.instance_name = inst.get('name') or f"port{port}"
            self.page = probe
            self._connected = True
            self._tab_manager = None
            # 切换后的实例窗口/工作 tab 切前台（用户可见管线在干活）
            try:
                _t = probe.latest_tab
                if _t:
                    _t.set.activate()
            except Exception:
                pass
            # 不再 quit 原实例窗口。原窗口"不可用"往往只是这一次请求零输出
            # （发送按钮未启用、消息未上屏、站点限流），账号与登录态都还好着；
            # 一旦关掉，服务重启该 profile 时若上下文不对（服务以 LocalSystem
            # 启动的 Chrome 解不开用户 Cookie）就再也回不来，只能整条链路挂死。
            # 各账号本就是独立 profile 的独立窗口，留着不互相干扰，还能切回。
            if old_page is not None and old_port:
                logger.info(
                    f"[Instance] 保留原实例窗口 :{old_port}"
                    "（仅切走，不关闭，可切回）")
            return True
        logger.warning("[Instance] 无可用备用实例，保持当前实例")
        return False

    def get_active_tab(self):
        if not self.ensure_connection():
            raise BrowserConnectionError("无法连接到浏览器")
        return self.tab_manager.get_active_tab(pin=True)

    def _session_fp(self, messages) -> str:
        """IDE 会话指纹：首条 system + 首条 user 内容的 hash。

        只取这两条（跳过 assistant/tool）——CodeArts 同一会话每轮重放
        完整历史时这两条恒定 → fp 恒定；IDE 新开会话 / 子代理独立
        请求 → fp 不同。"""
        try:
            sys_txt = ""
            user_txt = ""
            for m in (messages or []):
                role = (m.get("role") or "").lower()
                if not sys_txt and role == "system":
                    sys_txt = str(m.get("content") or "")[:1500]
                if not user_txt and role == "user":
                    c = m.get("content")
                    if isinstance(c, list):
                        c = " ".join(
                            str(x.get("text", "")) for x in c
                            if isinstance(x, dict))
                    user_txt = str(c or "")[:1500]
                if sys_txt and user_txt:
                    break
            import hashlib
            return hashlib.md5(
                f"{sys_txt}|{user_txt}".encode(
                    "utf-8", "ignore")).hexdigest()[:10]
        except Exception:
            return ""


    def _wait_for_composer(self, tab, domain: str,
                           timeout: Optional[int] = None) -> dict:
        """等站点把输入控件渲染出来再开始操作。

        导航后固定 sleep(4) 对 ChatGPT 冷启动不够（首连、切号后常见 10~20s
        才出 composer），而 FILL 的元素超时只有 3s，于是报"找不到输入框"
        "no-el"，上层当成站点控件/额度问题去轮转账号，真因只是没等页面就绪。
        错误页与登录墙提前退出——继续等下去没有意义，让守卫给出真因。"""
        if timeout is None:
            try:
                timeout = int(BrowserConstants.get("EDITOR_READY_TIMEOUT") or 30)
            except Exception:
                timeout = 30
        # ChatGPT 一进来先渲染带"登录/免费注册"的静态外壳，几秒后才水合成
        # 已登录应用；need_login 要连续成立一段时间才算真的未登录，
        # 否则水合期会被误判成登录墙（18:09 实测把登录着的 acc3 判死）。
        grace = 10.0
        t0 = time.time()
        st = probe_page_state(tab)
        login_since = None
        while st.get("state") not in ("ok", "error_page") \
                and time.time() - t0 < max(3, timeout):
            if st.get("state") == "need_login":
                if login_since is None:
                    login_since = time.time()
                elif time.time() - login_since >= grace:
                    break
            else:
                login_since = None
            time.sleep(1.0)
            st = probe_page_state(tab)
        waited = time.time() - t0
        if st.get("state") == "ok":
            if waited > 1.5:
                logger.info(f"[Route] {domain} 输入区就绪，额外等待 "
                            f"{waited:.1f}s")
        else:
            logger.warning(
                f"[Route] {domain} 页面未就绪（等待 {waited:.1f}s）："
                f"{st.get('detail') or st.get('state')}")
        return st

    def resolve_tab_for_model(self, model: Optional[str], messages=None):
        """按 model id 解析目标站点与发消息 tab。

        不做同模型多 tab 切换/会话绑定/新开 tab（2026-11-05 用户要求
        删除）：始终使用当前活动 tab，域名不符目标站点时导航到站点
        入口。返回 (tab, domain, session_new)——session_new 按消息
        指纹是否有已发送基准判定（驱动压缩/续传），与 tab 无关。"""
        if not self.ensure_connection():
            raise BrowserConnectionError("无法连接到浏览器")
        m = (model or "").strip().lower()
        domain = None
        engine = None
        if m and m not in ("web-browser", "default"):
            try:
                engine = self._get_config_engine()
                domain = match_site_domain(m, list(engine.sites.keys()))
            except Exception:
                domain = None
        tab = self.get_active_tab()
        if not domain:
            # 未命中 model（含默认 web-browser）→ 活动 tab 所在配置站点
            # → 默认站点 → 第一个站点（原 fallback 逻辑并入主流程）
            mapped_domain = None
            try:
                engine = engine or self._get_config_engine()
                mapped_domain = detect_tab_site_domain(
                    getattr(tab, "url", ""), list(engine.sites.keys()))
            except Exception:
                mapped_domain = None
            if mapped_domain:
                logger.info(
                    f"[Route] model={model} 未命中任何站点，"
                    f"映射到正在使用的站点 {mapped_domain}")
                domain = mapped_domain
            else:
                try:
                    engine = engine or self._get_config_engine()
                    default_site = (BrowserConstants.get("DEFAULT_SITE") or "")
                    if default_site in engine.sites:
                        domain = default_site
                    elif "chat.z.ai" in engine.sites:
                        domain = "chat.z.ai"
                    else:
                        domain = next(iter(engine.sites))
                except Exception:
                    domain = None
        if not domain:
            return tab, None, True
        # 目标站点入口 / 子模型会话页 URL
        model_url = ""
        entry = f"https://{domain}"
        try:
            engine = engine or self._get_config_engine()
            site_cfg = engine.sites.get(domain, {})
            slug = extract_model_slug(model, domain)
            if slug:
                tpl = str(site_cfg.get("MODEL_URL_TEMPLATE") or "")
                if tpl:
                    model_url = tpl.format(model=slug)
                else:
                    logger.info(
                        f"[Route] 站点 {domain} 未配置 MODEL_URL_TEMPLATE，"
                        f"子模型 {slug} 忽略，使用主模型入口")
            entry = (model_url or site_cfg.get("entry_url")
                     or f"https://{domain}")
        except Exception:
            pass
        # 域名/子模型页不符才导航（不切 tab、不新开 tab）
        cur = (getattr(tab, "url", "") or "").lower()
        target = model_url or entry
        if domain not in cur:
            logger.info(
                f"[Route] model={model} -> {domain} (活动tab导航 {target})")
            tab.get(target)
            time.sleep(4)
        elif model_url and model_url.lower() not in cur:
            logger.info(f"[Route] 切换到模型页 {model_url}")
            tab.get(model_url)
            time.sleep(4)
        # 页面健康检查：空白页重导航（登出弹窗时保留现场）
        try:
            site_cfg = self._get_config_engine().sites.get(domain, {})
            entry_u = (site_cfg.get("entry_url") or f"https://{domain}")
            rc = (site_cfg.get("selectors", {}).get("result_container", ""))
            healthy = False
            if rc:
                n = tab.run_js(
                    "let t=0;document.querySelectorAll(%s)"
                    ".forEach(e=>t+=(e.textContent||'').length);return t;"
                    % json.dumps(_plain_css(rc)))
                healthy = isinstance(n, int) and n >= 20
            if not healthy:
                if self._detect_login_modal(tab):
                    logger.warning(
                        "[Route] 检测到登录弹窗（会话已登出），"
                        "跳过健康重导航")
                else:
                    logger.warning(f"[Route] 页面空白，重新导航 {entry_u}")
                    tab.get(entry_u)
                    time.sleep(4)
        except Exception:
            pass
        # 就绪等待：composer 没渲染就往下走，FILL 只会拿到
        # "找不到输入框/no-el"，然后被误判成站点控件问题去切号
        self._wait_for_composer(tab, domain)
        # session_new：该消息指纹已有发送基准（_fp_last_payload）→ 续传
        # 轮（启用压缩）；否则新会话全量重放。与 tab 无关——原 fp 绑定
        # 命中/新开的等价判定（2026-11-05 删除切 tab 逻辑后保留语义）
        fp = self._session_fp(messages)
        fk = f"{domain}:{fp}" if fp else ""
        session_new = not (fk and (getattr(self, '_fp_last_payload', {})
                                   or {}).get(fk))
        logger.info(
            f"[Route] model={model} -> {domain} "
            f"({'新会话' if session_new else '续传'})")
        return tab, domain, session_new


    def execute_workflow(self, messages: List[Dict],
                         stream: bool = True,
                         tools: Optional[List[Dict]] = None,
                         tool_choice: Any = None,
                         model: Optional[str] = None,
                         think_hint: bool = False) -> Generator[str, None, None]:
        is_valid, error_msg, sanitized_messages = MessageValidator.validate(messages)
        if not is_valid:
            yield self.formatter.pack_error(
                f"无效请求: {error_msg}",
                error_type="invalid_request_error",
                code="invalid_messages",
            )
            return
        if stream:
            yield from self._execute_workflow_stream(
                sanitized_messages, tools=tools, tool_choice=tool_choice,
                model=model, think_hint=think_hint
            )
        else:
            yield from self._execute_workflow_non_stream(
                sanitized_messages, tools=tools, tool_choice=tool_choice,
                model=model, think_hint=think_hint
            )

    def execute_workflow_locked(self, messages: List[Dict],
                                stream: bool = True,
                                tools: Optional[List[Dict]] = None,
                                tool_choice: Any = None,
                                model: Optional[str] = None,
                                think_hint: bool = False) -> Generator[str, None, None]:
        """带 per-tab 请求互斥的 execute_workflow。

        CodeArts 会并发发请求（主 agent + 记忆提取等子代理），共享同一个
        网页 tab。不串行的话：A 请求正在生成，B 请求点"新聊天"把 A 的
        会话切走 → A 的后续 tool_result 续传发进陌生对话，上下文全丢
        （实测表现为"任务被拆散""AI 看不到附件"）。加锁后 B 排队等 A
        完整结束（含流式收集）才开始，new_chat 只发生在空闲 tab 上。
        客户端取消时生成器被 close → with 退出 → 锁自动释放。"""
        lock = getattr(self, '_req_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._req_lock = lock
        with lock:
            yield from self.execute_workflow(
                messages, stream=stream, tools=tools,
                tool_choice=tool_choice, model=model,
                think_hint=think_hint)

    def _execute_workflow_stream(self, messages: List[Dict],
                                tools: Optional[List[Dict]] = None,
                                tool_choice: Any = None,
                                model: Optional[str] = None,
                                think_hint: bool = False) -> Generator[str, None, None]:
        SSEFormatter.begin_response()

        try:
            if DIRECT_TOOL_DEBUG:
                is_tool_continuation_debug = any(m.get("role") == "tool" for m in messages)
                if not is_tool_continuation_debug:
                    logger.info("[DirectTool] 调试模式：构造 read tool_call")
                    last_user = ""
                    for m in reversed(messages):
                        if m.get("role") == "user":
                            last_user = m.get("content") or ""
                            break
                    m_path = re.search(
                        r'([A-Za-z]:[\\/](?:[^\s，。；;:"\'<>|?*()\[\]{}]+[\\/])+'
                        r'[^\s，。；;:"\'<>|?*()\[\]{}]+)',
                        last_user,
                    )
                    if m_path:
                        file_path = m_path.group(1).rstrip(".,;:")
                        logger.info(f"[DirectTool] 路径: {file_path}")
                        tool_call_id = f"call_debug_{uuid.uuid4().hex[:16]}"
                        args_json = json.dumps({"filePath": file_path}, ensure_ascii=False)
                        tc = {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {"name": "read", "arguments": args_json},
                        }
                        yield self.formatter.pack_role_chunk()
                        yield self.formatter.pack_tool_calls_chunk([tc])
                        yield self.formatter.pack_finish(finish_reason="tool_calls")
                        return

            if self._should_stop_checker():
                yield self.formatter.pack_error("请求已取消", code="cancelled")
                yield self.formatter.pack_finish()
                return

            if not self.ensure_connection():
                yield self.formatter.pack_error(
                    "浏览器连接失败", error_type="connection_error",
                    code="browser_disconnected")
                yield self.formatter.pack_finish()
                return

            try:
                tab, resolved_domain, session_new = \
                    self.resolve_tab_for_model(model, messages)
                if resolved_domain:
                    domain = resolved_domain
                else:
                    try:
                        domain = tab.url.split("//")[-1].split("/")[0]
                    except Exception:
                        yield self.formatter.pack_error("无法解析页面URL")
                        yield self.formatter.pack_finish()
                        return
            except BrowserConnectionError as e:
                yield self.formatter.pack_error(str(e))
                yield self.formatter.pack_finish()
                return

            logger.debug(f"域名: {domain}")

            page_status = self._check_page_status(tab)
            if not page_status["ready"]:
                yield self.formatter.pack_error(
                    f"页面未就绪: {page_status['reason']}", code="page_not_ready")
                yield self.formatter.pack_finish()
                return

            config_engine = self._get_config_engine()
            site_config = config_engine.get_site_config(domain, tab.html)
            if not site_config:
                yield self.formatter.pack_error("配置加载失败", code="config_error")
                yield self.formatter.pack_finish()
                return

            selectors = site_config.get("selectors", {})
            workflow = site_config.get("workflow", [])

            # 子模型（poe bot）：工作流里的 NAVIGATE 步骤改导航到
            # 该模型会话页（MODEL_URL_TEMPLATE），否则仍会跳回主模型
            _slug = extract_model_slug(model, domain)
            if _slug:
                _tpl = str(site_config.get("MODEL_URL_TEMPLATE") or "")
                if _tpl:
                    _model_url = _tpl.format(model=_slug)
                    _n_nav = 0
                    for step in workflow:
                        if (step.get("action") == "NAVIGATE"
                                and isinstance(step.get("value"), str)
                                and domain.lower() in step["value"].lower()):
                            step["value"] = _model_url
                            _n_nav += 1
                    if _n_nav:
                        logger.info(
                            f"[Route] 子模型 {_slug}: {_n_nav} 个 NAVIGATE "
                            f"步骤改用 {_model_url}")
            self.stealth_mode = site_config.get("stealth", False)
            upload_cfg = self._get_upload_config(site_config)

            is_tool_continuation = any(m.get("role") == "tool" for m in messages)
            # 续聊条件：多轮工具循环，或会话指纹已绑定 tab（IDE 同一会话
            # 的后续调用，即使客户端重放历史不带 role=="tool" 也续聊）。
            # 只有 IDE 新会话的第一次调用（新指纹）才点"新聊天"。
            is_continuation = is_tool_continuation or not session_new
            # 截断历史只在"确实续在同一 tab"时进行；绑定失效重新新开的
            # 会话没有前文，必须全量重放（session_new=True → 不截断）
            if is_continuation and not session_new:
                # 续聊：保留 system + 最后一条带 tool_calls 的 assistant 及其后全部
                # tool 结果。更早的历史网页端会话里本来就有；丢掉 assistant 的
                # tool_calls 会让网页 AI 不知道自己上轮调了什么，回复就会跑偏。
                start_idx = -1
                for i in range(len(messages) - 1, -1, -1):
                    if (messages[i].get("role") == "assistant"
                            and messages[i].get("tool_calls")):
                        start_idx = i
                        break
                if start_idx < 0:
                    start_idx = 0
                    for i, m in enumerate(messages):
                        if m.get("role") == "user":
                            start_idx = i
                system_msgs = [m for m in messages[:start_idx]
                               if m.get("role") == "system"]
                effective_messages = system_msgs + messages[start_idx:]
                logger.info(
                    f"[ToolRound] 续聊模式，system={len(system_msgs)}，"
                    f"从 index={start_idx} 起保留 {len(messages) - start_idx} 条"
                )
            else:
                effective_messages = messages

            # 附件场景：每轮上传的都是新文件，网页端会话记忆里未必看得到
            # 历史轮次的工具表 → 续传轮也在 prompt 末尾带工具表（附件体
            # 积大，多 1-2KB 工具表占比小；输入框仍只有短提示不膨胀）
            raw_prompt = ToolRoundBuilder.build(
                effective_messages, tools, tool_choice,
                protocol_mode="always_table", think_hint=think_hint)

            # —— 会话消息压缩（msg_condense）——
            # IDE 每轮重放完整历史（系统规则+工具轮次），prompt 数万字且
            # 与上一轮几乎相同，导致每条消息都要分片。对比本会话上一轮
            # 实际发送的原文，公共前缀替换为占位符只发增量尾部——网页
            # 会话里前文都在，AI 可直接读会话上文。仅续聊同 tab 时启用
            # （新会话新 tab 必须全量发）；压缩后长度下降，多数轮次不再
            # 触发分片/上传。
            _fk = ""
            _condense_original = raw_prompt  # 压缩前原文（成功后记录用）
            _condense_applied = False
            _send_confirmed = False
            # fp 键必须无条件计算：首轮（新会话）也要在成功后记录交付
            # 文本，否则下一轮永远没有比对基准、压缩永远不生效
            try:
                _fp0 = self._session_fp(messages)
                _fk = f"{domain}:{_fp0}" if _fp0 else ""
            except Exception:
                _fk = ""
            # 站点报"上下文窗口已达上限"→ 重开对话，并把本轮当新会话：
            # 压缩基准已被作废，跳过压缩才能把完整历史一次性发进新对话
            if self._handle_context_limit(tab, selectors, _fk):
                session_new = True
            if is_continuation and not session_new:
                try:
                    from msg_condense import condense
                    _prev = ((getattr(self, '_fp_last_payload', {}) or {})
                             .get(_fk, '')) if _fk else ''
                    _ref_note = ""
                    if _fk and not _prev:
                        # 上一轮正文是作为附件交付的（内存里没留下全文基准）：
                        # 去本地 uploads 把那个附件读回来比对
                        _hist = ""
                        try:
                            _hist = "\n".join(
                                str(m.get("content") or "")
                                for m in (messages or [])[-12:]
                                if isinstance(m, dict))
                        except Exception:
                            _hist = ""
                        _prev, _ref_note = self._attachment_baseline(
                            _fk, _hist[-200000:])
                        if _prev:
                            logger.info(
                                f"[MsgCondense] 比对基准取自历史附件 "
                                f"{_ref_note}（{len(_prev)} 字符）")
                    elif _fk:
                        # 基准在内存里，但上一轮内容是靠附件交付的：
                        # 占位符同样要指向附件，否则 AI 在正文里找不到
                        _ainfo = ((getattr(self, '_fp_attach', {}) or {})
                                  .get(_fk) or {})
                        _ref_note = _ainfo.get("label") or ""
                    if _prev:
                        # 协议区（工具表+深度思考指令+续传指令）豁免压缩：
                        # 网页 AI 只细读最新消息，工具表被压进占位符后它
                        # 看不到 cmd 等工具声明，就不会调用（上游复现为
                        # "没有可用工具"）。build(protocol_mode=
                        # "always_table") 下 tools 非空必带 protocol；
                        # hint 条件与 is_tool_continuation 一致，
                        # "+2" 对应 parts 间的 "\n\n" 分隔符。
                        _protect = 0
                        if tools:
                            _protect += len(
                                ToolRoundBuilder.tool_protocol(tools)) + 2
                        if think_hint:
                            _protect += len(
                                ToolRoundBuilder.DEEP_THINK_HINT) + 2
                        if is_tool_continuation:
                            _protect += len(
                                ToolRoundBuilder.TOOL_CONTINUATION_HINT) + 2
                        _c = condense(raw_prompt, _prev,
                                      protect_prefix=_protect,
                                      ref_note=_ref_note)
                        if _c != raw_prompt:
                            raw_prompt = _c
                            _condense_applied = True
                            logger.info(
                                f"[MsgCondense] 会话 {_fp0} 压缩生效: "
                                f"{len(_condense_original)} → {len(_c)} 字符"
                                f"（协议区 {_protect} 字符豁免，块级去重"
                                + (f"，省略内容在附件 {_ref_note} 里"
                                   if _ref_note else "") + "）")
                except Exception as _e:
                    logger.debug(f"[MsgCondense] 跳过压缩: {_e}")

            actual_prompt = raw_prompt
            upload_strategy = LongPromptRouter.STRATEGY_DIRECT
            upload_payload = None
            try:
                # 续聊（工具轮次）同样走长文本路由：多轮工具结果累积后
                # prompt 可达数万字，直接打进输入框会超过网页输入上限
                # 导致发送失败，必须落盘为附件
                upload_strategy, upload_payload = LongPromptRouter.decide(raw_prompt, upload_cfg)
            except Exception as e:
                logger.error(f"[LongPrompt] 落盘失败: {e}")
            if is_continuation and upload_strategy != LongPromptRouter.STRATEGY_DIRECT:
                logger.info(f"[ToolRound] 续聊长文本，走 {upload_strategy} 策略")

            if upload_strategy != LongPromptRouter.STRATEGY_DIRECT:
                logger.info(f"[LongPrompt] 策略: {upload_strategy}")
                if upload_strategy == LongPromptRouter.STRATEGY_FILE:
                    tpl = upload_cfg.get(
                        "short_prompt_template",
                        "你是一个具备真实工具调用能力的执行者。请完整阅读我上传的文件 {filename}："
                        "文件末尾的「工具清单」和「调用格式」章节是最高优先级指令——"
                        "这里的工具对你真实可用，需要执行操作时，必须严格按该格式输出 tool_call 代码块，"
                        "绝不要声称“没有可用工具”。如只是闲聊或提问才直接回答。")
                    actual_prompt = tpl.format(filename=upload_payload["filename"])
                else:
                    tpl = upload_cfg.get(
                        "chunked_prompt_template",
                        "你是一个具备真实工具调用能力的执行者。我上传了 {count} 个分片文件，"
                        "请按文件名顺序依次阅读后完成任务：文件末尾的「工具清单」和「调用格式」"
                        "章节是最高优先级指令——这里的工具对你真实可用，需要执行操作时，"
                        "必须严格按该格式输出 tool_call 代码块，绝不要声称“没有可用工具”。")
                    actual_prompt = tpl.format(count=len(upload_payload["files"]))
                # 工具协议必须内联：poe 对长附件的读取不保证完整，
                # 末尾的格式指令可能被忽略，导致模型声称"没有工具"。
                # 与 build() 一致：工具表仅首轮附带（历史无 assistant），
                # 续传轮只内联防污染+格式提示，不重发工具表 JSON。
                _has_history = any(
                    (m.get("role") == "assistant") for m in effective_messages)
                inline_protocol = ""
                if tools:
                    if not _has_history:
                        inline_protocol = ToolRoundBuilder.tool_protocol(tools)
                    else:
                        inline_protocol = ToolRoundBuilder.TOOL_CONTINUATION_HINT
                if inline_protocol:
                    actual_prompt += "\n\n" + inline_protocol
                    logger.info(
                        "[ToolRound] 工具协议已内联到输入框文本"
                        f"（{'首轮含工具表' if not _has_history else '续传轮仅格式提示'}）")

            executor = WorkflowExecutor(tab, self.stealth_mode,
                                        should_stop_checker=self._should_stop_checker)
            # 浏览器实例端口：掉线标记（mark_logged_out）要用
            executor.browser_port = self.port
            executor._active_tools = tools
            executor._active_tool_choice = tool_choice
            executor._already_finished = False

            context = {
                "prompt": actual_prompt,
                "upload_file": None,
                "upload_strategy": upload_strategy,
                "upload_payload": upload_payload,
                "upload_btn_selector": selectors.get("upload_btn") or "",
                "file_input_selector": selectors.get("file_input") or "",
                "upload_done": False,
            }

            workflow_has_explicit_upload = any(
                step.get("action") == "UPLOAD_FILE" for step in workflow
            )

            try:
                # 页面可能还在跑上一次生成（客户端 abort 的孤儿任务），
                # 不停止就发送会被页面忽略
                executor._ensure_page_idle()
                pre_send_state = None
                for step in workflow:
                    if self._should_stop_checker():
                        break

                    if is_continuation:
                        if (step.get("action") == "CLICK"
                                and step.get("target") == "new_chat_btn"):
                            continue
                        # 续聊保持在原会话：不做导航跳转
                        if step.get("action") == "NAVIGATE":
                            continue
                        # 续聊 + 文件策略时必须允许 UPLOAD_FILE 执行，
                        # 否则长 prompt 会直接打进输入框
                        if (step.get("action") == "UPLOAD_FILE"
                                and upload_strategy == LongPromptRouter.STRATEGY_DIRECT):
                            continue

                    action = step.get("action", "")
                    target_key = step.get("target", "")
                    optional = step.get("optional", False)
                    param_value = step.get("value")

                    if action == "UPLOAD_FILE":
                        if upload_strategy == LongPromptRouter.STRATEGY_DIRECT:
                            continue
                        if context["upload_done"]:
                            continue
                        try:
                            self._do_upload(executor, context, upload_cfg,
                                            selectors=selectors,
                                            session_fk=_fk)
                            context["upload_done"] = True
                        except PageUnavailableError:
                            # 页面本身不可用（错误页/未登录）：回退直输同样
                            # 会失败，只是多烧 30s 并掩盖真因，直接上抛
                            raise
                        except ElementNotFoundError as e:
                            # 假阴性复查：chip 实际已渲染时信任附件，
                            # 不刷新（会毁掉已附上的附件）、不回退直输
                            if executor._confirm_upload_false_negative():
                                context["upload_done"] = True
                                continue
                            # 站点无上传能力：回退直接输入，不中断请求。
                            # 上传尝试可能在页面留下残留状态（文件选择
                            # 对话框/半开面板）污染输入区，实测会让后续
                            # 发送按钮点击完全失效——刷新页面清理残留
                            logger.warning(
                                f"[LongPrompt] 上传不可用（{e}），"
                                f"刷新页面清理残留后回退直接输入")
                            try:
                                executor.tab.refresh(timeout=10)
                                time.sleep(3.0)
                            except Exception:
                                pass
                            # 双保险：刷新失败/SPA 未真正重载时附件可能
                            # 仍在页面（2026-11-05 实测"上传了附件又分片"
                            # 的共存场景）。FILE 策略下附件可见即上传
                            # 已成功——信任附件继续，绝不带着附件分片。
                            # （CHUNKED 部分上传不完整，不适用此信任）
                            _fn = getattr(executor, "_last_upload_name", "")
                            if (_fn
                                    and upload_strategy == (
                                        LongPromptRouter.STRATEGY_FILE)
                                    and executor._verify_attachment_visible(
                                        _fn)):
                                logger.warning(
                                    "[LongPrompt] 回退前复查到附件仍在页面，"
                                    f"判定上传已成功: {_fn}，按附件策略继续")
                                context["upload_done"] = True
                                continue
                            executor._clear_stale_attachments()
                            upload_strategy = LongPromptRouter.STRATEGY_DIRECT
                            context["upload_strategy"] = upload_strategy
                            context["upload_payload"] = None
                            context["prompt"] = raw_prompt
                            continue

                    if (action == "FILL_INPUT" and not workflow_has_explicit_upload
                            and not context["upload_done"]
                            and upload_strategy != LongPromptRouter.STRATEGY_DIRECT):
                        try:
                            self._do_upload(executor, context, upload_cfg,
                                            selectors=selectors,
                                            session_fk=_fk)
                            context["upload_done"] = True
                        except PageUnavailableError:
                            raise
                        except ElementNotFoundError as e:
                            # 假阴性复查：chip 实际已渲染时信任附件，
                            # 不刷新（会毁掉已附上的附件）、不回退直输
                            if executor._confirm_upload_false_negative():
                                context["upload_done"] = True
                            else:
                                # 站点无上传能力：回退直接输入，不中断请求
                                logger.warning(
                                    f"[LongPrompt] 上传不可用（{e}），"
                                    f"刷新页面清理残留后回退直接输入")
                                try:
                                    executor.tab.refresh(timeout=10)
                                    time.sleep(3.0)
                                except Exception:
                                    pass
                                # 双保险：刷新失败/SPA 未真正重载时附件
                                # 可能仍在页面。FILE 策略下附件可见即
                                # 上传已成功——信任附件继续正常填发，
                                # 绝不带着附件分片（CHUNKED 部分上传
                                # 不完整，不适用此信任）
                                _fn = getattr(
                                    executor, "_last_upload_name", "")
                                if (_fn
                                        and upload_strategy == (
                                            LongPromptRouter.STRATEGY_FILE)
                                        and executor.
                                        _verify_attachment_visible(_fn)):
                                    logger.warning(
                                        "[LongPrompt] 回退前复查到附件仍在"
                                        f"页面，判定上传已成功: {_fn}，"
                                        "按附件策略继续")
                                    context["upload_done"] = True
                                else:
                                    executor._clear_stale_attachments()
                                    upload_strategy = (
                                        LongPromptRouter.STRATEGY_DIRECT)
                                    context["upload_strategy"] = (
                                        upload_strategy)
                                    context["upload_payload"] = None
                                    context["prompt"] = raw_prompt

                    selector = selectors.get(target_key, "")
                    if not selector and action not in ("WAIT", "KEY_PRESS"):
                        if optional:
                            continue
                        yield self.formatter.pack_error(f"缺少配置: {target_key}",
                                                        code="missing_selector")
                        break

                    # 分片发送接管：上传不可用且消息超过单条上限时，
                    # 跳过单次填充/发送，改走自动切分多片发送
                    # 片长支持站点级配置（site_cfg.CHUNK_CHARS）
                    try:
                        from config_engine import config_engine
                        _site_cfg = (config_engine.sites.get(resolved_domain)
                                     or {}) if resolved_domain else {}
                    except Exception:
                        _site_cfg = {}
                    _chunk_limit = int(_site_cfg.get('CHUNK_CHARS')
                                       or BrowserConstants.get(
                                           'DIRECT_CHUNK_CHARS') or 24000)
                    if (action == "FILL_INPUT"
                            and not context.get("chunked_sent")
                            and upload_strategy == LongPromptRouter.STRATEGY_DIRECT
                            and len(context.get("prompt") or "") > _chunk_limit):
                        logger.info(
                            "[Chunk] 直输超限，跳过单次填充，"
                            "发送时自动切分多片")
                        continue
                    if (action == "CLICK" and target_key == "send_btn"
                            and not context.get("chunked_sent")
                            and upload_strategy == LongPromptRouter.STRATEGY_DIRECT
                            and len(context.get("prompt") or "") > _chunk_limit):
                        yield from self._chunked_direct_send(
                            executor, context, selectors,
                            chunk_limit=_chunk_limit)
                        context["chunked_sent"] = True
                        # 分片逐片校验通过才算送达；成功则记录供下轮压缩
                        _send_confirmed = True
                        continue
                    if (action in ("FILL_INPUT", "CLICK")
                            and context.get("chunked_sent")):
                        # 分片已全部发完，跳过原单次填充/发送步骤
                        continue

                    # 发送前采样，发送后校验：附件超限等情况下发送按钮
                    # 不生效，不校验会进入流式监听无限卡死
                    if action == "CLICK" and target_key == "send_btn":
                        pre_send_state = executor._capture_send_state(
                            selectors.get("input_box", ""),
                            selectors.get("message_wrapper")
                            or selectors.get("result_container") or "")
                        # 流监听同款选择器采"发送前计数"（AI 秒回检测基线）
                        executor._capture_pre_stream_count(
                            selectors.get("result_container") or "")
                        try:
                            # 必须写在 executor 上：_stream_monitor 是
                            # WorkflowExecutor 的方法，读 executor._sent_from_url
                            executor._sent_from_url = executor.tab.url or ''
                        except Exception:
                            executor._sent_from_url = ''
                        # 记录本次发送（无回复时同会话重发一次用；
                        # [Dead] 恢复逻辑在 WorkflowExecutor 上读取）
                        executor._last_send_payload = context.get(
                            "prompt") or ""
                        executor._last_send_input_sel = selectors.get(
                            "input_box", "")
                        executor._last_send_btn_sel = selectors.get(
                            "send_btn", "")

                    try:
                        yield from executor.execute_step(
                            action=action, selector=selector, target_key=target_key,
                            value=param_value, optional=optional, context=context)
                    except (ElementNotFoundError, WorkflowError):
                        break
                    except Exception as e:
                        if not optional:
                            yield self.formatter.pack_error(f"执行中断: {str(e)}")
                            break

                    # 工作流自身点了"新对话"：附件计数归零（新会话没有历史附件），
                    # 压缩基准同时作废——占位符指向的上文与附件都不在新对话里
                    if action == "CLICK" and target_key == "new_chat_btn":
                        if getattr(self, "_conv_upload_counts", None):
                            self._conv_upload_counts.clear()
                        self._invalidate_session_baseline(_fk, "工作流点击新聊天")

                    if (action == "CLICK" and target_key == "send_btn"
                            and pre_send_state is not None):
                        yield from executor._verify_and_recover_send(
                            executor, selectors, pre_send_state)
                        # 校验函数所有失败路径都会抛 WorkflowError，
                        # 走到这里说明发送已确认（或已自动恢复）
                        _send_confirmed = True

            finally:
                if not getattr(executor, "_already_finished", False):
                    yield self.formatter.pack_finish()
                self.tab_manager.release_pinned_tab()
                # 发送确认后记录本会话本轮交付内容（msg_condense 下一轮
                # 的压缩比对基准）。压缩生效时记录未压缩原文——保证前缀
                # 链不断（下一轮仍能对全文取公共前缀）；未压缩时记录实际
                # 发送文本。附件轮次同样记全文：正文虽写进 uploads\prompt_*.txt
                # 而没进会话消息，但内容已经交付过（模型读的是文件），
                # 只记那句"请阅读文件 xxx"的短模板会让下一轮无从比对，
                # 把已交付的几万字整份重发（2026-11-05 现场：续聊 96482 字符
                # 仍走 file 策略）。附件文件名另存一份，内存基准丢了还能
                # 从本地 uploads 读回来比对。
                if _send_confirmed and _fk:
                    try:
                        if not hasattr(self, '_fp_last_payload'):
                            self._fp_last_payload = {}
                        _via_attach = bool(
                            context.get("upload_done")
                            and upload_strategy in (
                                LongPromptRouter.STRATEGY_FILE,
                                LongPromptRouter.STRATEGY_CHUNKED))
                        if _condense_applied or _via_attach:
                            _rec = _condense_original
                        elif context.get("chunked_sent"):
                            # 分片路径：_last_send_payload 是单片（供
                            # [Dead] 重发用），压缩基准要用完整原文
                            _rec = context.get("prompt") or ""
                        else:
                            _rec = (getattr(executor, '_last_send_payload', '')
                                    or context.get("prompt") or "")
                        if _via_attach:
                            try:
                                if not hasattr(self, '_fp_attach'):
                                    self._fp_attach = {}
                                if (upload_strategy
                                        == LongPromptRouter.STRATEGY_FILE):
                                    _names = [str((upload_payload or {})
                                                  .get("filename") or "")]
                                else:
                                    _names = [n for _p, n in
                                              ((upload_payload or {})
                                               .get("files") or [])]
                                _names = [n for n in _names if n]
                                if _names:
                                    self._fp_attach[_fk] = {
                                        "names": _names[:5],
                                        "label": "、".join(_names[-2:])}
                                    while len(self._fp_attach) > 64:
                                        self._fp_attach.pop(
                                            next(iter(self._fp_attach)))
                            except Exception as _ae:
                                logger.debug(
                                    f"[MsgCondense] 附件基准登记失败: {_ae}")
                        if _rec:
                            # 先弹出再插入：活跃会话刷新 LRU 顺序
                            self._fp_last_payload.pop(_fk, None)
                            self._fp_last_payload[_fk] = _rec
                            # 上限 64：OpenClaw 并发子代理多，8 个会把
                            # 正在续聊的主任务指纹淘汰 → 下一轮误判
                            # 新会话点"新聊天"，毁掉续聊会话并全量
                            # 分片重放（2026-11-05 实测）
                            while len(self._fp_last_payload) > 64:
                                self._fp_last_payload.pop(
                                    next(iter(self._fp_last_payload)))
                            logger.info(
                                f"[MsgCondense] 已记录会话 {_fk} 交付文本 "
                                f"{len(_rec)} 字符（下轮压缩比对基准）")
                    except Exception as _e:
                        logger.debug(f"[MsgCondense] 记录失败: {_e}")

        finally:
            SSEFormatter.end_response()

    # 历史附件基准最多读回的字符数（防把超大文件整份拉进比对）
    _ATTACH_BASELINE_MAX = 4_000_000
    # 上一轮附件文件名（短模板里会写"请阅读文件 prompt_xxx.txt"）
    _ATTACH_NAME_RE = r"prompt_\d+_[0-9a-f]{6}(?:_part\d{2}of\d{2})?\.txt"

    def _read_upload_files(self, names: list) -> str:
        """按文件名从 uploads/ 读回历史附件原文（分片按序拼接复原）。
        一个都读不到时返回 ""，调用方退回全量发送。"""
        up_dir = (Path(__file__).resolve().parent / "uploads")
        parts, total = [], 0
        for name in names:
            try:
                fp = up_dir / name
                if not fp.is_file():
                    continue
                data = fp.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                logger.debug(f"[MsgCondense] 历史附件读取失败 {name}: {e}")
                continue
            parts.append(data)
            total += len(data)
            if total >= self._ATTACH_BASELINE_MAX:
                break
        return "".join(parts)

    def _attachment_baseline(self, fk: str, history_text: str) -> tuple:
        """把"上一轮已经作为附件交付过"的原文弄回来当压缩基准。

        附件轮次的正文不在会话消息里，输入框只有"请阅读文件 xxx.txt"一句
        短模板；但内容确实已经交给过模型（它读的是文件）。若因此不比对，
        下一轮就无从压缩，几万字已交付内容被整份重发（2026-11-05 现场：
        续聊 prompt 96482 字符仍走 file 策略）。
        先查本会话记录的附件（内存），没有再从对话历史里认文件名；
        文件已被 uploads 清理或换机器时读不到，退回全量发送。
        返回 (基准文本, 附件说明)。"""
        info = (getattr(self, '_fp_attach', {}) or {}).get(fk) or {}
        names = list(info.get("names") or [])
        label = info.get("label") or ""
        if not names:
            # 对话历史里找：短模板/分片模板都写了文件名，按出现顺序取最近几个
            try:
                found = re.findall(self._ATTACH_NAME_RE, history_text or "")
            except Exception:
                found = []
            names = list(dict.fromkeys(found))[-5:]
            if names:
                label = "、".join(names[-2:])
        if not names:
            return "", ""
        text = self._read_upload_files(names)
        if not text:
            logger.info(
                f"[MsgCondense] 历史附件已不在本地 uploads（已清理或换机），"
                f"本轮按附件比对失败，全量发送: {names}")
            return "", label
        return text, label

    def _invalidate_session_baseline(self, fk: str, why: str = ""):
        """新起对话后必须作废压缩基准。

        占位符的语义是"省略内容与本会话上文／上一轮附件相同"，新对话里两者
        都不存在；不作废就会把几万字历史压成几十个字发进空白对话，模型既
        读不到上文也找不到那个附件，表现为"失忆"。"""
        if not fk:
            return
        for attr in ('_fp_last_payload', '_fp_attach'):
            m = getattr(self, attr, None)
            if isinstance(m, dict) and m.pop(fk, None) is not None:
                logger.info(f"[MsgCondense] 基准作废 {fk}"
                            f"（{why or '会话已重开'}）")

    def _handle_context_limit(self, tab, selectors, fk: str) -> bool:
        """命中站点"上下文窗口已达上限"横幅时：作废基准 + 新起对话。

        这种状态下站点并不拒绝输入——它只是静默丢弃更早的上下文，所以发送
        会"成功"，而模型读不到我们压缩省略的那部分。唯一安全的处置是重开
        对话并让本轮全量重发（IDE 每轮本就重放完整历史，新对话里自洽）。
        命中判定必须"新聊天类按钮 + 近邻容器上限文案"同时成立，否则会把
        侧栏常驻的"新聊天"按钮或我们自己 prompt 里的"上下文"字样误判成上限。
        """
        try:
            st = probe_page_state(tab)
        except Exception:
            return False
        if not st.get("context_full"):
            return False
        logger.warning(
            f"[CtxLimit] 站点提示上下文已达上限 → 新起对话、本轮全量重发："
            f"{str(st.get('limit_text') or '')[:90]}")
        self._invalidate_session_baseline(fk, "上下文已达上限，新起对话")
        if getattr(self, "_conv_upload_counts", None):
            self._conv_upload_counts.clear()
        sel = _plain_css((selectors or {}).get("new_chat_btn", "") or "")
        if not sel:
            logger.warning("[CtxLimit] 站点未配置 new_chat_btn，无法自动重开对话，"
                           "本轮仍停用压缩")
            return True
        try:
            r = tab.run_js(
                "var els=document.querySelectorAll(%s);"
                "for(var i=0;i<els.length;i++){var e=els[i];"
                "if(e.offsetWidth||e.offsetHeight||e.getClientRects().length)"
                "{e.click();return 'clicked';}}"
                "return 'no-visible';" % json.dumps(sel))
            logger.info(f"[CtxLimit] 点击新聊天: {r}")
            time.sleep(1.5)
        except Exception as e:
            logger.warning(f"[CtxLimit] 点击新聊天失败: {e}")
        return True

    def _do_upload(self, executor, context, upload_cfg, selectors=None,
                   session_fk: str = ""):
        """执行上传（单文件/分块），带单会话附件上限保护。

        z.ai 等站点限制单个会话最多 5 个附件；超过上限时自动新起
        对话再上传——长 prompt 已完整写入附件，新对话里模型读文件
        即可续接任务，不会丢上下文。计数 key 用会话指纹（同一 IDE
        会话内累计）：活动 tab 全局共享后按 tab URL 计数会跨会话
        叠加，误触发"新起对话"毁掉正在续聊的会话。"""
        strategy = context["upload_strategy"]
        payload = context["upload_payload"]
        file_input_sel = context["file_input_selector"]
        upload_btn_sel = context["upload_btn_selector"]
        wait_after = float(upload_cfg.get("wait_after_upload", 1.5) or 1.5)

        attach_limit = int(upload_cfg.get(
            "max_attachments_per_conversation", 5) or 5)
        counts = getattr(self, "_conv_upload_counts", None)
        if counts is None:
            counts = self._conv_upload_counts = {}
        needed = (1 if strategy == LongPromptRouter.STRATEGY_FILE
                  else len((payload or {}).get("files") or [1]))
        count_key = session_fk or (getattr(executor.tab, "url", "") or "")
        used = counts.get(count_key, 0)
        if used + needed > attach_limit:
            logger.warning(
                f"[AttachLimit] 当前会话已传 {used} 个附件，"
                f"本请求还需 {needed} 个，超过上限 {attach_limit}"
                " —— 新起对话继续任务")
            new_chat_sel = ""
            if selectors:
                new_chat_sel = selectors.get("new_chat_btn", "")
            if new_chat_sel:
                try:
                    # execute_step 是生成器：必须迭代才执行（同分片发送事故）
                    for _c in executor.execute_step(
                            action="CLICK", selector=new_chat_sel,
                            target_key="new_chat_btn", value=None,
                            optional=True, context=context):
                        pass
                    counts.pop(count_key, None)
                    time.sleep(1.0)
                    # 新对话里没有上文也没有旧附件，压缩基准必须作废
                    self._invalidate_session_baseline(
                        session_fk, "附件超限，新起对话")
                    logger.info(
                        "[AttachLimit] 已新起对话，该会话附件计数归零")
                except Exception as e:
                    logger.warning(f"[AttachLimit] 新建对话失败: {e}")
            else:
                logger.warning("[AttachLimit] 站点未配置 new_chat_btn，"
                               "无法新起对话，按原会话继续尝试")

        if strategy == LongPromptRouter.STRATEGY_FILE:
            executor._execute_upload_file(
                file_input_selector=file_input_sel,
                upload_btn_selector=upload_btn_sel,
                file_path=payload["file_path"],
                optional=False,
                wait_after=wait_after,
            )
            counts[getattr(executor.tab, "url", "") or ""] = used + 1
            logger.info(f"[Upload] 单文件上传完成: {payload['filename']}")
            return

        if strategy == LongPromptRouter.STRATEGY_CHUNKED:
            files = payload["files"]
            uploaded = 0
            for i, (fp, _name) in enumerate(files):
                if self._should_stop_checker():
                    logger.warning("[Upload] 分块上传被取消")
                    break
                try:
                    executor._execute_upload_file(
                        file_input_selector=file_input_sel,
                        upload_btn_selector=upload_btn_sel,
                        file_path=fp,
                        optional=(i > 0),
                        wait_after=wait_after,
                    )
                    uploaded += 1
                except ElementNotFoundError:
                    if i == 0:
                        raise
                    logger.warning(f"[Upload] 分块 {i + 1} 上传失败，跳过")
            counts[getattr(executor.tab, "url", "") or ""] = used + uploaded
            logger.info(f"[Upload] 分块上传完成: {uploaded}/{len(files)}")
            return

    def _chunked_direct_send(self, executor, context, selectors,
                             chunk_limit: Optional[int] = None):
        """直输超限兜底：上传不可用（额度耗尽）且消息超过站点单条上限
        （实测 chatgpt 70K 被拒"你提交的消息过长"）时，把长消息切成
        ≤DIRECT_CHUNK_CHARS 的多条分开发送：
        - 中间片头部声明"消息尚未发完，请勿回复"
        - 最后一片头部声明"消息已全部发送完毕，请统一回复"
        发完后由 workflow 正常进入流式监听，等 AI 统一回复。"""
        prompt = context.get("prompt") or ""
        limit = int(chunk_limit
                    or BrowserConstants.get('DIRECT_CHUNK_CHARS') or 24000)
        # 兜底：页面若残留已附上的附件（验证假阴性误回退/上一轮发送
        # 失败残留），先清理再分片，避免"附件完整附带却还在分片"共存。
        # 2026-11-05 修正：只查 input[type=file].files 检测不到已被
        # 站点接管的 chip（files 被清空、chip 仍在）——文件名仍可见
        # 时同样算残留，一并清理
        try:
            _fn = getattr(executor, "_last_upload_name", "")
            stale = bool(_fn
                         and executor._verify_attachment_visible(_fn))
            if not stale:
                stale = bool(executor.tab.run_js(
                    "var i=document.querySelector('input[type=file]');"
                    "return !!(i&&i.files&&i.files.length>0)"))
            if stale:
                executor._clear_stale_attachments()
        except Exception:
            pass
        chunks = _split_prompt_chunks(prompt, limit)
        n = len(chunks)
        input_sel = selectors.get("input_box", "")
        send_sel = selectors.get("send_btn", "")
        msg_sel = (selectors.get("message_wrapper")
                   or selectors.get("result_container") or "")
        logger.info(
            f"[Chunk] 消息过长（{len(prompt)} 字符 > 单条上限 {limit}），"
            f"切分为 {n} 条分开发送")
        # 流监听同款选择器采"发送前计数"（AI 秒回检测基线，只采一次）
        executor._capture_pre_stream_count(
            selectors.get("result_container")
            or selectors.get("message_wrapper") or "")
        i = 0
        while i < len(chunks):
            ch = chunks[i]
            if self._should_stop_checker():
                logger.warning("[Chunk] 分片发送被取消")
                return
            i += 1
            if i < len(chunks):
                head = (f"[长消息分片 {i}/{n}] 消息尚未发送完，请勿回复，"
                        f"等全部 {n} 片发完后再统一处理。\n\n")
            else:
                head = (f"[长消息分片 {i}/{n}] 以上消息已全部发送完毕，"
                        f"请根据全部内容统一回复。\n\n")
            try:
                executor._execute_fill(input_sel, head + ch,
                                       target_key="input_box",
                                       optional=False, msg_selector=msg_sel)
            except ElementNotFoundError as e:
                raise WorkflowError(f"[Chunk] 分片 {i}/{n} 填充失败: {e}")
            # fill 期间消息已被发出（Enter 发送型站点遇换行提交）：跳过
            # 点击与校验，等生成平息后继续下一片
            if getattr(executor, '_fill_submitted', False):
                logger.info(f"[Chunk] 分片 {i}/{n} 已在填充阶段发出，"
                            "跳过点击")
                executor._last_send_payload = head + ch
                executor._last_send_input_sel = input_sel
                executor._last_send_btn_sel = send_sel
                if i < len(chunks):
                    time.sleep(1.5)
                    self._wait_generation_settled(executor, timeout=90)
                continue
            # 采样必须在 CLICK 之前：小片上屏快，CLICK 后再采样时新 turn
            # 已出现、输入框已清空，pre=post 导致"消息数增加/输入框清空"
            # 两个发送判据全部失效 → 永远误判发送失败
            pre_send_state = executor._capture_send_state(input_sel, msg_sel)
            try:
                executor._sent_from_url = executor.tab.url or ''
            except Exception:
                executor._sent_from_url = ''
            # 记录本次发送（无回复时同会话重发一次用；
            # [Dead] 恢复逻辑在 WorkflowExecutor 上读取）
            executor._last_send_payload = head + ch
            executor._last_send_input_sel = input_sel
            executor._last_send_btn_sel = send_sel
            try:
                # execute_step 是生成器函数：必须迭代才会真正执行
                # （2026-11-05 poe 事故实锤——此前只调用不迭代，分片
                # 发送的 CLICK 从未真正执行，全靠 fill 自动提交碰巧顶上）
                for _err_chunk in executor.execute_step(
                        action="CLICK", selector=send_sel,
                        target_key="send_btn", value=None,
                        optional=False, context=context):
                    if _err_chunk:
                        yield _err_chunk
            except (ElementNotFoundError, WorkflowError) as e:

                raise WorkflowError(f"[Chunk] 分片 {i}/{n} 发送失败: {e}")
            try:
                yield from executor._verify_and_recover_send(
                    executor, selectors, pre_send_state)
            except WorkflowError:
                # 兜底复核：校验信号全灭但对比填充前基线消息数/轮数已
                # 增长 → 实际已发送（信号选择器失效场景，2026-11-04 poe
                # 实测），不能误判失败去全量重放
                time.sleep(1.5)
                _late = executor._capture_send_state(input_sel, msg_sel)
                _grew = (
                    _late.get("msg_count", -1) > pre_send_state.get("msg_count", -1)
                    or _late.get("turn_count", -1)
                    > pre_send_state.get("turn_count", -1))
                if not _grew:
                    raise
                logger.info(
                    f"[Chunk] 分片 {i}/{n} 校验信号未动，但复核确认消息已"
                    f"上屏（msg {_late.get('msg_count')}，"
                    f"turn {_late.get('turn_count')}），继续")
            # 检测 chatgpt 拒收（新版 UI 在消息 turn 内渲染错误面板
            # "你提交的消息过长，请编辑后重新发送"，消息未进入对话）。
            # 只查最后一个 turn：会话历史里的旧拒收面板会让全文检测
            # 永远命中（假阳性 → 无限降半重发）
            time.sleep(2.0)
            rejected = False
            try:
                rejected = bool(executor.tab.run_js(
                    'const ts = document.querySelectorAll('
                    '"[data-turn-key]");'
                    'const t = ts.length ? ts[ts.length - 1] : null;'
                    'return !!t && (t.innerText || "").includes('
                    '"你提交的消息过长");'))
            except Exception:
                pass
            if rejected:
                new_limit = max(2000, len(ch) // 2)
                logger.warning(
                    f"[Chunk] 分片 {i}/{n}（{len(ch)} 字符）被拒收"
                    f"（消息过长），减半至 {new_limit} 重新切分")
                # ch 本身不含分片头（fill 时才拼 head），直接重切；
                # 此前误剥 len(head) 导致每轮拒收丢头 40 字符内容
                re_split = _split_prompt_chunks(
                    ch + "".join(chunks[i:]), new_limit)
                chunks = chunks[:i - 1] + re_split
                n = len(chunks)
                i -= 1
                continue
            logger.info(f"[Chunk] 分片 {i}/{n} 已发送"
                        f"（{len(head + ch)} 字符）")
            if i < len(chunks):
                # 等 AI 对本片的反应平息再发下一片（实测连发太快时
                # chatgpt 三片全发完仍静默零产出；模拟真人分条发消息，
                # AI 中间可能回"收到，请继续"，下一片头部会再次声明）
                time.sleep(1.5)
                self._wait_generation_settled(executor, timeout=90)
        logger.info(f"[Chunk] {len(chunks)} 片全部发送完毕，等待 AI 统一回复")

    def _wait_generation_settled(self, executor, timeout: float = 90):
        """等待 AI 生成停止（stop 按钮消失后静止 3s）或超时。"""
        deadline = time.time() + timeout
        last_active = time.time()
        while time.time() < deadline:
            if self._should_stop_checker():
                return
            try:
                gen = bool(executor.tab.run_js(
                    'return !!document.querySelector('
                    "'[data-testid=\"stop-button\"],"
                    'button[aria-label*="停止"],'
                    'button[aria-label*="Stop"]\');'))
            except Exception:
                gen = False
            if gen:
                last_active = time.time()
            elif time.time() - last_active > 3:
                return
            time.sleep(1.0)

    def _execute_workflow_non_stream(self, messages: List[Dict],
                                     tools: Optional[List[Dict]] = None,
                                     tool_choice: Any = None,
                                     model: Optional[str] = None,
                                     think_hint: bool = False) -> Generator[str, None, None]:
        collected_content: List[str] = []
        collected_tool_calls: List[Dict] = []
        collected_reasoning: List[str] = []
        error_data = None

        for chunk in self._execute_workflow_stream(
            messages, tools=tools, tool_choice=tool_choice, model=model,
            think_hint=think_hint
        ):
            if chunk.startswith("data: [DONE]"):
                continue
            if not chunk.startswith("data: "):
                continue
            try:
                data_str = chunk[6:].strip()
                if not data_str:
                    continue
                data = json.loads(data_str)

                if "error" in data:
                    error_data = data
                    break

                if "choices" in data and data["choices"]:
                    delta = data["choices"][0].get("delta", {}) or {}
                    content = delta.get("content", "")
                    if content:
                        collected_content.append(content)
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        collected_reasoning.append(reasoning)
                    tcs = delta.get("tool_calls")
                    if tcs:
                        collected_tool_calls.extend(tcs)
            except json.JSONDecodeError:
                continue

        if error_data:
            yield json.dumps(error_data, ensure_ascii=False)
            return

        full_content = "".join(collected_content)
        full_reasoning = "".join(collected_reasoning)

        if collected_tool_calls:
            message = {
                "role": "assistant",
                "content": full_content or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    }
                    for tc in collected_tool_calls
                ],
            }
            if full_reasoning:
                message["reasoning_content"] = full_reasoning
                message["reasoning"] = full_reasoning
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": full_content}
            if full_reasoning:
                message["reasoning_content"] = full_reasoning
                message["reasoning"] = full_reasoning
            finish_reason = "stop"

        response = {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "web-browser",
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        yield json.dumps(response, ensure_ascii=False)

    def _check_page_status(self, tab) -> Dict[str, Any]:
        result = {"ready": True, "reason": None}
        try:
            url = tab.url or ""
            if not url or url in ("about:blank", "chrome://newtab/"):
                result["ready"] = False
                result["reason"] = "请先打开目标AI网站"
                return result
            error_indicators = ["chrome-error://", "about:neterror"]
            for indicator in error_indicators:
                if indicator in url:
                    result["ready"] = False
                    result["reason"] = "页面加载错误"
                    return result
            title = (tab.title or "").lower()
            warning_keywords = ["404", "not found", "error", "无法访问", "refused"]
            for keyword in warning_keywords:
                if keyword in title:
                    logger.debug(f"页面可能存在问题: {tab.title}")
                    break
        except Exception as e:
            logger.debug(f"页面状态检查异常: {e}")
        return result

    def close(self):
        logger.info("关闭浏览器连接")
        self._connected = False
        self.page = None
        self._tab_manager = None
        with self._lock:
            BrowserCore._instance = None
            self._initialized = False


_browser_instance: Optional[BrowserCore] = None
_browser_lock = threading.Lock()


def get_browser(port: int = None, auto_connect: bool = True) -> BrowserCore:
    global _browser_instance
    if _browser_instance is not None:
        return _browser_instance
    with _browser_lock:
        if _browser_instance is None:
            instance = BrowserCore(port)
            if auto_connect:
                if not instance.ensure_connection():
                    raise BrowserConnectionError(
                        f"无法连接到浏览器 (端口: {instance.port})"
                    )
            _browser_instance = instance
    return _browser_instance


class _LazyBrowser:
    def __getattr__(self, name):
        return getattr(get_browser(auto_connect=False), name)

    def __call__(self, *args, **kwargs):
        return get_browser(*args, **kwargs)


browser = _LazyBrowser()