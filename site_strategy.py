"""site_strategy.py - 站点级应对策略集中管理

（文件名不叫 site.py：标准库有个 site.py，同名会把它遮蔽，pip/venv 一导入
就炸。这里是被 browser_core 当作"站点策略表"使用的模块，语义一致。）

为什么单独成模块：网页站的"不可用"有很多种，处置方式完全不同，此前这些
判定和处置散落在 browser_core 的各处（错误页守卫、上下文超限重开、登录墙
跳过、额度耗尽标记），改一处容易漏另一处，而且新增站点就得再抄一遍。

这里只做三件事：
1. classify(domain, facts)：把页面事实归一成 SiteState
2. policy(state)         ：该状态应该怎么处置（标记谁、要不要切号、要不要重开对话）
3. AccountStateStore     ：受限/冷却状态的持久化（与积分耗尽记录分开，语义不同）

facts 由 browser_core 的页面探测（_PAGE_STATE_JS）提供：
    {url, err_code, rich_editors, plain_textareas, login_ctas, btn_texts,
     banner, context_full, limit_text, head, tail}
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('site')

# ---------------- 站点状态 ----------------
OK = "ok"
UNREACHABLE = "unreachable"          # 错误页：机器级，切账号无用
NEED_LOGIN = "need_login"            # 登录墙：该 profile 没会话
NO_EDITOR = "no_editor"              # composer 没渲染
CONTEXT_FULL = "context_full"        # 上下文窗口已满：重开对话才有救
USAGE_LIMITED = "usage_limited"      # 账号级配额/频率受限：只能换账号或等

# ---------------- 处置动作 ----------------
ACTION_NONE = "none"
ACTION_NEW_CHAT = "new_chat"         # 重开对话并全量重发
ACTION_ROTATE = "rotate"             # 标记当前账号 + 切下一个账号
ACTION_FAIL = "fail"                 # 直接失败，重试没有意义


class Policy:
    __slots__ = ("mark_limited", "rotate", "new_chat", "cooldown", "resend")

    def __init__(self, mark_limited=False, rotate=False, new_chat=False,
                 cooldown=0.0, resend=True):
        self.mark_limited = mark_limited
        self.rotate = rotate
        self.new_chat = new_chat
        self.cooldown = cooldown      # 标记有效期（秒）
        self.resend = resend          # 切号后是否允许重发本轮内容

    def as_dict(self) -> Dict[str, Any]:
        return {"mark_limited": self.mark_limited, "rotate": self.rotate,
                "new_chat": self.new_chat, "cooldown": self.cooldown,
                "resend": self.resend}


_DEFAULT_DOMAIN = "default"

# ---------------- 各站点的受限文案 ----------------
# 只列"账号级配额/频率"类措辞；上下文超限单独一类（处置不同）。
# 说明：ChatGPT 的每小时上限是英文原文（页面语言无关），其余站点按中英双检。
# default 条目必须存在：陌生站点/空 domain 走到这里若 KeyError，会把整条
# 发送链路带崩（现场：_handle_context_limit 里 except 吞掉后静默失效）。
_GENERIC_USAGE = ["rate limit", "too many requests", "usage limit",
                  "try again later"]
_GENERIC_CONTEXT_FULL = ["上下文窗口已达上限", "context window limit",
                         "reached the context window"]

_USAGE_LIMIT_PATTERNS: Dict[str, List[str]] = {
    "chatgpt.com": [
        "limit of messages per hour",
        "messages per hour",
        "please try again later",
        "you've reached our limit",
        "you have reached our limit",
        "达到我们每小时的消息上限",
        "每小时的消息数已达上限",
        "今日使用上限",
        "usage limit",
    ],
    "chat.deepseek.com": ["已达上限", "usage limit", "rate limit",
                          "too many requests"],
    "grok.com": ["rate limit", "try again later", "daily limit"],
    "chat.z.ai": ["limit reached", "too many requests", "额度"],
    "poe.com": ["rate limit", "try again later"],
    "aistudio.google.com": ["quota", "rate limit", "try again later"],
    "lmarena.ai": ["rate limit", "quota", "try again later"],
    _DEFAULT_DOMAIN: _GENERIC_USAGE,
}

# 「重试」按钮近邻专用：只保留几乎不可能是聊天内容的措辞。
# 这里不放大白名单——近邻容器有 <400 字符上限，短消息正文可能落进来，
# 一条 "usage limit" 的技术回答就足以把好账号打进 1 小时冷却。
_STRONG_USAGE_PATTERNS: Dict[str, List[str]] = {
    "chatgpt.com": [
        "limit of messages per hour",
        "you've reached our limit",
        "you have reached our limit",
        "达到我们每小时的消息上限",
        "每小时的消息数已达上限",
    ],
    _DEFAULT_DOMAIN: ["too many requests", "limit of messages per hour",
                      "daily limit reached", "usage limit reached"],
}

# 上下文窗口已满（与"账号受限"处置不同：换账号无用，重开对话才有救）
_CONTEXT_FULL_PATTERNS: Dict[str, List[str]] = {
    "chatgpt.com": ["上下文窗口已达上限", "context window limit"],
    _DEFAULT_DOMAIN: _GENERIC_CONTEXT_FULL,
}

# 冷却时长：受限文案里通常不带恢复时间，按站点给保守值
_COOLDOWN: Dict[str, float] = {
    "chatgpt.com": 3600.0,     # 每小时上限 → 1 小时
    _DEFAULT_DOMAIN: 1800.0,
}

# WAPI_STATE_DIR 只给测试用：单测不得把受限冷却写进生产运行时状态
_STATE_FILE = os.path.join(os.environ.get("WAPI_STATE_DIR")
                           or os.path.dirname(os.path.abspath(__file__)),
                           "site_state.json")


def host_of(domain: str) -> str:
    """'https://chatgpt.com/c/x?y=1' / 'chatgpt.com' → 'chatgpt.com'。
    文案表、冷却表、受限登记 key 都走这个归一，避免同一账号被记成两条。"""
    d = str(domain or "").strip().lower()
    for scheme in ("https://", "http://", "www."):
        if d.startswith(scheme):
            d = d[len(scheme):]
    return d.split("/")[0].split("?")[0]


def _lookup(domain: str, table: Dict[str, Any]) -> Any:
    """按 host 命中表项，支持子域（chat.deepseek.com 命中 deepseek.com）。"""
    d = host_of(domain)
    for key, val in table.items():
        if key != _DEFAULT_DOMAIN and (d == key or d.endswith("." + key)):
            return val
    return table[_DEFAULT_DOMAIN]


def _patterns(domain: str, table: Dict[str, List[str]]) -> List[str]:
    return _lookup(domain, table)


def _match(haystack: str, needles: List[str]) -> Optional[str]:
    low = (haystack or "").lower()
    for n in needles:
        if n and n.lower() in low:
            return n
    return None


def classify(domain: str, facts: Dict[str, Any]) -> str:
    """页面事实 → 站点状态。判定顺序即优先级。

    语料只用站点自己的提示区，绝不吃整页正文：正文里混着我们发出的 prompt
    （里面常出现 rate limit / 上下文 之类的字样），拿它判定会把正常轮次误标
    成受限，进而把好账号打进冷却。两类语料的可信度不同，配的文案也不同：
      banner + btn_texts —— alert/status/aria-live/页脚，站点提示区，文案表全用
      limit_hits        —— 「重试」按钮的近邻容器，可能是短的消息文本，只认
                          无歧义的配额措辞（STRONG 表）
    """
    if not isinstance(facts, dict):
        return OK
    url = str(facts.get("url") or "")
    if (url.startswith("chrome-error://") or url.startswith("about:neterror")
            or facts.get("err_code")):
        return UNREACHABLE
    chrome = " ".join([str(facts.get(k) or "") for k in
                       ("banner", "btn_texts")])
    near = " ".join(str(x) for x in (facts.get("limit_hits") or []))
    hit = (_match(chrome, _patterns(domain, _USAGE_LIMIT_PATTERNS))
           or _match(near, _patterns(domain, _STRONG_USAGE_PATTERNS)))
    if hit:
        logger.info(f"[Site] {domain} 命中账号受限文案: {hit!r}")
        return USAGE_LIMITED
    if facts.get("context_full") or _match(
            chrome, _patterns(domain, _CONTEXT_FULL_PATTERNS)):
        return CONTEXT_FULL
    if facts.get("login_ctas") and not facts.get("rich_editors"):
        return NEED_LOGIN
    if not (facts.get("rich_editors") or facts.get("plain_textareas")):
        return NO_EDITOR
    return OK


def policy(state: str, domain: str = "") -> Optional[Policy]:
    """状态 → 处置策略。

    账号受限：标记 + 切号，切过去之后**不要**把整段巨型内容重发多次；
    上下文已满：重开对话 + 全量重发，但不标记账号（不是账号的问题）；
    未登录：切号有意义（别的 profile 可能登录着）；不可达：机器级，切号无用。
    """
    cool = _lookup(domain, _COOLDOWN)
    if state == USAGE_LIMITED:
        return Policy(mark_limited=True, rotate=True, new_chat=False,
                      cooldown=cool, resend=True)
    if state == NEED_LOGIN:
        return Policy(mark_limited=False, rotate=True, new_chat=False,
                      cooldown=0, resend=True)
    if state == CONTEXT_FULL:
        return Policy(mark_limited=False, rotate=False, new_chat=True,
                      cooldown=0, resend=True)
    if state in (UNREACHABLE, NO_EDITOR):
        return Policy(mark_limited=False, rotate=False, new_chat=False,
                      cooldown=0, resend=False)
    return None


def cooldown_for(domain: str) -> float:
    return _lookup(domain, _COOLDOWN)


class AccountStateStore:
    """带命名空间的到期时间登记表：port + domain (+ cap) → 解除时间戳。

    与 CreditStateStore（积分耗尽、按天重置）分开：这里到期就自动恢复，
    不该污染积分语义，也不该由人工"清除积分"顺带清掉。
    namespace 用来区分登记种类：空 = 账号受限；"cap" = 站点能力缺失
    （例如上传口被禁用）。两者语义相同（一段时间内别再试），实现复用一份。"""

    _lock = threading.Lock()

    def __init__(self, path: str = _STATE_FILE, namespace: str = ""):
        self.path = path
        self.namespace = namespace
        self._data: Dict[str, float] = {}
        self._load()

    def _key(self, domain: str, port) -> str:
        base = f"{host_of(domain)}|{port}"
        return f"{self.namespace}|{base}" if self.namespace else base

    def _load(self):
        try:
            if os.path.isfile(self.path):
                raw = json.load(open(self.path, encoding="utf-8"))
                self._data = {k: float(v) for k, v in (raw or {}).items()}
        except Exception as e:
            logger.warning(f"[Site] 受限状态读取失败: {e}")
            self._data = {}

    def _save(self):
        try:
            now = time.time()
            self._data = {k: v for k, v in self._data.items() if v > now}
            json.dump(self._data, open(self.path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning(f"[Site] 受限状态写入失败: {e}")

    def mark_limited(self, domain: str, port, seconds: float,
                     what: str = "账号受限") -> float:
        until = time.time() + max(60.0, seconds)
        with self._lock:
            self._load()
            self._data[self._key(domain, port)] = until
            self._save()
        logger.warning(f"[Site] {what}已标记 domain={domain} port={port} "
                       f"解除时间 {time.strftime('%m-%d %H:%M', time.localtime(until))}")
        return until

    def is_limited(self, domain: str, port) -> bool:
        with self._lock:
            self._load()
            return self._data.get(self._key(domain, port), 0) > time.time()

    def remaining(self, domain: str, port) -> float:
        return max(0.0, self._data.get(self._key(domain, port), 0) - time.time())

    def until(self, domain: str, port) -> float:
        """解除时间戳；不在冷却中返回 0（仪表盘只关心"还要等多久"）。"""
        v = self._data.get(self._key(domain, port), 0)
        return v if v > time.time() else 0.0

    def clear(self, domain: str, port) -> bool:
        with self._lock:
            self._load()
            if self._data.pop(self._key(domain, port), None) is not None:
                self._save()
                return True
        return False

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        return {k: time.strftime('%m-%d %H:%M', time.localtime(v))
                for k, v in self._data.items() if v > now}


account_state = AccountStateStore()

# ---------------- 站点能力缺失登记 ----------------
# "这个账号此刻没有上传权限"和"这个账号受限"是两件事：受限要切号，
# 能力缺失不一定要切号（别的账号可能同样没有），但**必须提前知道**，
# 否则每次长 prompt 都要先花 ~12s 试着上传、被站点静默丢弃、再刷新页面
# 回退直输（2026-11-06 实测该账号 4 个 input[type=file] 全部 disabled）。
# 登记成一段时间内"别再试"，到期自动恢复——站点随时可能把功能放回来。
CAP_UPLOAD = "upload"

_CAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "site_caps.json")

_CAP_COOLDOWN: Dict[str, float] = {
    CAP_UPLOAD: 900.0,      # 15 分钟内不再尝试上传（站点侧开关通常更久不变）
}

capability_state = AccountStateStore(path=_CAP_FILE, namespace="cap")


def cap_cooldown(cap: str) -> float:
    return float(_CAP_COOLDOWN.get(cap, 900.0))


def _cap_domain(cap: str, domain: str) -> str:
    """把能力名并进 domain 维度：同一账号的不同能力各自计时。

    用 "@" 而不是 "|" 分隔：AccountStateStore 的 key 本身以 "|" 分隔
    domain/port，再在 domain 里塞 "|" 会让面板反解出错误的站点名。"""
    return f"{cap}@{host_of(domain)}"


def mark_capability_missing(cap: str, domain: str, port,
                            seconds: Optional[float] = None) -> float:
    return capability_state.mark_limited(
        _cap_domain(cap, domain), port,
        seconds if seconds is not None else cap_cooldown(cap),
        what=f"缺能力[{cap}]")


def capability_ok(cap: str, domain: str, port) -> bool:
    return not capability_state.is_limited(_cap_domain(cap, domain), port)


def capability_until(cap: str, domain: str, port) -> float:
    """该能力何时恢复（0 = 当前没有被标记为缺失）"""
    return capability_state.until(_cap_domain(cap, domain), port)


def capability_snapshot() -> Dict[str, Any]:
    return capability_state.snapshot()
