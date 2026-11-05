"""
msg_condense.py - 会话消息压缩模块（纯函数，无副作用）

背景：IDE 每轮请求都会重放完整对话历史（系统规则 + 工具调用轮次），
序列化后的 prompt 数万字且与上一轮几乎完全相同，导致每条消息都要分片。

方案（两级压缩）：
1. 头部前缀压缩：对比本会话上一条实际发送到页面的文本（由 browser_core
   按会话指纹缓存），若本次 prompt 与其存在足够长的公共前缀，则把前缀
   替换为占位符说明，只发送新增尾部。
2. 中间块级去重：IDE 重放的历史并非纯追加（system 规则里常有时间/诊断
   等动态内容，让公共前缀提前截止），前缀截止后与上轮已发送内容相同的
   长区块（>= min_block）也各自替换为占位符——已存在的内容都支持压缩。

安全性：
- 网页会话中此前消息全文上屏可见，压缩只省略"已发送过的重复内容"，
  从未发送的新内容永远原样保留，AI 可直接读取会话上文，不丢失信息。
- prompt 开头的协议区（工具表 + 续传指令）通过 protect_prefix 豁免，
  保证网页 AI 每轮都能在最新消息里看到完整工具清单（否则它看不到
  cmd 等工具声明，表现为"没有可用工具"）。

降级策略：任何条件不满足（无缓存 / 前缀太短 / 块太短 / 节省太少 / 匹配
预算超限）都返回原文，即"不压缩则全量发"，绝不因压缩引入风险。
"""

import logging

logger = logging.getLogger('msg_condense')

# 压缩参数（可按需调整）
# 用户要求：100 字级别的重复段也要压缩——每个重复段都处理，
# 不再只压大块头部（2026-11-05 放宽：2000/1200/800 → 100/50/100）。
MIN_PREFIX_LEN = 100     # 头部公共前缀至少要这么长才值得压缩
MIN_SAVINGS = 50         # 总节省至少要这么多字符才有意义
MIN_BLOCK_LEN = 100      # 中间重复块至少要这么长才单独压缩

# 块级匹配内部参数
_MIN_LINE_MATCH = 12       # 参与匹配索引的最短行长（过短行噪声大；12 字符
                           # 已能覆盖常见短行，配合连续行延伸防误匹配）
_MAX_CANDIDATES = 8        # 每行最多尝试的重复起点数
_MATCH_BUDGET = 1_000_000  # 行比较总步数保险，超限整体回退全量发

# 占位符：交错式——相同的连续段落用 "…" 标记省略，不同的内容原样留在原位。
# 保留 "省略 N 字符" 结构（测试与日志正则依赖），措辞尽量短以让小块也有净收益。
_HEAD_PLACEHOLDER = "…〔省略 {saved} 字符，与上文完全相同〕…\n"

# 中间块占位符：省略的是历史中段与此前消息重复的区块。
_MID_PLACEHOLDER = "…〔省略 {saved} 字符，与上文相同〕…\n"


def _placeholders(ref_note: str = "") -> tuple:
    """返回 (头部占位符, 块占位符)。

    ref_note 非空时，被省略的内容不在会话正文里，而在上一轮上传的附件
    （如 prompt_1793890600_f32ad0.txt）里：必须直接写明"去读那个附件"，
    否则网页 AI 会以为上文消息里有正文，读到占位符就以为内容丢了。"""
    if not ref_note:
        return _HEAD_PLACEHOLDER, _MID_PLACEHOLDER
    return (
        "…〔省略 {saved} 字符，内容与上一轮上传的附件 " + ref_note
        + " 相同，请读取该附件〕…\n"
    ), (
        "…〔省略 {saved} 字符，与附件 " + ref_note
        + " 内容相同，请读取该附件〕…\n"
    )


def common_prefix_len(a: str, b: str) -> int:
    """返回两个字符串的公共前缀长度。

    无需换行对齐：被省略的 new_text[:plen] 与 prev_text[:plen] 逐字
    相同（这正是前缀的定义），任意切点替换都不产生语义破碎。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _split_lines(text: str) -> list:
    """按行切分并保留行尾换行符，保证拼接后与原文逐字一致。"""
    if not text:
        return []
    return text.splitlines(keepends=True)


def _condense_blocks(rest: str, prev: str, min_block: int,
                     mid_ph: str = _MID_PLACEHOLDER) -> tuple:
    """块级去重：rest 中与 prev 逐字相同的连续长行块替换为占位符。

    匹配是行对齐的精确相等比较（索引 + 贪心延伸），只会省略 prev 中
    逐字出现过的内容，新增内容永不被省略。
    返回 (压缩后文本, 节省字符数)。永不抛异常。"""
    if not rest or not prev or min_block <= 0:
        return rest, 0
    try:
        prev_lines = _split_lines(prev)
        if not prev_lines:
            return rest, 0
        index = {}
        for j, pl in enumerate(prev_lines):
            if len(pl) >= _MIN_LINE_MATCH:
                index.setdefault(pl, []).append(j)
        if not index:
            return rest, 0
        new_lines = _split_lines(rest)
        out = []
        saved = 0
        steps = 0
        i = 0
        n = len(new_lines)
        m = len(prev_lines)
        while i < n:
            line = new_lines[i]
            cands = index.get(line)
            replaced = False
            if cands:
                best = 0
                for c in cands[:_MAX_CANDIDATES]:
                    k = 0
                    while (i + k < n and c + k < m
                           and new_lines[i + k] == prev_lines[c + k]):
                        k += 1
                    steps += k
                    if k > best:
                        best = k
                    if steps > _MATCH_BUDGET:
                        break
                if best:
                    block_len = sum(len(x) for x in new_lines[i:i + best])
                    if block_len >= min_block:
                        ph = mid_ph.format(saved=block_len)
                        out.append(ph)
                        saved += block_len - len(ph)
                        i += best
                        replaced = True
            if not replaced:
                out.append(line)
                i += 1
            if steps > _MATCH_BUDGET:
                logger.info("[MsgCondense] 块级匹配预算超限，回退全量发送")
                return rest, 0
        return "".join(out), saved
    except Exception as e:
        logger.warning("[MsgCondense] 块级压缩失败: %s", e)
        return rest, 0


def condense(new_text: str, prev_text: str,
             min_prefix: int = MIN_PREFIX_LEN,
             min_savings: int = MIN_SAVINGS,
             protect_prefix: int = 0,
             min_block: int = MIN_BLOCK_LEN,
             ref_note: str = "") -> str:
    """两级压缩 new_text，与 prev_text（上轮已发送全文）对比：

    1. 头部公共前缀（>= min_prefix）替换为头部占位符，只发增量尾部；
    2. 前缀之后与 prev 重复的长行块（>= min_block）各自替换为块占位符；
    3. protect_prefix：开头豁免压缩的字符数（协议区：工具表+续传指令），
       保证网页 AI 每轮都能在最新消息里看到完整工具清单。
    4. ref_note：prev_text 是通过附件（而非正文消息）交付时，填附件文件名，
       占位符改为"请读取该附件"，避免 AI 在会话正文里找不存在的内容。

    总节省 < min_savings 时返回原文。永不抛异常，任何异常回退全量发。"""
    try:
        if not new_text or not prev_text:
            return new_text
        head_ph_tpl, mid_ph_tpl = _placeholders(ref_note)
        if not isinstance(protect_prefix, int) or protect_prefix < 0:
            protect_prefix = 0
        protect_prefix = min(protect_prefix, len(new_text))
        head = new_text[:protect_prefix]
        rest_new = new_text[protect_prefix:]
        rest_prev = (prev_text[protect_prefix:]
                     if protect_prefix < len(prev_text) else "")

        plen = common_prefix_len(rest_new, rest_prev) if rest_prev else 0
        pieces = []
        total_savings = 0
        body_start = 0
        if plen >= min_prefix:
            # 切点对齐换行边界：分叉发生在行中间时把半行留给新增区，
            # 避免"中间隔断B"这类词被拦腰截断（信息虽在上文，但完整
            # 词可读性更好）；回退后不足 min_prefix 则放弃头部压缩，
            # 交由块级去重处理
            cut = rest_new.rfind("\n", 0, plen) + 1 if plen > 0 else 0
            if 0 < cut < plen and cut >= min_prefix:
                plen = cut
        if plen >= min_prefix:
            head_ph = head_ph_tpl.format(saved=plen)
            pieces.append(head_ph)
            total_savings += plen - len(head_ph)
            body_start = plen

        # 块级基准用整个 rest_prev：new 中段内容与 prev 任何已发送部分
        # 重复（含头部前缀区覆盖的部分）都可安全省略——那些内容在网页
        # 会话的上轮消息里逐字可见。
        body, block_saved = _condense_blocks(
            rest_new[body_start:], rest_prev, min_block, mid_ph_tpl)
        pieces.append(body)
        total_savings += block_saved

        if total_savings < min_savings:
            return new_text
        condensed = head + "".join(pieces)
        if condensed == new_text:
            return new_text
        logger.info("[MsgCondense] 压缩生效: 原文 %d → %d 字符 "
                    "(头部前缀 %d, 块级省略 %d)",
                    len(new_text), len(condensed),
                    plen if plen >= min_prefix else 0, block_saved)
        return condensed
    except Exception as e:
        logger.warning("[MsgCondense] 压缩失败，回退全量发送: %s", e)
        return new_text
