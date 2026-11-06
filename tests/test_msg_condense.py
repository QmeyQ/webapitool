"""
test_msg_condense.py - 会话消息压缩模块单测

覆盖：
- 公共前缀检测与换行边界对齐
- min_prefix / min_savings 门槛（不达标则原样返回）
- 占位符语义（省略字符数、尾部保留）
- 安全降级（空输入 / 异常）
"""

import sys
import pytest
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

    def test_short_run_not_compressed_long_run_is(self):
        """不足 MIN_RUN 的相同段不压（占位符本身 6 字，压了不省）；够长才压。
        段尾对齐换行，所以构造内容都带行尾。"""
        from msg_condense import MIN_RUN
        short = "短" * (MIN_RUN // 2) + "\n"
        assert condense(short + "NEW\n", short + "OLD\n") == short + "NEW\n"
        long = "长" * (MIN_RUN * 2) + "\n"
        out = condense(long + "NEW\n", long + "OLD\n")
        assert out != long + "NEW\n"
        assert "NEW" in out and ".." in out

    def test_basic_compress(self):
        rules = _rule_block(8000)
        prev = rules + "\n上一轮的尾部内容\n"
        new = rules + "\n本轮的新增任务：请帮我写一个排序算法。\n"
        out = condense(new, prev)
        assert out != new, "前缀足够长时应压缩"
        assert "新增任务" in out, "尾部新增内容必须保留"
        assert "排序算法" in out
        assert ".." in out, "相同段应压成 首两字..末两字"
        assert "压缩说明" in out, "首次压缩应带一行图例说明"
        assert len(out) < len(new) // 2, "压缩应大幅缩短"
        # 规格：相同段 = 首两字 + ".." + 末两字
        plen = common_prefix_len(new, prev)
        run = new[:plen]
        assert run[:2] + ".." + run[-2:] in out

    def test_no_all_or_nothing_bailout(self):
        """旧实现有"总节省不够就一个字都不压"的整体放弃路径；
        新实现按段结算：能压多少压多少，不同内容原样留在原位"""
        rules = _rule_block(3000)
        prev = rules + "A" * 60
        new = rules[:200] + "B" * 80 + rules[200:] + "C" * 60
        out = condense(new, prev)
        assert out != new, "存在可压缩段就必须压缩"
        assert "B" * 80 in out, "不同的一段必须原样保留"

    def test_identical_full_text(self):
        t = _rule_block(8000)
        out = condense(t, t)
        assert out != t
        assert "与上文完全相同" in out, "头部占位符应标注与上文重复"

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
        assert out.count("..") >= 2, "差异前后可压缩的段应各自出现占位符"

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

    def test_81_char_run_is_compressed(self):
        """81 字相同段 ≥ MIN_RUN → 压缩（旧实现按 100 字门槛会整体放弃）"""
        rules = _rule_block(81)
        new = rules + "\nNEW\n"
        out = condense(new, rules + "\nOLD\n")
        assert out != new
        assert "NEW" in out and ".." in out


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
        assert ".." in out, "相同段应压成 首两字..末两字"
        assert "PREV 独有尾部内容" not in out

        # 多个分散的重复段各自独立压缩
        segA = "".join(f"段A{i}: 稳定内容行，用于重复段检测测试。\n"
                       for i in range(5))
        segB = "".join(f"段B{i}: 另一段稳定内容行，用于重复段检测。\n"
                       for i in range(5))
        prev2 = segA + "中间隔断A\n" + segB + "PREV 尾\n"
        new2 = segA + "中间隔断B\n" + segB + "NEW 尾\n"
        out2 = condense(new2, prev2)
        assert out2.count("..") == 2, "每个重复段都应各自压缩"
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


class TestAttachmentBaseline:
    """附件轮次也要参与压缩比对：上一轮写进 uploads 的附件原文就是基准。"""

    NAME = "prompt_1793900000_dead01.txt"
    # 键必须带真实对话 id：没有对话 id 的键（IDE 指纹、裸串）一律不许当基准，
    # 否则另一个同样停在根地址的会话会把别人的内容当成"本对话已交付"压掉
    CK1 = "chatgpt.com:c-delhist"
    CK2 = "chatgpt.com:c-registered"
    CK3 = "chatgpt.com:c-file-gone"

    def _write_upload(self, text: str) -> Path:
        up = Path(__file__).resolve().parent.parent / "uploads"
        up.mkdir(exist_ok=True)
        f = up / self.NAME
        f.write_text(text, encoding="utf-8")
        return f

    def test_from_history_attachment_name(self):
        """对话历史里认得出附件名 → 读本地文件复原基准"""
        from browser_core import BrowserCore
        text = _rule_block(3000)
        f = self._write_upload(text)
        try:
            bc = object.__new__(BrowserCore)
            bc._fp_attach = {}
            got, label = bc._attachment_baseline(
                self.CK1, f"请完整阅读我上传的文件 {self.NAME} 后完成任务")
            assert got == text, "基准应等于附件原文"
            assert self.NAME in label
        finally:
            f.unlink(missing_ok=True)

    def test_recorded_names_take_priority(self):
        """本会话已登记附件名时不必再从历史里找"""
        from browser_core import BrowserCore
        text = "已交付的长内容\n" * 400
        f = self._write_upload(text)
        try:
            bc = object.__new__(BrowserCore)
            bc._fp_attach = {self.CK2: {"names": [self.NAME],
                                        "label": self.NAME}}
            got, label = bc._attachment_baseline(self.CK2, "")
            assert got == text and label == self.NAME
        finally:
            f.unlink(missing_ok=True)

    def test_missing_file_degrades_to_full_send(self):
        """uploads 已被启动清理/换机器 → 读不到就不比对，全量发"""
        from browser_core import BrowserCore
        bc = object.__new__(BrowserCore)
        bc._fp_attach = {}
        got, _label = bc._attachment_baseline(
            self.CK3, f"上一轮的附件 {self.NAME}")
        assert got == ""

    def test_key_without_conversation_id_is_refused(self):
        """没有对话 id 的键（历史遗留的 "fk1"、裸站点名）一律不比对"""
        from browser_core import BrowserCore
        text = "已交付的长内容" * 300
        f = self._write_upload(text)
        try:
            bc = object.__new__(BrowserCore)
            bc._fp_attach = {"fk1": {"names": [self.NAME], "label": self.NAME}}
            got, _label = bc._attachment_baseline("fk1", "")
            assert got == "", "无对话 id 的键不能拿到基准"
            assert bc._attachment_baseline("chatgpt.com", "")[0] == ""
        finally:
            f.unlink(missing_ok=True)


class TestCondenseRefNote:
    """基准来自附件时，占位符必须让网页 AI 去读那个附件，而不是找正文上文"""

    def test_placeholder_points_to_attachment(self):
        prev = _rule_block(4000)
        new = prev + "\n本轮新增指令：继续执行。\n"
        name = "prompt_1_aaaaaa.txt"
        out = condense(new, prev, ref_note=name)
        assert name in out, "占位符要写明附件文件名"
        assert "附件" in out
        assert out.endswith("本轮新增指令：继续执行。\n")

    def test_default_wording_unchanged(self):
        prev = _rule_block(4000)
        new = prev + "\n新增\n"
        out = condense(new, prev)
        assert "与上文" in out, "默认措辞：相同段用 … 标记，不提附件"
        assert "附件" not in out, "不带 ref_note 时文案保持原样"


class TestContextLimitHandling:
    """站点报"上下文窗口已达上限"：新起对话 + 作废压缩基准（本轮全量重发）"""

    def _bc(self):
        from browser_core import BrowserCore
        bc = object.__new__(BrowserCore)
        bc._fp_last_payload = {"fk": "已交付的长文本" * 200}
        bc._fp_attach = {"fk": {"names": ["prompt_1_aaaaaa.txt"],
                                "label": "prompt_1_aaaaaa.txt"}}
        bc._conv_upload_counts = {"chatgpt.com:xx": 4}
        return bc

    def test_probe_passes_flag_through(self):
        from browser_core import probe_page_state

        class T:
            def run_js(self, script, *a, **k):
                return {"url": "https://chatgpt.com/c/x", "err_code": "",
                        "rich_editors": 1, "plain_textareas": 0,
                        "login_ctas": 0, "context_full": 1,
                        "limit_text": "上下文窗口已达上限 开启新聊天"}
        st = probe_page_state(T())
        assert st["state"] == "ok", "上限页面仍可输入，state 不该变"
        assert st.get("context_full") is True
        assert "已达上限" in st.get("limit_text", "")

    def test_handler_invalidates_and_clicks(self):
        bc = self._bc()
        clicks = []

        class T:
            def run_js(self, script, *a, **k):
                if "rich_editors" in script:      # 状态探测脚本
                    return {"url": "https://chatgpt.com/c/x", "err_code": "",
                            "rich_editors": 1, "plain_textareas": 0,
                            "login_ctas": 0, "context_full": 1,
                            "limit_text": "上下文窗口已达上限"}
                clicks.append(script)            # 点击新聊天的脚本
                return "clicked"

        ok = bc._handle_context_limit(
            T(), {"new_chat_btn": 'css:button[aria-label="新聊天"]'}, "fk")
        assert ok is True
        assert "fk" not in bc._fp_last_payload, "压缩基准必须作废"
        assert "fk" not in bc._fp_attach, "附件登记也要作废"
        assert bc._conv_upload_counts == {}, "新会话附件计数归零"
        assert clicks, "必须真的点击新聊天"

    def test_no_false_positive_without_banner(self):
        """没命中上限时绝不清基准——侧栏常驻"新聊天"按钮不是上限信号"""
        bc = self._bc()

        class T:
            def run_js(self, script, *a, **k):
                return {"url": "https://chatgpt.com/", "err_code": "",
                        "rich_editors": 1, "plain_textareas": 0,
                        "login_ctas": 0, "context_full": 0, "limit_text": ""}

        assert bc._handle_context_limit(T(), {"new_chat_btn": "css:button"},
                                        "fk") is False
        assert "fk" in bc._fp_last_payload

    def test_missing_selector_still_disables_condense(self):
        """站点没配 new_chat_btn：点不了新聊天，但仍要停用压缩"""
        bc = self._bc()

        class T:
            def run_js(self, script, *a, **k):
                return {"url": "https://chatgpt.com/", "err_code": "",
                        "rich_editors": 1, "plain_textareas": 0,
                        "login_ctas": 0, "context_full": 1,
                        "limit_text": "上下文窗口已达上限"}

        assert bc._handle_context_limit(T(), {}, "fk") is True
        assert "fk" not in bc._fp_last_payload


class TestConversationKey:
    """会话指纹：基准键必须带对话标识"""

    def test_conversation_key_isolates_baselines(self):
        """换了 /c/<id> 就不能沿用旧基准"""
        from browser_core import _conversation_key as ck
        assert ck("https://chatgpt.com/c/abc123") == "abc123"
        assert ck("https://chatgpt.com/c/abc123?x=1") == "abc123"
        assert ck("https://chatgpt.com/") == ""
        assert ck("https://chatgpt.com/c/abc123") != ck("https://chatgpt.com/c/zzz9")


class TestPageProbeNotPerChunk:
    """页面可用性判定属于策略阶段：发送循环里不得逐片重扫全页"""

    def _ex(self, counter, state=None):
        from browser_core import WorkflowExecutor

        class T:
            url = "https://chatgpt.com/"

            def run_js(self, script, *a, **k):
                counter.append(1)
                return dict({"url": "https://chatgpt.com/", "err_code": "",
                             "rich_editors": 1, "plain_textareas": 0,
                             "login_ctas": 0, "btn_texts": "",
                             "context_full": 0, "limit_text": ""},
                            **(state or {}))
        ex = object.__new__(WorkflowExecutor)
        ex.tab = T()
        return ex

    def test_cached_within_ttl(self):
        calls = []
        ex = self._ex(calls)
        for _ in range(96):      # 96 片的极端场景
            ex._guard_page_usable("填充", cache_ttl=5.0)
        assert len(calls) == 1, "同阶段必须复用页面状态，不应 96 次全页比对"

    def test_uncached_probes_every_time(self):
        calls = []
        ex = self._ex(calls)
        for _ in range(3):
            ex._guard_page_usable("填充")
        assert len(calls) == 3, "不传 TTL 时保持逐次真实探测"

    def test_expired_cache_reprobes_and_raises(self):
        """缓存过期后必须重探：中途被踢到登录墙要能立刻停手"""
        from browser_core import PageUnavailableError
        calls = []
        ex = self._ex(calls)
        ex._guard_page_usable("填充", cache_ttl=5.0)
        ex._page_state_cache = (0.0, {})          # 强制过期
        ex.tab.run_js = lambda script, *a, **k: dict(
            {"url": "https://chatgpt.com/", "err_code": "",
             "rich_editors": 0, "plain_textareas": 1, "login_ctas": 2,
             "btn_texts": "登录|免费注册", "context_full": 0,
             "limit_text": ""})
        with pytest.raises(PageUnavailableError):
            ex._guard_page_usable("填充", cache_ttl=5.0)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
