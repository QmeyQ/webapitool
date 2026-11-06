"""prompt_packager.py — 「消息文本 → 压缩 → 打包策略」这条链的唯一归属地

为什么要有这个类（2026-11-06 现场复盘）：
这条链原先散在 browser_core.execute_workflow 的三段里——拼 prompt 一段、
压缩一段、路由一段，中间还夹着体量拦截与"工具协议内联"。三段各自记着
一半规则，于是反复出现"修好这里坏那里"：

  * 压缩是按 fk（domain:指纹:对话）找基准的。IDE 换一个会话（指纹变）
    但网页端还在同一个对话里，基准就找不到 → 整份不压缩 → 218,684 字符
    原样落盘当附件（uploads 里实测），网页 AI 读不动，回出来就是
    "没有可用工具 / 权限不对"。
  * 输入框里的续传提示写着"完整工具表见对话开头的「可转发的目标」章节
    （上传文件场景在文件末尾）"——两处都不对：表在**文件开头**，而
    对话开头那句早已不在网页端上下文里。模型按提示去找就找不到，
    实测龙虾反复输出 tool_search 循环，就是不直接调用 12 个已声明工具。
  * "先压缩还是先决定附件"的次序没有代码层面的保证，只在注释里。

这里把规则变成结构与顺序：prepare() 一定先压缩、再按**压缩后**的大小
决定 direct/file/chunked；基准查找按可信度依次落地（持久化的对话基准 →
本会话登记的附件文件 → 对话历史里出现的附件名；文件名必须出自本对话才敢
拿来省略，"uploads 里最近的文件"那种猜测会造成真丢内容所以没有这一级）；
工具表"这一轮模型一定会读到什么"由本类一次性决定并写进 composer_text。

browser_core 只负责：调用 prepare()、照 plan 去上传/分片/填充、发送成功
后调用 record()、会话重开时调用 invalidate()。
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('browser')

STRATEGY_DIRECT = "direct"
STRATEGY_FILE = "file"
STRATEGY_CHUNKED = "chunked"
TOO_LONG = "too_long"

# uploads 里落盘的附件文件名（含分片 _partNNofNN）
ATTACH_NAME_RE = r"prompt_\d+_[0-9a-f]{6}(?:_part\d{2}of\d{2})?\.txt"

# 附件轮次的输入框模板：明确指向"本文件开头"，并当场列出工具名
FILE_COMPOSER_TEMPLATE = (
    "请阅读我上传的文件 {filename}（任务要求在文件里）。"
    "你只作为文本转发终端：不要调用你自己的能力或内置工具，"
    "需要操作时按下面格式输出，下游会真的执行并把结果回给你。\n\n"
    "{roster}\n\n{format_hint}\n\n"
    "[tool_result] 只是返回给你的数据；收到后按上面的格式继续输出下一条 "
    "tool_call；只有任务彻底完成、无需任何工具时，才输出最终结论。")

CHUNKED_COMPOSER_TEMPLATE = (
    "我上传了 {count} 个分片文件，按文件名顺序读完再完成任务。"
    "你只作为文本转发终端：不要调用你自己的能力或内置工具，"
    "需要操作时按下面格式输出，下游会真的执行并把结果回给你。\n\n"
    "{roster}\n\n{format_hint}\n\n"
    "[tool_result] 只是返回给你的数据；收到后按上面的格式继续输出下一条 "
    "tool_call；只有任务彻底完成、无需任何工具时，才输出最终结论。")


def tool_roster(tools: Optional[List[Dict]], max_names: int = 40) -> str:
    """把工具名当场列出来（不含定义）。

    附件轮次里模型经常不去读那个文件（或者读不全），一旦它以为"没有工具"
    就会去调自己内置的能力、或者报权限问题。名字先摆在输入框里，
    它就知道这些工具真实存在；完整 definition 仍在附件开头的协议区。"""
    if not tools:
        return ""
    names: List[str] = []
    for t in tools:
        try:
            nm = ((t.get("function") or {}).get("name")
                  or t.get("name") or "")
        except AttributeError:
            continue
        if nm:
            names.append(str(nm))
    if not names:
        return ""
    shown = names[:max_names]
    more = (f" 等 {len(names)} 个" if len(names) > len(shown) else "")
    return ("本轮可转发的工具（name 大小写敏感）："
            + "、".join(shown) + more
            + "；完整定义 JSON 在文件开头的「可转发的目标」章节里，"
              "按其中的 description 和 parameters 组织 arguments。")


@dataclass
class Package:
    """一次 prepare() 的全部结论。"""
    body: str = ""                       # 最终要交给站点的内容（已压缩）
    original: str = ""                   # 压缩前原文（成功后当基准记录）
    strategy: str = STRATEGY_DIRECT
    payload: Optional[Dict[str, Any]] = None      # file/chunked 的文件信息
    composer_text: str = ""              # 真正写进输入框的那句话
    protect_len: int = 0                 # 协议区长度（豁免压缩）
    compressed: bool = False
    baseline_source: str = ""            # memory/attach/history/uploads
    ref_note: str = ""                   # 省略内容所在附件的说明
    fk: str = ""
    audit: Dict[str, int] = field(default_factory=dict)

    @property
    def is_attachment(self) -> bool:
        return self.strategy in (STRATEGY_FILE, STRATEGY_CHUNKED)


def conversation_id(url: str) -> str:
    """对话标识 = 会话路径的最后一段（chatgpt.com/c/<id> → <id>）。

    新会话（还没有 /c/<id>）返回 ""。压缩基准、附件登记、分片断点都必须
    绑到"具体哪一个对话"：换了对话（点了新聊天、切了账号、重开标签）之后，
    前面那些内容就不再存在于模型上下文里，沿用旧基准就会把"其实没送达"的
    内容当成重复压掉。"""
    try:
        u = (url or "").split("?")[0].split("#")[0]
        tail = u.split("://", 1)[-1]
        path = tail.split("/", 1)[1] if "/" in tail else ""
        segs = [s for s in path.split("/") if s]
        return segs[-1][:60] if segs else ""
    except Exception:
        return ""


def conv_key(domain: str, tab_url: str) -> str:
    """会话状态的唯一键位：`站点:对话id`。

    刻意**不含 IDE 会话指纹**：指纹每轮都可能变（最后一条消息一变就变），
    按指纹存基准就会让"续传轮"永远找不到基准，工作流于是点「新聊天」把
    前几轮刚交付的几万字整个丢掉，再把全部内容重发一遍——2026-11-06 现场
    那份 218,684 字符未压缩附件就是这么来的，模型读不动，上游表现为
    "没有可用工具/权限不对"。
    共用同一个网页对话也不会串味：压缩只会省略**逐字相同**的段落，
    基准对不上时压不动，最坏就是全量发送，绝不会凭空省略内容。"""
    cid = conversation_id(tab_url)
    d = (domain or "").lower()
    return f"{d}:{cid}" if cid else d


class PromptPackager:
    """压缩基准、附件登记、分片进度与打包决策的统一持有者。

    键位是关键设计：**基准按"网页端对话"存，不按 IDE 会话指纹存**。
    占位符的语义是"这段内容就在**这个对话**里已经交付过"，而 IDE 换一个
    会话（指纹变）时网页端还停在同一个对话里——按指纹存基准就会找不到，
    于是整份不压缩、218,684 字符原样落盘当附件（2026-11-06 实测），
    网页 AI 读不动，回出来就是"没有可用工具 / 权限不对"。
    状态持久化到 pack_state.json：服务重启后同一对话仍能继续压缩，
    不再"每次重启都全量重发一遍"。
    """

    _STATE_FILE = os.path.join(
        os.environ.get("WAPI_STATE_DIR")
        or os.path.dirname(os.path.abspath(__file__)),
        "pack_state.json")

    def __init__(self, upload_dir: Optional[str] = None,
                 state_file: Optional[str] = None,
                 max_persisted: int = 400_000):
        self.baseline: Dict[str, str] = {}          # ckey → 已交付原文
        self.attachments: Dict[str, Dict] = {}      # ckey → {"names","label"}
        self.chunk_progress: Dict[str, Dict] = {}   # ckey → {"sent","n","conv","ts"}
        self.upload_dir = Path(upload_dir
                               or Path(__file__).resolve().parent / "uploads")
        self.state_file = state_file or self._STATE_FILE
        self.max_persisted = max_persisted
        self._load()

    # ---------------- 持久化 ----------------

    def _load(self) -> None:
        try:
            if not os.path.isfile(self.state_file):
                return
            raw = json.load(open(self.state_file, encoding="utf-8"))
            for k, v in (raw or {}).items():
                if not isinstance(v, dict):
                    continue
                text = str(v.get("original") or "")
                if text:
                    self.baseline[k] = text
                if isinstance(v.get("attach"), dict):
                    self.attachments[k] = v["attach"]
        except Exception as e:
            logger.warning(f"[Packager] 基准状态读取失败（按冷启动处理）: {e}")
            self.baseline = {}

    def _save(self) -> None:
        try:
            keep: Dict[str, Dict] = {}
            for k, text in list(self.baseline.items())[-12:]:
                if len(text) <= self.max_persisted:
                    keep[k] = {"original": text,
                               "attach": self.attachments.get(k) or {},
                               "ts": time.time()}
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning(f"[Packager] 基准状态写入失败: {e}")

    # ---------------- 基准查找 ----------------

    @staticmethod
    def _conv_ok(ckey: str) -> bool:
        """键里必须有真实对话 id 才允许存/用基准。

        根地址（chatgpt.com/ 还没 /c/<id>）不是一个可依赖的上下文标识：
        在它上面记基准，下一个同样停在根地址的新会话会把自己的内容当成
        "上一轮已交付"压掉——那是真的丢内容，不是压得少。"""
        return bool(ckey) and ":" in ckey and bool(ckey.split(":", 1)[1])

    def find_baseline(self, ckey: str,
                      history_text: str = "") -> Tuple[str, str, str]:
        """返回 (基准文本, 附件说明, 来源)。找不到就返回 ("", "", "")，
        调用方据此全量发送——宁多不漏，绝不凭空缺内容。

        顺序即可信度：
          1 state/memory —— 该网页对话上一轮交付的原文（本进程记的或
                             pack_state.json 里恢复的）
          2 attach       —— 该对话登记过的附件文件名，从 uploads 读回原文
          3 history      —— 对话历史里出现过的附件文件名（服务重启、登记
                             丢了也能认回来；文件名出自本对话的历史，
                             所以它一定属于这个对话）

        刻意**没有**"拿 uploads 里最近的文件当基准"这一级：文件本身不带着
        对话标识，OpenClaw 并发子代理时最近的文件很可能属于另一个对话，
        拿它比对就会把这个对话没送达过的内容当成重复省略掉——那是丢内容，
        不是压得少。基准原文已经随 pack_state.json 持久化，重启也不丢。
        """
        prev = self.baseline.get(ckey) or "" if self._conv_ok(ckey) else ""
        if prev:
            src = "memory" if (self.attachments.get(ckey) is None) else "state"
            return prev, (self.attachments.get(ckey) or {}).get("label") or "", src
        names, label = self._names_for(ckey, history_text)
        if names and self._conv_ok(ckey):
            text = self._read_files(names)
            if text:
                return text, label, ("attach" if ckey in self.attachments
                                     else "history")
        return "", "", ""

    def _names_for(self, ckey: str, history_text: str) -> Tuple[List[str], str]:
        info = self.attachments.get(ckey) or {}
        names = [str(n) for n in (info.get("names") or []) if n]
        label = info.get("label") or ""
        if names:
            return names, label
        try:
            found = re.findall(ATTACH_NAME_RE, history_text or "")
        except Exception:
            found = []
        names = list(dict.fromkeys(found))[-6:]
        if names:
            label = "、".join(names[-2:])
        return names, label

    def _read_files(self, names: List[str]) -> str:
        """按文件名读回附件原文。

        分片文件是"把整份内容按序切开"落盘的，拼接时必须不带任何分隔符：
        多插一个换行就让边界处的逐字比对失败，白白让整段压不动。"""
        out, total = [], 0
        limit = 4_000_000
        for name in names:
            try:
                p = self.upload_dir / str(name)
                if not p.is_file():
                    continue
                s = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            out.append(s)
            total += len(s)
            if total >= limit:
                break
        return "".join(out)

    # ---------------- 主流程 ----------------

    def prepare(self, *, build_text, messages: List[Dict],
                tools: Optional[List[Dict]], think_hint: bool,
                continuation: bool, ckey: str, history_text: str = "",
                upload_cfg: Optional[Dict] = None,
                chunk_limit: int = 24000,
                max_chunks: int = 30) -> Package:
        """一定先压缩，再按压缩后的大小决定打包方式。

        build_text(effective_messages, protocol_in_body) 由调用方提供
        （它才知道 ToolRoundBuilder 的那些细节），本类只管顺序与判定。
        protocol_in_body=False 时拼出来的文本不含工具表——用于
        "附件里已带工具表、输入框只要短模板"的轮次。"""
        upload_cfg = upload_cfg or {}
        original = build_text(messages, True)
        pkg = Package(body=original, original=original, fk=ckey,
                      protect_len=self._protect_len(
                          original, messages, tools, think_hint))

        # —— 1. 压缩（只在续聊同一对话时有意义）——
        if continuation and ckey:
            prev, ref_note, src = self.find_baseline(ckey, history_text)
            pkg.baseline_source = src
            pkg.ref_note = ref_note
            if prev:
                try:
                    from msg_condense import condense
                    _c = condense(original, prev,
                                  protect_prefix=pkg.protect_len,
                                  ref_note=ref_note)
                except Exception as e:
                    logger.warning(f"[Packager] 压缩异常，按原文发送: {e}")
                    _c = original
                if _c != original:
                    pkg.body = _c
                    pkg.compressed = True
                    logger.info(
                        f"[Packager] 压缩生效（基准来源={src}）: "
                        f"{len(original)} → {len(_c)} 字符"
                        + (f"，省略内容在附件 {ref_note} 里" if ref_note else ""))
                else:
                    logger.info(f"[Packager] 无可压缩段（基准来源={src}，"
                                f"{len(original)} 字符）")

        # —— 2. 按压缩后的大小决定策略 ——
        size = len(pkg.body)
        max_len = int(upload_cfg.get("max_prompt_length", 2000) or 2000)
        max_file = int(upload_cfg.get("max_file_size", 5 * 1024 * 1024)
                       or 5 * 1024 * 1024)
        enabled = upload_cfg.get("enabled", True)
        n_chunks = -(-size // max(1, chunk_limit))
        if size > max_len and enabled:
            if size * 2 > max_file:            # 字节数粗估（utf-8 中文约 3x）
                pkg.strategy = STRATEGY_CHUNKED if n_chunks <= max_chunks \
                    else TOO_LONG
            else:
                pkg.strategy = STRATEGY_FILE
        else:
            pkg.strategy = STRATEGY_DIRECT
        if pkg.strategy == TOO_LONG:
            raise TooLongPlan(
                f"[Packager] 压缩后仍有 {size} 字符，需切约 {n_chunks} 片，"
                f"超过单请求最多 {max_chunks} 片的上限——"
                "网页端承载不了这种体量，请缩减上下文或让它走文件而非常驻历史")
        pkg.audit = {"original": len(original), "body": size,
                     "chunks": n_chunks, "protect": pkg.protect_len}
        logger.info(f"[Packager] 策略={pkg.strategy} 原文={len(original)} "
                    f"交付={size} 片数约={n_chunks} 基准={pkg.baseline_source or '无'}")

        # —— 3. 输入框文本（附件轮次：短模板 + 工具名清单 + 格式提示）——
        pkg.composer_text = self._composer_text(pkg, messages, tools,
                                                upload_cfg, size)
        return pkg

    def _composer_text(self, pkg: Package, messages: List[Dict],
                       tools: Optional[List[Dict]], upload_cfg: Dict,
                       size: int) -> str:
        from browser_core import ToolRoundBuilder as T
        roster = tool_roster(tools)
        if pkg.strategy == STRATEGY_FILE:
            tpl = (upload_cfg.get("file_composer_template")
                   or FILE_COMPOSER_TEMPLATE)
            return tpl.format(filename=(pkg.payload or {}).get("filename", ""),
                              roster=roster or "（本轮无工具声明）",
                              format_hint=T.TOOL_FORMAT_HINT)
        if pkg.strategy == STRATEGY_CHUNKED:
            tpl = (upload_cfg.get("chunked_composer_template")
                   or CHUNKED_COMPOSER_TEMPLATE)
            return tpl.format(count=len((pkg.payload or {}).get("files") or []),
                              roster=roster or "（本轮无工具声明）",
                              format_hint=T.TOOL_FORMAT_HINT)
        return pkg.body

    def _protect_len(self, text: str, messages: List[Dict],
                     tools: Optional[List[Dict]], think_hint: bool) -> int:
        """协议区长度 = 正文里第一个消息标记（[user] / [assistant] /
        [tool_result]）之前的字符数，这部分逐字豁免压缩。

        build() 把协议放在最前、消息在后，所以这个位置就是协议区的边界；
        按文本判定而不是按"工具表长度 + 各提示长度"累加，是因为累加那份
        公式要时刻跟着 build() 的分隔符改，一改就漏（历史上漏掉过
        DEEP_THINK_HINT，表现为工具表被压没，模型回答"没有可用工具"）。"""
        head = text[:60000]
        positions = [i for i in (head.find("\n[user]"), head.find("[user]"),
                                 head.find("\n[assistant]"),
                                 head.find("[assistant]"),
                                 head.find("[tool_result")) if i >= 0]
        return min(positions) if positions else 0

    # ---------------- 记录与作废 ----------------

    def attach_payload(self, ckey: str, pkg: Package, payload: Dict,
                       strategy: str) -> None:
        """路由把落盘文件信息给我之后，补齐 composer 文本与附件登记。"""
        pkg.payload = payload
        pkg.strategy = strategy
        pkg.composer_text = self._composer_text(pkg, [], None, {}, len(pkg.body))
        names = ([str(payload.get("filename") or "")]
                 if strategy == STRATEGY_FILE
                 else [str(n) for _p, n in (payload.get("files") or [])])
        names = [x for x in names if x]
        if ckey and names:
            self.attachments[ckey] = {"names": names[:6],
                                      "label": "、".join(names[-2:])}

    def record(self, ckey: str, pkg: Package) -> None:
        """发送成功后把**交付原文**（压缩前）当基准。

        附件轮次也必须记原文：模型读的是文件，内容确实交付过；记成
        "请阅读文件 xxx" 那句短模板的话，下一轮比对基准就是一句空话，
        几万字永远压不动（2026-11-05 现场）。"""
        if not ckey or not pkg.original or not self._conv_ok(ckey):
            return
        self.baseline.pop(ckey, None)      # 活跃对话挪到尾部（LRU 顺序）
        self.baseline[ckey] = pkg.original
        self.chunk_progress.pop(ckey, None)
        # 上限 64：OpenClaw 并发子代理多，容量太小会把正在续聊的主任务
        # 挤掉，下一轮误判新会话点"新聊天"，毁掉续聊会话并全量重放
        while len(self.baseline) > 64:
            self.baseline.pop(next(iter(self.baseline)))
        while len(self.attachments) > 64:
            self.attachments.pop(next(iter(self.attachments)))
        self._save()
        logger.info(f"[Packager] 已记录 {ckey} 交付原文 "
                    f"{len(pkg.original)} 字符（下轮压缩基准，"
                    f"来源={pkg.baseline_source or '无'}）")

    def migrate(self, old_key: str, new_key: str) -> None:
        """把发送前登记的附件/断点迁到发送后的真实对话键上。

        新会话是在发出第一条消息后 URL 才出现 /c/<id>，不迁移的话
        下一轮按真实对话键找不到附件登记，比对链就断了。"""
        if not old_key or not new_key or old_key == new_key:
            return
        for table in (self.baseline, self.attachments, self.chunk_progress):
            if old_key in table:
                table.setdefault(new_key, table.pop(old_key))

    def invalidate(self, ckey: str, why: str = "") -> List[str]:
        """会话重开 = 一切"已交付"知识作废（基准/附件登记/分片断点）。

        否则占位符指向虚空、断点续传从第 N 片起发而前 N-1 片根本不在会话里，
        表现为"失忆"或全量重发死循环。"""
        if not ckey:
            return []
        dropped = []
        for name, table in (("baseline", self.baseline),
                            ("attachments", self.attachments),
                            ("chunk_progress", self.chunk_progress)):
            if table.pop(ckey, None) is not None:
                dropped.append(name)
        if dropped:
            logger.info(f"[Packager] 作废 {ckey} 的 {','.join(dropped)}"
                        f"（{why or '会话已重开'}）")
        self._save()
        return dropped

    # ---------------- 分片断点 ----------------

    def set_progress(self, ckey: str, sent: int, n: int, conv: str) -> None:
        if ckey:
            self.chunk_progress[ckey] = {"sent": sent, "n": n, "conv": conv,
                                        "ts": time.time()}

    def progress(self, ckey: str, n: Optional[int] = None,
                 conv: str = "", max_age: float = 1800.0) -> int:
        """有效断点返回已送达片数，否则 0（从头发，宁多不漏）。"""
        prog = (self.chunk_progress.get(ckey) or {}) if ckey else {}
        if not prog or time.time() - prog.get("ts", 0) > max_age:
            return 0
        if n is not None and prog.get("n") != n:
            return 0
        if conv and prog.get("conv") and prog.get("conv") != conv:
            logger.warning(f"[Packager] 断点属于另一个会话"
                           f"（{str(prog.get('conv'))[:40]}），忽略进度从头发送")
            self.chunk_progress.pop(ckey, None)
            return 0
        return int(prog.get("sent") or 0)


class TooLongPlan(Exception):
    """压缩后仍然装不进网页端：由调用方翻成 TooLongError 上报"""
