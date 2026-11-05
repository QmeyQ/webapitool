"""
test_msg_condense.py - 会话消息压缩模块单测

覆盖：
- 公共前缀检测与换行边界对齐
- min_prefix / min_savings 门槛（不达标则原样返回）
- 占位符语义（省略字符数、尾部保留）
- 安全降级（空输入 / 异常）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from msg_condense import common_prefix_len, condense, MIN_PREFIX_LEN


def _rule_block(n: int, seed: str = "规则") -> str:
    """构造 n 字符的稳定文本（模拟 IDE 系统规则，跨轮完全相同）"""
    lines = []
    total = 0
    i = 0
    while total < n:
        line = f"{seed}{i}: 这是一个用于测试的稳定规则行，内容保持不变。\n"
        lines.append(line)
        total += len(line)
        i += 1
    return "".join(lines)[:n] if n < total else "".join(lines)


class TestCommonPrefix:
    def test_identical(self):
        a = _rule_block(500)
        assert common_prefix_len(a, a) == len(a)

    def test_partial(self):
        a = _rule_block(500)
        b = a[:300] + "DIFFERENT TAIL"
        assert common_prefix_len(a, b) >= 290

    def test_diverge_at_line_start(self):
        a = "line1\nline2\nline3\n"
        b = "line1\nline2\nXXXX\n"
        # 公共前缀到 "line2\n" 结束（换行边界），不带下半行
        pl = common_prefix_len(a, b)
        assert pl == len("line1\nline2\n")
        assert a[:pl] == b[:pl]

    def test_no_common(self):
        assert common_prefix_len("abc", "xyz") == 0

    def test_empty(self):
        assert common_prefix_len("", "abc") == 0
        assert common_prefix_len("abc", "") == 0


class TestCondense:
    def test_no_prev_returns_original(self):
        t = _rule_block(5000)
        assert condense(t, "") == t

    def test_empty_new_returns_empty(self):
        assert condense("", _rule_block(5000)) == ""

    def test_prefix_too_short(self):
        # 60 字符规则 < MIN_PREFIX_LEN(100)，块级 54 字符 < MIN_BLOCK_LEN(100)
        prev = _rule_block(60) + "OLD TAIL"
        new = _rule_block(60) + "NEW TAIL"
        assert condense(new, prev) == new

    def test_basic_compress(self):
        rules = _rule_block(8000)
        prev = rules + "\n上一轮的尾部内容\n"
        new = rules + "\n本轮的新增任务：请帮我写一个排序算法。\n"
        out = condense(new, prev)
        assert out != new, "前缀足够长时应压缩"
        assert "新增任务" in out, "尾部新增内容必须保留"
        assert "排序算法" in out
        assert "省略" in out and "字符" in out, "占位符应说明省略量"
        assert len(out) < len(new) // 2, "压缩应大幅缩短"
        # 占位符里的省略数应≈前缀长度
        import re
        m = re.search(r"省略 (\d+) 字符", out)
        assert m and abs(int(m.group(1)) - common_prefix_len(new, prev)) < 10

    def test_min_savings_threshold(self):
        # 自定义门槛：前缀 300 字符但 min_savings 要求省 5000 → 不压缩
        rules = _rule_block(300)
        prev = rules + "A" * 50
        new = rules + "B" * 50
        assert condense(new, prev, min_prefix=100, min_savings=5000) == new
        # 同样内容放宽门槛 → 压缩
        assert condense(new, prev, min_prefix=100, min_savings=10) != new

    def test_identical_full_text(self):
        t = _rule_block(8000)
        out = condense(t, t)
        assert out != t
        assert "新增内容" in out

    def test_tail_content_not_corrupted(self):
        rules = _rule_block(6000)
        tail = "\n最后一行指令：输出 JSON {\"a\": 1}\n"
        new = rules + tail
        out = condense(new, rules)
        assert out.endswith(tail), "压缩后尾部必须与原文逐字一致"

    def test_default_thresholds_realistic(self):
        """真实场景：IDE 每轮重放 3 万字规则 + 少量增量"""
        rules = _rule_block(30000)
        prev = rules + "\n上一轮任务尾巴\n"
        new = rules + "\n新一轮任务：继续修复 bug。\n"
        out = condense(new, prev)
        assert len(out) < 1000, f"应压到极短，实际 {len(out)}"
        assert MIN_PREFIX_LEN <= common_prefix_len(new, prev)

    def test_never_raises_on_weird_input(self):
        # 各种畸形输入都应安全返回原文本
        assert condense(None, None) in (None, "")
        assert condense("abc", None) == "abc"
        assert condense(None, "abc") in (None, "")


class TestBlockCondense:
    """块级去重：前缀截止后，与上轮已发送内容相同的中段长块也应压缩"""

    def test_mid_block_compressed_when_prefix_breaks(self):
        """system 规则中段有动态内容 → 公共前缀提前截止，
        之后与上轮相同的历史大块仍应被压缩（核心需求）"""
        rules = _rule_block(3000)
        mid = _rule_block(2000, seed="历史")
        prev = rules + "\n" + mid + "\n上一轮结尾\n"
        new = (rules[:1500] + "〔动态时间戳 2026-11-05 12:00〕" + rules[1500:]
               + "\n" + mid + "\n本轮新增任务：执行 cmd 工具。\n")
        out = condense(new, prev)
        assert out != new, "中段存在大块重复时应压缩"
        assert "动态时间戳" in out, "前缀截止处的内容必须保留"
        assert "本轮新增任务" in out, "新增内容必须保留"
        assert len(out) < len(new) - 1200, "中段重复块应被大幅压缩"
        assert out.count("此处省略") >= 1, "应出现块级占位符"

    def test_head_and_block_combined(self):
        """头部前缀压缩与块级去重可同时生效"""
        rules = _rule_block(3000)
        mid = _rule_block(1500, seed="历史")
        prev = rules + mid + "\nPREV-TAIL\n"
        new = rules + mid + "\nNEW-TAIL 本轮任务\n"
        out = condense(new, prev)
        assert out != new
        assert "NEW-TAIL" in out
        assert "PREV-TAIL" not in out

    def test_min_block_threshold(self):
        # 3 行 ≈ 81 字符 < 默认 MIN_BLOCK_LEN(100) → 不压缩
        rules = _rule_block(81)
        prev = rules + "\nOLD\n"
        new = rules + "\nNEW\n"
        assert condense(new, prev) == new
        # 放宽 min_block → 压缩
        assert condense(new, prev, min_block=50, min_prefix=50,
                        min_savings=10) != new

    def test_100_char_blocks_all_compressed(self):
        """用户需求：100 字符级别的重复段也要压缩，每个重复段都处理"""
        seg = "".join(
            f"片段{i}: 这是一个用于测试的稳定规则行，内容保持不变。\n"
            for i in range(5))  # 5 完整行 ≈ 135 字符 ≥ MIN_BLOCK_LEN
        prev = seg + "PREV 独有尾部内容\n"
        new = seg + "NEW 本轮新增任务内容\n"
        out = condense(new, prev)
        assert out != new, "100+ 字符重复段应压缩"
        assert "NEW 本轮新增任务内容" in out, "新增内容必须保留"
        assert "此处省略" in out, "块级占位符应出现"
        assert "PREV 独有尾部内容" not in out

        # 多个分散的重复段各自独立压缩
        segA = "".join(f"段A{i}: 稳定内容行，用于重复段检测测试。\n"
                       for i in range(5))
        segB = "".join(f"段B{i}: 另一段稳定内容行，用于重复段检测。\n"
                       for i in range(5))
        prev2 = segA + "中间隔断A\n" + segB + "PREV 尾\n"
        new2 = segA + "中间隔断B\n" + segB + "NEW 尾\n"
        out2 = condense(new2, prev2)
        assert out2.count("此处省略") == 2, "每个重复段都应各自压缩"
        assert "NEW 尾" in out2 and "中间隔断B" in out2

    def test_block_never_eats_new_content(self):
        """新增内容永不被块级压缩吃掉"""
        rules = _rule_block(3000)
        prev = rules + "\n历史大块A：只有上一轮才有的一段长内容。\n" * 20
        new = rules + "\n历史大块A：只有上一轮才有的一段长内容。\n" * 20 \
            + "\n全新的本轮指令：读取 C:/workspace/main.py\n"
        out = condense(new, prev)
        assert "全新的本轮指令" in out
        assert "main.py" in out
        assert out != new

    def test_chain_base_is_uncompressed_original(self):
        """压缩输出 + 未压缩原文作基准：链式压缩不丢前缀信息"""
        rules = _rule_block(5000)
        prev = rules + "\n上一轮任务结束\n"
        new = rules + "\n本轮任务开始\n"
        out1 = condense(new, prev)
        assert "本轮任务开始" in out1
        # 第三轮以未压缩 new 为基准（模拟 browser_core 记录 _condense_original）
        new3 = new + "\n第三轮增量：继续执行。\n"
        out2 = condense(new3, new)
        assert "第三轮增量" in out2
        assert out2 != new3


class TestProtectPrefix:
    """协议区豁免：工具表必须逐字保留在压缩后文本开头"""

    def test_protocol_area_never_condensed(self):
        proto = ("PROTOCOL-START 你只作为大语言模型 可转发的目标 "
                 "{\"name\":\"cmd\",\"description\":\"run command\"} "
                 "PROTOCOL-END\n")
        rules = _rule_block(3000)
        prev = proto + rules + "\n旧尾部\n"
        new = proto + rules + "\n新尾部\n"
        out = condense(new, prev, protect_prefix=len(proto))
        assert out.startswith(proto), "协议区必须逐字保留"
        assert "新尾部" in out
        assert out != new

    def test_protect_larger_than_text(self):
        t = _rule_block(100)
        assert condense(t, t, protect_prefix=10**9) == t

    def test_protect_zero_keeps_default_behavior(self):
        rules = _rule_block(5000)
        prev = rules + "\n旧\n"
        new = rules + "\n新\n"
        out = condense(new, prev, protect_prefix=0)
        assert out != new and "新" in out

    def test_protect_with_mid_blocks(self):
        """协议区豁免 + 中段块级压缩同时生效（真实续传轮场景）"""
        proto = "PROTOCOL {\"tools\": [1, 2, 3]} END\n"
        rules = _rule_block(2000)
        mid = _rule_block(1500, seed="历史")
        prev = proto + rules[:1000] + "旧状态" + rules[1000:] + mid + "\nP\n"
        new = proto + rules[:1000] + "新状态" + rules[1000:] + mid + "\nN\n"
        out = condense(new, prev, protect_prefix=len(proto))
        assert out.startswith(proto)
        assert "旧状态" not in out and "新状态" in out
        assert len(out) < len(new) - 1500, "协议区之后的大块重复应压缩"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
