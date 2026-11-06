"""
msg_condense.py - 会话消息压缩模块（纯函数，无副作用）

背景：IDE 每轮请求都会重放完整对话历史（系统规则 + 工具调用轮次），
序列化后的 prompt 数万字且与上一轮大量重复，导致每条消息都要分片。

算法（全部内容拼接完成后，从头到尾线性扫描一遍）：
- 命中"与已交付内容逐字相同"的连续片段 → 压成 `首两字..末两字`
- 不相同的片段 → 原样留在原位
- 相同段可以有很多段，**中间有差异不影响后面的相同段继续被压缩**
- 判定基于逐字相等（先确认整段 min_run 相等，再指数+二分找出最长公共
  前缀），因此不会把"不同的内容"压掉

与旧实现的区别（2026-11-06 重写）：
1. 旧实现是"头部公共前缀 + 行对齐块级去重"两级：行匹配要求整行逐字相等且
   行长 ≥ 12，短行（`[user]`、JSON 片段、`- 调用 x` 等）永远无法参与匹配，
   于是一处短行就把本可压缩的历史切成多段、常常压不动；
2. 旧实现有两处"整体放弃"：匹配步数超预算 → `return rest, 0`；总节省低于
   阈值 → 返回原文。都会让明明大量重复的内容一个字都不压。新实现按段结算，
   能压多少压多少，超预算只停止后续扫描并原样保留剩余内容。
"""

import logging

logger = logging.getLogger('msg_condense')

# 相同片段至少这么长才值得压缩：占位符自身 6 字符（首2 + .. + 末2），
# 太短的段压了不省反亏。
MIN_RUN = 60
# 总节省低于该值时不注入压缩说明行（说明行本身占字数），压缩结果仍然生效
MIN_LEGEND_SAVINGS = 80

# 索引与匹配参数
_SEED = 24        # 定位相同段所用的种子长度
_STRIDE = 8       # 已交付内容的索引步长：任何 ≥MIN_RUN 的相同段必含对齐种子
_MAX_EXTEND = 8   # 单次扫描中允许做完整扩展的候选数上限（防最坏情况）

# 兼容旧调用与旧测试保留的常量/参数名（新算法不再使用它们做判定）
MIN_PREFIX_LEN = MIN_RUN
MIN_SAVINGS = MIN_LEGEND_SAVINGS
MIN_BLOCK_LEN = MIN_RUN

_LEGEND = (
    "〔压缩说明：形如「首两字+两个点+末两字」的片段表示该段内容与上文完全相同，"
    "中间已省略；其余内容均为原样保留。〕\n")
_LEGEND_ATTACH = (
    "〔压缩说明：形如「首两字+两个点+末两字」的片段表示该段内容与附件 {ref} 中的内容"
    "完全相同，中间已省略；需要全文请读取该附件。其余内容均为原样保留。〕\n")


def common_prefix_len(a: str, b: str) -> int:
    """返回两个字符串的公共前缀长度（保留给外部调用与测试）。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _match_len(new: str, i: int, prev: str, p: int, min_run: int) -> int:
    """new[i:] 与 prev[p:] 逐字相同的最长长度；不足 min_run 直接返回 0。

    比较全部走切片（C 层内存比较）：先确认 min_run 整段相等，再按步长
    指数推进，最后在一格步长内二分——60K 字的相同段也能一次扫过。"""
    n, m = len(new), len(prev)
    if i + min_run > n or p + min_run > m:
        return 0
    if new[i:i + min_run] != prev[p:p + min_run]:
        return 0
    hi = min(n - i, m - p)
    step = 1024
    end = min_run
    while end + step <= hi and new[i + end:i + end + step] == prev[p + end:p + end + step]:
        end += step
    a, b = end, min(end + step, hi)
    while a < b:
        mid = (a + b + 1) // 2
        if new[i + end:i + mid] == prev[p + end:p + mid]:
            a = mid
        else:
            b = mid - 1
    return a


def _index_prev(prev: str) -> dict:
    """给已交付内容建种子索引：seed → [出现位置]。"""
    idx = {}
    if len(prev) < _SEED:
        return idx
    for p in range(0, len(prev) - _SEED + 1, _STRIDE):
        idx.setdefault(prev[p:p + _SEED], []).append(p)
    return idx


def _snap_to_line(new: str, i: int, run: int, min_run: int) -> int:
    """把相同段的末尾回缩到换行边界；回缩后不足 min_run 就不压这一段。

    差异常常落在行内（"中间隔断A"vs"中间隔断B"、一个数字被改）：段尾切在行
    中间时，被压掉的 token 会连差异所在行的上下文一起吃掉，可读性差也容易
    让 AI 误判。整行保留更稳，代价只是少压几个字。"""
    if run < min_run:
        return 0
    end = i + run
    if end == len(new):
        return run
    cut = new.rfind("\n", i + min_run - 1, end)
    if cut < 0:
        return 0
    ln = cut + 1 - i
    return ln if ln >= min_run else 0


def _find_run(new: str, i: int, prev: str, idx: dict, min_run: int,
              budget: int) -> tuple:
    """求 new[i:] 与 prev 逐字相同的最长长度，返回 (长度, 剩余预算)。

    种子索引按 _STRIDE 取样，而相同段的真实起点可能落在取样点之后
    1..STRIDE-1 个字符内：只查对齐位置会漏掉段头，导致开头几字白白重发
    （2026-11-06 实测段头漏 7 字）。所以按偏移逐个回查。"""
    n = len(new)
    best = 0
    for shift in range(_STRIDE):
        j = i + shift
        if j + _SEED > n:
            break
        cands = idx.get(new[j:j + _SEED])
        if not cands:
            continue
        for q in cands:
            if budget <= 0:
                return best, 0
            p2 = q - shift
            if p2 < 0:
                continue
            budget -= 1
            ln = _match_len(new, i, prev, p2, min_run)
            if ln > best:
                best = ln
        if best:
            break
    return best, budget


def _condense_runs(new: str, prev: str, min_run: int) -> tuple:
    """从头到尾扫描 new，把与 prev 逐字相同的连续段压成 `首两字..末两字`。

    返回 (压缩后文本, 节省字符数)。不同内容一律原样保留在原位；
    相同段可以出现任意多段，互不影响。"""
    if not new or not prev or min_run <= 0:
        return new, 0
    idx = _index_prev(prev)
    if not idx:
        return new, 0
    out = []
    n = len(new)
    i = 0
    diff_start = 0
    saved = 0
    budget = _MAX_EXTEND * max(1, n // min_run)
    while i < n:
        if i + min_run > n:
            break
        run, budget = _find_run(new, i, prev, idx, min_run, budget)
        run = _snap_to_line(new, i, run, min_run)
        if run >= min_run:
            if i > diff_start:
                out.append(new[diff_start:i])   # 不同的部分原样留在原位
            seg = new[i:i + run]
            out.append(seg[:2] + '..' + seg[-2:])
            saved += run - 6
            i += run
            diff_start = i
            continue
        i += 1
    if diff_start < n:
        out.append(new[diff_start:])
    return "".join(out), saved


def condense(new_text: str, prev_text: str,
             min_prefix: int = MIN_RUN,
             min_savings: int = 1,
             protect_prefix: int = 0,
             min_block: int = MIN_RUN,
             ref_note: str = "",
             min_run: int = MIN_RUN) -> str:
    """对**拼接完成的整段内容**做一次线性压缩，与 prev_text（此前已交付的
    全文）比对：

    - 与 prev 逐字相同的连续段（≥ min_run）→ `首两字..末两字`
    - 不相同的内容 → 原样保留在原位
    - protect_prefix：开头豁免压缩的字符数（取回说明等每轮必须可见的行）

    任何异常都返回原文（不压缩则全量发），绝不因压缩丢内容。
    min_prefix / min_block 参数保留只为兼容旧调用，不再参与判定。"""
    try:
        if not new_text or not prev_text:
            return new_text
        if not isinstance(protect_prefix, int) or protect_prefix < 0:
            protect_prefix = 0
        protect_prefix = min(protect_prefix, len(new_text))
        head = new_text[:protect_prefix]
        rest_new = new_text[protect_prefix:]
        rest_prev = (prev_text[protect_prefix:]
                     if protect_prefix < len(prev_text) else prev_text)
        run = min_run if min_run and min_run > 0 else MIN_RUN
        body, saved = _condense_runs(rest_new, rest_prev, run)
        if saved <= 0 or body == rest_new:
            return new_text
        legend = ""
        if saved >= MIN_LEGEND_SAVINGS:
            legend = (_LEGEND_ATTACH.format(ref=ref_note) if ref_note
                      else _LEGEND)
        logger.info("[MsgCondense] 压缩生效: 原文 %d → %d 字符 (省略 %d 字符)",
                    len(new_text), len(head) + len(legend) + len(body), saved)
        return head + legend + body
    except Exception as e:
        logger.warning("[MsgCondense] 压缩失败，回退全量发送: %s", e)
        return new_text
