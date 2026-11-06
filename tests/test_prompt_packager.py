"""PromptPackager 单测：次序、基准键位、附件比对、工具名清单。

真实样本来自 2026-11-06 现场：
  uploads/prompt_1793966551_875bf7.txt —— 218,684 字符，**未经压缩**整份
    落盘当附件（IDE 换了会话指纹，但网页端还在同一个对话，基准按指纹存
    所以找不到）。网页 AI 读不动这种体量，回出来就是"没有可用工具/权限不对"。
  工具表在文件第 149 字符处（开头），而输入框那句提示却让模型"去对话开头
    或文件末尾找"——指错位置，模型于是反复 tool_search。
"""

import glob
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompt_packager as pp

REAL_HEAD = (
    "你只作为大语言模型（你是ai中间人），禁止调用你自生的工具或除文本"
    "以外的内容（下游会识别工具并调用），严格按照我的格式要求生成工具"
    "和调用内容。转发指令格式（逐字照抄结构）：\n\n```tool_call\n"
    '{"name": "工具名", "arguments": {"参数名": "值"}}\n```\n\n'
    "可转发的目标（name 大小写敏感，完整定义 JSON 如下，按其中的"
    " description 和 parameters 选择工具并组织 arguments）：\n"
    '[{"function":{"name":"exec","description":"Run a shell command"}},'
    '{"function":{"name":"screen_record","description":"Capture screen"}}]\n\n')

TOOLS = [{"function": {"name": "exec",
                       "description": "Run a shell command",
                       "parameters": {"type": "object", "properties": {}}}},
         {"function": {"name": "screen_record",
                       "description": "Capture the screen",
                       "parameters": {"type": "object", "properties": {}}}},
         {"function": {"name": "nodes_status",
                       "description": "List paired nodes",
                       "parameters": {"type": "object", "properties": {}}}}]


def _build(messages, with_table):
    """模拟 ToolRoundBuilder.build：协议区在前，消息在后"""
    parts = []
    if with_table:
        parts.append(pp.__dict__ and REAL_HEAD.replace(
            "nodes_status", "nodes_status"))
    for m in messages:
        parts.append(f"[{m['role']}]\n{m['content']}")
    return "\n\n".join(parts)


@pytest.fixture
def packager(tmp_path):
    up = tmp_path / "uploads"
    up.mkdir()
    return pp.PromptPackager(upload_dir=str(up),
                             state_file=str(tmp_path / "pack_state.json"))


def _msgs(body):
    return [{"role": "system", "content": "规则"},
            {"role": "user", "content": body}]


# ---------------- 次序：先压缩，再决定怎么打包 ----------------

class TestOrderCompressThenPlan:
    def test_plan_uses_compressed_size(self, packager):
        """用户明确要求：压缩在打包之前，尺寸判断看压缩后的结果。

        这里原文约 60k 字符（超 file 上限 → 会被判成分片/拒单），
        与上一轮交付内容逐字相同的部分压掉后只剩新增内容 → 必须走 direct。
        """
        prev = REAL_HEAD + "[user]\n" + ("历史正文段落。\n" * 6000)
        packager.baseline["chatgpt.com:conv1"] = prev
        new = REAL_HEAD + "[user]\n" + ("历史正文段落。\n" * 6000) \
            + "新增的一条工具结果：node A 已连接\n"
        calls = []

        def build(messages, with_table):
            calls.append(with_table)
            return new

        pkg = packager.prepare(build_text=build, messages=_msgs(new),
                               tools=TOOLS, think_hint=False,
                               continuation=True, ckey="chatgpt.com:conv1",
                               upload_cfg={"max_prompt_length": 2000,
                                           "max_file_size": 100_000})
        assert pkg.compressed is True
        assert len(pkg.body) < len(new) / 10, (
            f"压缩后仍 {len(pkg.body)} 字符，原文 {len(new)}")
        assert pkg.strategy == pp.STRATEGY_DIRECT, (
            "按原文尺寸会被判成附件，按压缩后尺寸就该直发")
        assert pkg.audit["original"] == len(new)
        assert pkg.audit["body"] == len(pkg.body)

    def test_still_oversized_after_compress_goes_to_file(self, packager):
        packager.baseline["chatgpt.com:convC"] = REAL_HEAD + "[user]\n" + ("旧。\n" * 200)
        new = REAL_HEAD + "[user]\n" + ("旧。\n" * 200) + ("全新内容。\n" * 20000)
        pkg = packager.prepare(build_text=lambda m, t: new, messages=_msgs(new),
                               tools=TOOLS, think_hint=False,
                               continuation=True, ckey="chatgpt.com:convC",
                               upload_cfg={"max_prompt_length": 2000,
                                           "max_file_size": 100_000})
        assert pkg.strategy in (pp.STRATEGY_FILE, pp.STRATEGY_CHUNKED)

    def test_absurd_size_raises_instead_of_opening_96_chunks(self, packager):
        new = "内容" * 400_000          # 800k 字符，压缩不了（没有基准）
        with pytest.raises(pp.TooLongPlan):
            packager.prepare(build_text=lambda m, t: new, messages=_msgs(new),
                             tools=None, think_hint=False,
                             continuation=False, ckey="chatgpt.com:convC",
                             upload_cfg={"max_prompt_length": 2000,
                                         "max_file_size": 100_000},
                             chunk_limit=12000, max_chunks=30)

    def test_disabled_upload_is_direct(self, packager):
        new = "x" * 5000
        pkg = packager.prepare(build_text=lambda m, t: new, messages=_msgs(new),
                               tools=None, think_hint=False,
                               continuation=False, ckey="chatgpt.com:convC",
                               upload_cfg={"enabled": False})
        assert pkg.strategy == pp.STRATEGY_DIRECT


# ---------------- 基准键位：按网页对话，不按 IDE 指纹 ----------------

class TestBaselineKeying:
    def test_ide_fingerprint_change_still_compresses(self, packager):
        """现场事故：IDE 换会话（指纹变）但网页端还在同一对话，
        基准按指纹存就找不到 → 218,684 字符整份未压缩落盘"""
        prev = REAL_HEAD + "[user]\n" + ("同一份历史。\n" * 8000)
        packager.baseline["chatgpt.com:convX"] = prev
        new = REAL_HEAD + "[user]\n" + ("同一份历史。\n" * 8000) + "本轮新增\n"
        pkg = packager.prepare(build_text=lambda m, t: new, messages=_msgs(new),
                               tools=TOOLS, think_hint=False,
                               continuation=True, ckey="chatgpt.com:convX",
                               upload_cfg={"max_prompt_length": 2000})
        assert pkg.compressed and pkg.baseline_source == "delivery"

    def test_different_conversation_never_shares_baseline(self, packager):
        packager.baseline["chatgpt.com:convA"] = "只属于 A 对话的内容" + ("A" * 5000)
        pkg = packager.prepare(
            build_text=lambda m, t: "只属于 A 对话的内容" + ("A" * 5000),
            messages=_msgs("x"), tools=None, think_hint=False,
            continuation=True, ckey="chatgpt.com:convB",
            upload_cfg={"max_prompt_length": 2000})
        assert pkg.compressed is False, "跨对话沿用基准会把内容压进虚空"

    def test_record_survives_restart(self, packager, tmp_path):
        """重启后同一对话还要能继续压缩，不能每次都全量重发"""
        text = REAL_HEAD + "[user]\n" + ("内容。\n" * 4000)
        pkg = packager.prepare(build_text=lambda m, t: text, messages=_msgs(text),
                               tools=TOOLS, think_hint=False,
                               continuation=False, ckey="chatgpt.com:convR",
                               upload_cfg={"max_prompt_length": 2000})
        packager.record("chatgpt.com:convR", pkg)
        again = pp.PromptPackager(upload_dir=str(packager.upload_dir),
                                  state_file=packager.state_file)
        prev, _note, src = again.find_baseline("chatgpt.com:convR", [])
        assert prev == text and src == "delivery", "重启后同一对话还要能继续压"

    def test_invalidate_clears_all_delivered_knowledge(self, packager):
        packager.baseline["chatgpt.com:convC"] = "旧基准"
        packager.attachments["chatgpt.com:convC"] = {"names": ["prompt_1_a.txt"], "label": "x"}
        packager.set_progress("chatgpt.com:convC", 3, 9, "https://chatgpt.com/c/x")
        dropped = packager.invalidate("chatgpt.com:convC", "上下文已达上限，新起对话")
        assert sorted(dropped) == ["attachments", "baseline", "chunk_progress"]
        assert packager.progress("chatgpt.com:convC") == 0


# ---------------- 附件内容参与比对 ----------------

class TestAttachmentBaseline:
    """附件参与压缩比对的前提是：**页面上下文里真的有这个附件**。"""

    CK = "chatgpt.com:convC"

    def test_page_attachment_is_read_back_as_baseline(self, packager):
        body = REAL_HEAD + "[user]\n" + ("附件里的旧内容。\n" * 3000)
        f = packager.upload_dir / "prompt_1793900000_abcdef.txt"
        f.write_text(body, encoding="utf-8")
        prev, note, src = packager.find_baseline(self.CK, [f.name])
        assert prev == body and src == "page-attachment" and f.name in note

    def test_name_only_in_ide_history_is_not_evidence(self, packager):
        """用户要求现场扫上下文，不能靠 IDE 重放的文本认文件名。

        真实场景：上传被站点拒绝后回退，uploads 里留下一堆 prompt_*.txt，
        客户端下一轮又把"请阅读文件 prompt_xxx.txt"这句原样重放回来。
        文本里有名字只证明客户端又发了一次名字给中转，网页会话里根本没有
        那个附件——按它比对，占位符就指向 AI 看不到的文件，表现为
        "找不到附件、答非所问"（2026-11-06 实测：本地 8 个文件，
        网页对话里出现 0 次）。"""
        body = REAL_HEAD + "[user]\n" + ("残档内容。\n" * 2000)
        f = packager.upload_dir / "prompt_1793900001_beef01.txt"
        f.write_text(body, encoding="utf-8")
        prev, _note, src = packager.find_baseline(self.CK, [])
        assert prev == "", "页面没有这个附件，绝不能当基准"

    def test_registered_but_missing_attachment_discards_baseline(self, packager):
        """上一轮"以为"用附件交付了：附件不在页面上时基准作废、全量重发。

        站点静默拒绝上传（实测该账号 input[type=file] 全为禁用态）时，
        upload_done 复查可能当时通过、下一轮上下文里已经没有它；
        继续拿那份内容当"已交付"省略，AI 就两头都看不到。"""
        body = REAL_HEAD + "[user]\n" + ("附件交付的内容。\n" * 2000)
        f = packager.upload_dir / "prompt_1793900002_cccccc.txt"
        f.write_text(body, encoding="utf-8")
        pkg = pp.Package(original=body, body=body)
        packager.attachments[self.CK] = {"names": [f.name], "label": f.name,
                                         "mode": "attachment"}
        packager.baseline[self.CK] = body
        prev, _note, src = packager.find_baseline(self.CK, [])
        assert prev == "" and src == ""
        assert self.CK not in packager.baseline, "失效基准要被清掉，别再反复误判"

    def test_attachment_still_on_page_keeps_baseline(self, packager):
        body = REAL_HEAD + "[user]\n" + ("附件交付且仍在页面上。\n" * 2000)
        f = packager.upload_dir / "prompt_1793900003_dddddd.txt"
        f.write_text(body, encoding="utf-8")
        packager.attachments[self.CK] = {"names": [f.name], "label": f.name,
                                         "mode": "attachment"}
        packager.baseline[self.CK] = body
        prev, _note, src = packager.find_baseline(self.CK, [f.name])
        assert prev == body and src == "delivery"

    def test_text_delivery_needs_no_page_attachment_check(self, packager):
        """正文/分片交付的基准不依赖附件复核（内容就在消息里）"""
        body = REAL_HEAD + "[user]\n" + ("正文交付。\n" * 2000)
        packager.attachments[self.CK] = {"names": [], "label": "",
                                         "mode": "text"}
        packager.baseline[self.CK] = body
        prev, _note, src = packager.find_baseline(self.CK, [])
        assert prev == body and src == "delivery"

    def test_unattributed_recent_upload_is_not_a_baseline(self, packager):
        """uploads 里"最近的文件"不能当基准：文件本身不带对话标识。

        OpenClaw 并发子代理时最近的文件很可能属于另一个对话，拿它比对就会
        把这个对话从没送达过的内容当成重复省略掉——那是丢内容，不是压得少。"""
        body = REAL_HEAD + "[user]\n" + ("别的对话的内容。\n" * 2000)
        f = packager.upload_dir / "prompt_1793900002_aaaaaa.txt"
        f.write_text(body, encoding="utf-8")
        prev, _note, src = packager.find_baseline(self.CK, [])
        assert prev == "", "不认识这个文件属于本对话，就不许拿来省略"

    def test_missing_files_degrade_to_full_send(self, packager):
        """页面上有附件名，但本地文件已被清理或换机：读不到就全量发送"""
        packager.attachments[self.CK] = {"names": ["prompt_9_zzz.txt"],
                                         "label": "z", "mode": "attachment"}
        prev, _note, src = packager.find_baseline(
            self.CK, ["prompt_1793900009_eeeeee.txt"])
        assert prev == ""

    def test_garbage_page_names_are_ignored(self, packager):
        """页面扫回来的必须是我们的文件名格式，其它一律不用"""
        prev, _n, src = packager.find_baseline(
            self.CK, ["报告.pdf", "notes.txt", "prompt_1_a.txt"])
        assert prev == "" and src == ""

    def test_real_218k_sample_compresses_against_previous_round(self, packager):
        """用现场那份真实 218KB 附件的头部当"上一轮已交付"，
        验证同一份历史在新一轮里会被压成占位符而不是整份再传"""
        real = None
        for f in sorted(glob.glob(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "uploads", "prompt_*.txt")), key=os.path.getmtime):
            if os.path.getsize(f) > 100_000:
                real = f
        if not real:
            pytest.skip("本机 uploads 里没有 >100k 的真实样本（启动时会被清理）")
        with open(real, encoding="utf-8", errors="ignore") as fh:
            delivered = fh.read()
        packager.baseline["chatgpt.com:convC"] = delivered
        new = delivered[:len(delivered) - 200] + "\n新增的一条 tool_result：节点已连接\n"
        pkg = packager.prepare(build_text=lambda m, t: new, messages=_msgs(new),
                               tools=TOOLS, think_hint=False,
                               continuation=True, ckey="chatgpt.com:convC",
                               upload_cfg={"max_prompt_length": 2000,
                                           "max_file_size": 100_000})
        assert pkg.compressed is True
        assert len(pkg.body) < len(new) / 2, (
            f"真实样本没有压下来：{len(new)} → {len(pkg.body)}")


# ---------------- 输入框里必须说清工具有哪些、表在哪 ----------------

class TestComposerText:
    def _pkg(self, packager, strategy):
        pkg = pp.Package(strategy=strategy,
                         payload=({"filename": "prompt_1_x.txt"}
                                  if strategy == pp.STRATEGY_FILE
                                  else {"files": [("/p/a", "a.txt")]}),
                         body="正文")
        pkg.composer_text = packager._composer_text(
            pkg, [], TOOLS, {}, len(pkg.body))
        return pkg.composer_text

    def test_roster_lists_tool_names_inline(self, packager):
        text = self._pkg(packager, pp.STRATEGY_FILE)
        for nm in ("exec", "screen_record", "nodes_status"):
            assert nm in text, f"输入框里没列出 {nm}，模型会以为没有工具"
        assert "tool_call" in text

    def test_no_misleading_pointer_to_conversation_start(self, packager):
        """旧措辞让模型"去对话开头或文件末尾找完整工具表"，
        实测表在文件开头 149 字符处——指错位置就表现为反复 tool_search"""
        text = self._pkg(packager, pp.STRATEGY_FILE)
        assert "对话开头" not in text and "文件末尾" not in text
        assert "文件开头" in text

    def test_chunked_template_counts_files(self, packager):
        text = self._pkg(packager, pp.STRATEGY_CHUNKED)
        assert "1 个分片文件" in text

    def test_direct_strategy_uses_body(self, packager):
        pkg = pp.Package(strategy=pp.STRATEGY_DIRECT, body="完整正文")
        assert packager._composer_text(pkg, [], TOOLS, {}, 4) == "完整正文"

    def test_roster_without_tools_says_so(self):
        assert pp.tool_roster([]) == ""
        assert pp.tool_roster(None) == ""


# ---------------- 协议区豁免 ----------------

class TestProtectRegion:
    def test_protocol_head_protected_from_condense(self, packager):
        """工具表被压成 ".." 后模型看不见声明 → "没有可用工具"（11:43 实锤）"""
        text = REAL_HEAD + "[user]\n" + ("内容。\n" * 500)
        n = packager._protect_len(text, [], TOOLS, False)
        assert 0 < n <= text.find("[user]")
        assert text[:n].startswith("你只作为大语言模型")
        assert '[{"function"' in text[:n], "工具表本体必须整段落在豁免区内"

    def test_no_marker_means_no_protection(self, packager):
        assert packager._protect_len("纯文本没有消息标记", [], None, False) == 0


class TestRootKeyIsNotAConversation:
    """键里没有真实对话 id 时，绝不存基准、也绝不用基准压内容。

    停在根地址（还没 /c/<id>）的两个不同会话共用一个键的话，第二个会话
    会把第一个会话的内容当成"本轮已交付"压成占位符——那是真丢内容。"""

    def test_no_record_without_conversation_id(self, packager):
        pkg = pp.Package(original="历史内容" * 500, body="历史内容" * 500)
        packager.record("chatgpt.com", pkg)
        assert packager.baseline == {}

    def test_no_baseline_lookup_without_conversation_id(self, packager):
        # uploads 里有最近文件，但键没有对话 id → 一律不比对
        f = packager.upload_dir / "prompt_1793900009_ffaaee.txt"
        f.write_text("上一轮真实内容" * 300, encoding="utf-8")
        prev, note, src = packager.find_baseline("chatgpt.com")
        assert prev == "" and src == ""

    def test_prepare_with_root_key_sends_full(self, packager):
        text = REAL_HEAD + "[user]\n" + ("同一份内容。\n" * 4000)
        packager.baseline["chatgpt.com"] = text        # 人为塞进去也要被忽略
        pkg = packager.prepare(build_text=lambda m, t: text, messages=_msgs(text),
                               tools=TOOLS, think_hint=False,
                               continuation=True, ckey="chatgpt.com",
                               upload_cfg={"max_prompt_length": 2000})
        assert pkg.compressed is False


class TestConversationIdParsing:
    def test_extracts_tail_segment(self):
        assert pp.conversation_id("https://chatgpt.com/c/abc123") == "abc123"
        assert pp.conversation_id("https://chatgpt.com/c/abc?x=1") == "abc"
        assert pp.conversation_id("https://chatgpt.com/") == ""
        assert pp.conversation_id("") == ""

    def test_conv_key_shape(self):
        assert pp.conv_key("ChatGPT.com",
                           "https://chatgpt.com/c/x1") == "chatgpt.com:x1"
        assert pp.conv_key("chatgpt.com", "https://chatgpt.com/") == "chatgpt.com"


class TestPageScan:
    """页面附件扫描器：判"上下文里有没有附件"的唯一信息来源"""

    def test_returns_only_names_found_on_page(self):
        import browser_core as bc

        class T:
            def run_js(self, script, *a, **k):
                return ["prompt_1793968220_6429e3.txt", "", None,
                        "prompt_1793970921_3b8414.txt"]

        assert bc.attachment_names_in_page(T()) == [
            "prompt_1793968220_6429e3.txt", "prompt_1793970921_3b8414.txt"]

    def test_scan_failure_is_empty_not_a_guess(self):
        """扫不到就返回空表 → 不压缩，全量发送。绝不用"大概传过"当依据"""
        import browser_core as bc

        class T:
            def run_js(self, script, *a, **k):
                raise RuntimeError("页面被刷新")

        assert bc.attachment_names_in_page(T()) == []

        class T2:
            def run_js(self, script, *a, **k):
                return None

        assert bc.attachment_names_in_page(T2()) == []

    def test_js_targets_leaf_nodes_only(self):
        """只认叶子节点：否则同一个文件名会被祖先节点重复报出来"""
        import browser_core as bc
        assert "childElementCount" in bc._ATTACH_NAMES_IN_PAGE_JS
        assert "prompt_" in bc._ATTACH_NAMES_IN_PAGE_JS

    def test_packager_ignores_non_attachment_names(self, packager):
        """站点正文里什么文本都有：只有我们自己的落盘文件名格式才认"""
        prev, _n, src = packager.find_baseline(
            "chatgpt.com:convC",
            ["上传失败", "prompt_1.txt", "报告.pdf", "notes.txt"])
        assert prev == "" and src == ""


class TestRealSampleEndToEnd:
    """用现场那份真实 218KB 附件跑完整两轮的语义（不发网页，只跑判定链）"""

    def test_second_round_compresses_and_stays_direct(self, packager):
        import browser_core as bc
        first = REAL_HEAD + "[user]\n" + ("同一份长历史。\n" * 6000)
        pkg1 = packager.prepare(
            build_text=lambda m, t: first, messages=_msgs(first),
            tools=TOOLS, think_hint=False, continuation=False,
            ckey="chatgpt.com:convE", upload_cfg={"max_prompt_length": 2000,
                                                  "max_file_size": 100_000})
        assert pkg1.strategy == pp.STRATEGY_FILE, "首轮原文大 → 附件"
        name = "prompt_1793970921_3b8414.txt"
        (packager.upload_dir / name).write_text(first, encoding="utf-8")
        packager.attach_payload("chatgpt.com:convE", pkg1,
                               {"file_path": "x", "filename": name},
                               pp.STRATEGY_FILE)
        packager.mark_delivery("chatgpt.com:convE", "attachment")
        packager.record("chatgpt.com:convE", pkg1)

        # 第二轮：IDE 重放同一份历史 + 新增一条工具结果
        second = first + "\n[assistant]\n已执行\n[tool_result name=exec]\n节点在线\n"
        pkg2 = packager.prepare(
            build_text=lambda m, t: second, messages=_msgs(second),
            tools=TOOLS, think_hint=False, continuation=True,
            ckey="chatgpt.com:convE", page_names=[name],
            upload_cfg={"max_prompt_length": 2000, "max_file_size": 100_000})
        assert pkg2.compressed and pkg2.baseline_source == "delivery"
        assert len(pkg2.body) < len(second) / 5

        # 附件其实没落地（页面扫不到）→ 基准作废、这一轮绝不省略
        pkg3 = packager.prepare(
            build_text=lambda m, t: second, messages=_msgs(second),
            tools=TOOLS, think_hint=False, continuation=True,
            ckey="chatgpt.com:convE", page_names=[],
            upload_cfg={"max_prompt_length": 2000, "max_file_size": 100_000})
        assert pkg3.compressed is False
        assert pkg3.body == second, "页面没有附件时，必须整份发送"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
