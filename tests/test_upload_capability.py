"""待办 6：上传能力提前探测 + 登记，不再每轮空烧一次上传。

现场（2026-11-06，:9222 那个留着龙虾对话的窗口）：chatgpt.com composer 的
4 个 `input[type=file]` 全部 disabled。原先每个长 prompt 都要
"注入 → 等 12s 发现附件没出现 → 刷新页面清残留 → 回退直输"，
而刷新还会打断这一轮的发送。现在策略阶段先探一次、把"该账号没有上传能力"
登记 15 分钟，期间直接走正文/分片，根本不再尝试附件。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_core as bc
import prompt_packager as pp
import site_strategy as sp


@pytest.fixture
def packager(tmp_path):
    up = tmp_path / "uploads"
    up.mkdir()
    return pp.PromptPackager(upload_dir=str(up),
                             state_file=str(tmp_path / "pack_state.json"))


# 协议区开头（与现场附件里的真实文本同构）：工具表 + 转发格式
REAL_HEAD = (
    "你只作为大语言模型（你是ai中间人），禁止调用你自生的工具或除文本"
    "以外的内容（下游会识别工具并调用），严格按照我的格式要求生成工具"
    "和调用内容。转发指令格式（逐字照抄结构）：\n\n```tool_call\n"
    '{"name": "工具名", "arguments": {"参数名": "值"}}\n```\n\n'
    "可转发的目标（name 大小写敏感，完整定义 JSON 如下）：\n"
    '[{"function":{"name":"exec","description":"Run a shell command"}}]\n\n')


# ---------------- accept / disabled 打分（规则只此一份） ----------------

class TestUploadPortScore:
    def test_disabled_port_is_dead_even_with_open_accept(self):
        """实测就是这种：accept 为空看着最可用，但 disabled → 注入不报错、
        附件永不出现。判死才有机会提前改路由。"""
        assert bc.upload_port_score(
            {"accept": "", "disabled": True, "in_focused_editor_form": True},
            "txt") == -9.0

    def test_empty_accept_in_focused_composer_is_best(self):
        assert bc.upload_port_score(
            {"accept": "", "in_focused_editor_form": True, "multiple": True},
            "txt") > 6.0

    def test_image_only_port_cannot_take_txt(self):
        assert bc.upload_port_score({"accept": "image/*"}, "txt") < 0

    def test_image_only_port_can_take_png(self):
        assert bc.upload_port_score({"accept": "image/*"}, "png") > 0

    def test_stale_composer_port_penalised(self):
        """点过新聊天后旧输入区的口还在 DOM 里，注入它同样静默失败"""
        fresh = bc.upload_port_score(
            {"accept": "", "in_focused_editor_form": True}, "txt")
        stale = bc.upload_port_score(
            {"accept": "", "in_editor_form": True}, "txt")
        assert fresh > stale


class TestProbeUploadCapability:
    def test_all_disabled_means_no_upload(self):
        class T:
            def run_js(self, script, *a, **k):
                return [{"accept": "image/*", "disabled": True},
                        {"accept": "image/*,video/*", "disabled": True},
                        {"accept": "", "disabled": True},
                        {"accept": "image/*", "disabled": True}]

        cap = bc.probe_upload_capability(T())
        assert cap["upload"] is False
        assert "禁用" in cap["reason"]

    def test_one_usable_port_is_enough(self):
        class T:
            def run_js(self, script, *a, **k):
                return [{"accept": "image/*", "disabled": True},
                        {"accept": "", "in_focused_editor_form": True}]

        assert bc.probe_upload_capability(T())["upload"] is True

    def test_probe_failure_assumes_available(self):
        """探针坏掉不能把附件能力判死——最多退回"试一次"的旧行为，
        不能让正常账号失去上传"""
        class T:
            def run_js(self, script, *a, **k):
                raise RuntimeError("页面被刷新")

        assert bc.probe_upload_capability(T())["upload"] is True

    def test_no_ports_at_all_assumes_available(self):
        class T:
            def run_js(self, script, *a, **k):
                return []

        assert bc.probe_upload_capability(T())["upload"] is True


# ---------------- 能力登记（site_strategy） ----------------

class TestCapabilityState:
    def test_mark_then_unavailable_until_expiry(self, tmp_path):
        st = sp.AccountStateStore(path=str(tmp_path / "caps.json"),
                                  namespace="cap")
        st.mark_limited(sp._cap_domain("upload", "chatgpt.com"), 9222, 900,
                        what="缺能力[upload]")
        assert st.is_limited(sp._cap_domain("upload", "chatgpt.com"), 9222)
        assert st.until(sp._cap_domain("upload", "chatgpt.com"), 9222) > 0

    def test_cap_key_separates_capability_and_site(self):
        """upload 与别的能力、chatgpt 与别的站点互不干扰"""
        assert sp._cap_domain("upload", "https://chatgpt.com/c/x") \
            == "upload@chatgpt.com"
        assert sp._cap_domain("upload", "grok.com") != \
            sp._cap_domain("upload", "chatgpt.com")

    def test_cap_store_is_a_different_file(self):
        """能力缺失与账号受限语义不同，冷却时长也不同，不能挤进同一份登记"""
        assert sp._CAP_FILE != sp._STATE_FILE
        assert sp.capability_state.namespace == "cap"

    def test_upload_cooldown_is_bounded(self):
        assert 60 <= sp.cap_cooldown("upload") <= 3600


class TestStrategyStageUsesCapability:
    def _core(self, monkeypatch, tmp_path, probe_result, caps=None):
        core = object.__new__(bc.BrowserCore)
        core.port = 9222
        state = sp.AccountStateStore(path=str(tmp_path / "caps.json"),
                                     namespace="cap")
        monkeypatch.setattr(sp, "capability_state", state)
        monkeypatch.setattr(bc.site_policy, "capability_state", state)
        calls = []

        def fake_probe(tab, ext="txt"):
            calls.append(1)
            return probe_result

        monkeypatch.setattr(bc, "probe_upload_capability", fake_probe)
        return core, calls

    def test_unavailable_port_marks_and_degrades(self, monkeypatch, tmp_path):
        core, calls = self._core(
            monkeypatch, tmp_path,
            {"upload": False, "ports": [], "reason": "4 个上传口全部禁用"})
        assert core._upload_capability(object(), "chatgpt.com") is False
        assert sp.capability_state.is_limited(
            sp._cap_domain("upload", "chatgpt.com"), 9222)

    def test_cached_missing_capability_skips_probing(self, monkeypatch, tmp_path):
        """登记有效期内不再碰页面：每轮一次全页扫描本身就值钱"""
        core, calls = self._core(
            monkeypatch, tmp_path, {"upload": True, "ports": [], "reason": ""})
        sp.capability_state.mark_limited(
            sp._cap_domain("upload", "chatgpt.com"), 9222, 600, what="x")
        assert core._upload_capability(object(), "chatgpt.com") is False
        assert calls == [], "冷却期内不该再探测"

    def test_available_stays_available(self, monkeypatch, tmp_path):
        core, calls = self._core(
            monkeypatch, tmp_path,
            {"upload": True, "ports": [{"accept": ""}], "reason": ""})
        assert core._upload_capability(object(), "chatgpt.com") is True
        assert len(calls) == 1

    def test_runtime_failure_marks_for_next_round(self, monkeypatch, tmp_path):
        """实测失败一次就登记：下一轮策略阶段直接不选附件"""
        core, _ = self._core(monkeypatch, tmp_path,
                             {"upload": True, "ports": [], "reason": ""})
        core.mark_upload_unavailable("chatgpt.com", "附件未出现在页面")
        assert sp.capability_state.is_limited(
            sp._cap_domain("upload", "chatgpt.com"), 9222)


# ---------------- 打包决策真的被能力改变 ----------------

class TestPlanWithoutUpload:
    def _pkg(self, packager, size, upload_available, chunk_limit=12000,
             max_chunks=30):
        text = "内容" * (size // 2)
        return packager.prepare(
            build_text=lambda m, t: text, messages=[{"role": "user",
                                                     "content": text}],
            tools=None, think_hint=False, continuation=False,
            ckey="chatgpt.com:conv1", upload_available=upload_available,
            upload_cfg={"max_prompt_length": 2000, "max_file_size": 100_000},
            chunk_limit=chunk_limit, max_chunks=max_chunks)

    def test_no_upload_capability_never_picks_file(self, packager):
        """原本站点上限会选 file；知道没上传能力后必须改道，不再空试一次"""
        pkg = self._pkg(packager, 30000, True)
        assert pkg.strategy == pp.STRATEGY_FILE
        pkg2 = self._pkg(packager, 30000, False)
        assert pkg2.strategy == pp.STRATEGY_DIRECT, (
            "没有上传能力还选 file = 每轮白烧 12s 上传 + 一次页面刷新")

    def test_huge_body_without_upload_raises_actionable_error(self, packager):
        with pytest.raises(pp.TooLongPlan) as ei:
            self._pkg(packager, 12000 * 40, False)
        assert "没有上传能力" in str(ei.value)

    def test_upload_available_flag_does_not_break_small_prompt(self, packager):
        text = "很短"
        pkg = packager.prepare(build_text=lambda m, t: text,
                               messages=[{"role": "user", "content": text}],
                               tools=None, think_hint=False,
                               continuation=False, ckey="chatgpt.com:conv1",
                               upload_available=False,
                               upload_cfg={"max_prompt_length": 2000})
        assert pkg.strategy == pp.STRATEGY_DIRECT
        assert pkg.composer_text == text

class TestDontCompressWhatFitsAnyway:
    """"能压"不等于"该压"：单条装得下的内容压成占位符只是让模型读不懂。

    现场：4,051 字符被压到 108 字符后，模型回"请发送完整消息。"——
    压缩的意义在于避免十几万字符切成几十片，不在省这几秒输入。"""

    def _two_rounds(self, packager, chunk_limit, size_units):
        first = REAL_HEAD + "[user]\n" + ("同一份历史。\n" * size_units)
        p1 = packager.prepare(build_text=lambda m, t: first,
                              messages=[{"role": "user", "content": first}],
                              tools=None, think_hint=False,
                              continuation=False, ckey="chatgpt.com:convT",
                              upload_cfg={"max_prompt_length": 2000},
                              chunk_limit=chunk_limit)
        packager.record("chatgpt.com:convT", p1)
        second = first + "\n[assistant]\n已执行\n[tool_result name=exec]\n在线\n"
        p2 = packager.prepare(build_text=lambda m, t: second,
                              messages=[{"role": "user", "content": second}],
                              tools=None, think_hint=False,
                              continuation=True, ckey="chatgpt.com:convT",
                              upload_cfg={"max_prompt_length": 2000},
                              chunk_limit=chunk_limit)
        return p1, p2

    def test_small_body_is_sent_whole(self, packager):
        """约 4k 字符（片长上限 12000）→ 不压缩，正文原样发"""
        _p1, p2 = self._two_rounds(packager, chunk_limit=12000, size_units=400)
        assert p2.compressed is False
        assert p2.body == p2.original

    def test_large_body_still_compressed(self, packager):
        """明显会切成多片的体量才压"""
        _p1, p2 = self._two_rounds(packager, chunk_limit=12000, size_units=6000)
        assert p2.compressed is True
        assert len(p2.body) < len(p2.original) / 2


class TestStaleAttachmentNameIsNotCited:
    """上传失败后登记的附件名必须清掉，否则占位符会指向一个不存在的附件"""

    def test_text_delivery_clears_registered_names(self, packager):
        ck = "chatgpt.com:convS"
        packager.attachments[ck] = {
            "names": ["prompt_1793974991_b4fe40.txt"],
            "label": "prompt_1793974991_b4fe40.txt",
            "mode": "attachment-pending"}
        packager.mark_delivery(ck, "text")
        info = packager.attachments[ck]
        assert info["mode"] == "text" and info["names"] == [] \
            and info["label"] == ""

    def test_ref_note_empty_after_failed_upload(self, packager):
        ck = "chatgpt.com:convR"
        first = REAL_HEAD + "[user]\n" + ("同一份历史。\n" * 6000)
        p1 = packager.prepare(build_text=lambda m, t: first,
                              messages=[{"role": "user", "content": first}],
                              tools=None, think_hint=False,
                              continuation=False, ckey=ck,
                              upload_cfg={"max_prompt_length": 2000},
                              chunk_limit=12000)
        packager.attach_payload(ck, p1,
                               {"file_path": "x",
                                "filename": "prompt_1793974991_b4fe40.txt"},
                               pp.STRATEGY_FILE)
        packager.mark_delivery(ck, "text")     # 实际是回退正文发送
        packager.record(ck, p1)
        second = first + "\n[assistant]\n已执行\n[tool_result name=exec]\n在线\n"
        p2 = packager.prepare(build_text=lambda m, t: second,
                              messages=[{"role": "user", "content": second}],
                              tools=None, think_hint=False,
                              continuation=True, ckey=ck, page_names=[],
                              upload_cfg={"max_prompt_length": 2000},
                              chunk_limit=12000)
        assert p2.compressed is True
        assert p2.ref_note == "", "不该再引用那个从未落地的附件"


class TestSiteSettingPrecedence:
    """站点 → 全局 → 代码默认，只此一处实现"""

    def test_site_wins_over_global(self):
        assert bc.site_setting({'CHUNK_CHARS': 8000}, 'CHUNK_CHARS',
                               'DIRECT_CHUNK_CHARS', 12000) == 8000

    def test_global_used_when_site_absent(self):
        assert bc.site_setting({}, 'CHUNK_CHARS', 'DIRECT_CHUNK_CHARS',
                               12000) == 12000

    def test_blank_string_counts_as_absent(self):
        """面板留空会被存成 ''，必须理解为"跟随全局"而不是 0"""
        assert bc.site_setting({'MAX_CHUNKED_CHUNKS': ''},
                               'MAX_CHUNKED_CHUNKS', 'MAX_CHUNKED_CHUNKS',
                               30) == 30

    def test_bool_parses_text_false(self):
        assert bc.site_setting({'CONDENSE_ENABLED': 'false'},
                               'CONDENSE_ENABLED', 'CONDENSE_ENABLED',
                               True) is False
        assert bc.site_setting({'CONDENSE_ENABLED': True},
                               'CONDENSE_ENABLED', 'CONDENSE_ENABLED',
                               True) is True

    def test_garbage_number_falls_back(self):
        assert bc.site_setting({'CHUNK_CHARS': '很多'}, 'CHUNK_CHARS',
                               'DIRECT_CHUNK_CHARS', 12000) == 12000

    def test_defaults_registered_globally(self):
        d = bc.BrowserConstants.get_defaults()
        assert d['CONDENSE_MIN_CHARS'] == 0
        assert d['CONDENSE_ENABLED'] is True


class TestChunkedSendHonorsSiteCaps:
    """分片路径也要用站点级片长/片数，不能退回另一套默认值"""

    def test_context_values_are_used(self):
        import tests.test_chunked_send as tcs
        ex = tcs.FakeExecutor(reject_over=99999)
        auto = bc.BrowserCore.__new__(bc.BrowserCore)
        auto._should_stop_checker = lambda: False
        auto._wait_generation_settled = lambda e, timeout=90: None
        ctx = {"prompt": "X" * 50000, "session_fk": "",
               "chunk_limit": 20000, "max_chunked_chunks": 2}
        with pytest.raises(bc.TooLongError) as ei:
            list(bc.BrowserCore._chunked_direct_send(auto, ex, ctx,
                  {"input_box": "#in", "send_btn": "#send"},
                  chunk_limit=None))
        assert "最多 2 片" in str(ei.value)
        assert "≤20000" in str(ei.value), "片长也要用站点值"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
