"""site_strategy 单测——语料用 2026-11-06 从真实页面抓下来的原文。

现场：
  :9222 acc3（mienb 免费版）对话页脚横幅
      "上下文窗口已达上限 开启新聊天，并带入自动生成的当前对话摘要；或升级套餐…"
  同一账号更早时候（每小时配额打满）：
      "You've reached our limit of messages per hour. Please try again later." + 重试
这两个文案分别对应 CONTEXT_FULL 与 USAGE_LIMITED，处置完全不同：
前者重开对话（账号没错），后者标记账号并切号（重开对话没用）。

负向用例同样重要：我们发出去的 prompt 正文里常有 rate limit / 上下文 之类
字样（日志、报错文本、工具说明），判定若吃整页正文就会把好账号打进冷却。
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_core as bc
import site_strategy as sp

# ---------------- 真实抓取的站点文案 ----------------

# acc3 实时 banner（_qoder_site_probe.py 于 :9222 抓到，原样保留）
REAL_BANNER_CONTEXT_FULL = (
    "正在加载更早的消息… | 最新一条回复 上下文窗口已达上限 开启新聊天，"
    "并带入自动生成的当前对话摘要；或升级套餐，继续当前对话，让每次聊天都"
    "能容纳更多上下文。 新聊天 升级至 Plus 思考 | 上下文窗口已达上限 开启"
    "新聊天，并带入自动生成的当前对话摘要；或升级套餐，继续当前对话，让每"
    "次聊天都能容纳更多上下文。 新聊天 升级至 Plus")

# 免费版每小时配额打满时的站点原文（中英各一）
REAL_HOURLY_LIMIT_EN = ("You've reached our limit of messages per hour. "
                        "Please try again later.")
REAL_HOURLY_LIMIT_ZH = "每小时的消息数已达上限，请稍后再试。"

URL_CHATGPT_CONV = "https://chatgpt.com/c/6aed7776-4fe4-83e8-9442-ee2dfa67f91f"


def _facts(**kw):
    """_PAGE_STATE_JS 的真实返回结构（缺省=可用页面）"""
    base = {"url": URL_CHATGPT_CONV, "err_code": "", "rich_editors": 2,
            "plain_textareas": 0, "login_ctas": 0, "btn_texts": "",
            "banner": "", "head": "", "tail": "", "limit_hits": [],
            "context_full": 0, "limit_text": ""}
    return {**base, **kw}


# ---------------- classify ----------------

class TestClassifyRealWording:
    def test_context_full_banner_real(self):
        """真实抓到的页脚横幅 → context_full（不是账号受限）"""
        f = _facts(banner=REAL_BANNER_CONTEXT_FULL, context_full=1,
                   limit_text="上下文窗口已达上限 开启新聊天")
        assert sp.classify("chatgpt.com", f) == sp.CONTEXT_FULL

    def test_hourly_limit_en_in_retry_neighbour(self):
        """每小时上限那条挂在「重试」按钮近邻 → usage_limited"""
        f = _facts(limit_hits=[REAL_HOURLY_LIMIT_EN + " 重试"],
                   btn_texts="")
        assert sp.classify("chatgpt.com", f) == sp.USAGE_LIMITED

    def test_hourly_limit_zh_in_retry_neighbour(self):
        f = _facts(limit_hits=[REAL_HOURLY_LIMIT_ZH])
        assert sp.classify("chatgpt.com", f) == sp.USAGE_LIMITED

    def test_hourly_limit_in_alert_banner(self):
        """同样文案出现在站点提示区（alert/status）也要命中"""
        f = _facts(banner="通知：" + REAL_HOURLY_LIMIT_EN)
        assert sp.classify("chatgpt.com", f) == sp.USAGE_LIMITED

    def test_ok_page_stays_ok(self):
        assert sp.classify("chatgpt.com", _facts()) == sp.OK

    def test_context_full_and_limit_together_prefers_limit(self):
        """两种横幅同时出现：受限是账号级的，重开对话没用，先按受限走"""
        f = _facts(banner=REAL_BANNER_CONTEXT_FULL, context_full=1,
                   limit_hits=[REAL_HOURLY_LIMIT_EN])
        assert sp.classify("chatgpt.com", f) == sp.USAGE_LIMITED


class TestClassifyNoFalsePositive:
    """我们自己的文本绝不能被当成站点提示"""

    def test_prompt_body_mentioning_rate_limit_is_not_limit(self):
        """真实场景：prompt 正文/工具说明里就带 rate limit 字样，
        判定只吃 banner/limit_hits/btn_texts，正文（head/tail）不参与"""
        f = _facts(
            tail=("错误日志片段：HTTP 429 rate limit exceeded, please try "
                  "again later. 上下文窗口已达上限 请检查 usage limit"))
        assert sp.classify("chatgpt.com", f) == sp.OK

    def test_short_ai_answer_near_retry_button_needs_strong_phrase(self):
        """近邻容器可能落到一条短消息上，只认无歧义措辞：
        "usage limit" 这种技术回答里的词不算，"limit of messages per hour" 才算"""
        f = _facts(limit_hits=["这个接口有 usage limit 限制，建议加退避重试"])
        assert sp.classify("chatgpt.com", f) == sp.OK
        f2 = _facts(limit_hits=["You've reached our limit of messages per hour."])
        assert sp.classify("chatgpt.com", f2) == sp.USAGE_LIMITED

    def test_login_wall_not_usage_limited(self):
        f = _facts(rich_editors=0, plain_textareas=1, login_ctas=2,
                   btn_texts="登录|免费注册",
                   banner="欢迎使用 ChatGPT，登录后即可开始，遇到问题请稍后再试")
        assert sp.classify("chatgpt.com", f) == sp.NEED_LOGIN


class TestClassifyOtherStates:
    def test_error_page(self):
        f = _facts(url="chrome-error://chromewebdata/", err_code="ERR_CONNECTION_RESET")
        assert sp.classify("chatgpt.com", f) == sp.UNREACHABLE

    def test_no_editor(self):
        assert sp.classify("chatgpt.com", _facts(rich_editors=0,
                                                plain_textareas=0)) == sp.NO_EDITOR

    def test_bad_input_never_raises(self):
        """回归：_patterns 曾缺 default 条目，陌生 domain 直接 KeyError，
        上层 except 把它吞成 return False → "上下文已达上限"永不处理"""
        for domain in ("", "unknown.example", None, "https://grok.com/chat"):
            assert sp.classify(domain, _facts()) in (sp.OK, sp.NO_EDITOR)
        assert sp.classify("chatgpt.com", None) == sp.OK
        assert sp.classify("chatgpt.com", {"url": ""}) == sp.NO_EDITOR


# ---------------- host 归一 ----------------

class TestHostOf:
    def test_forms_agree(self):
        for raw in ("chatgpt.com", "https://chatgpt.com", "https://chatgpt.com/c/x?y=1",
                    "WWW.ChatGPT.com/", "http://www.chatgpt.com/c/abc"):
            assert sp.host_of(raw) == "chatgpt.com", raw

    def test_empty(self):
        assert sp.host_of("") == ""
        assert sp.host_of(None) == ""


class TestCooldownLookup:
    def test_subdomain_and_url(self):
        assert sp.cooldown_for("chatgpt.com") == 3600.0
        assert sp.cooldown_for("https://chatgpt.com/c/x") == 3600.0
        assert sp.cooldown_for("unknown.dev") == 1800.0
        assert sp.cooldown_for("") == 1800.0


# ---------------- policy ----------------

class TestPolicy:
    def test_usage_limited_marks_and_rotates(self):
        p = sp.policy(sp.USAGE_LIMITED, "chatgpt.com")
        assert (p.mark_limited, p.rotate, p.new_chat) == (True, True, False)
        assert p.cooldown == 3600.0, "每小时上限 → 冷却 1 小时"

    def test_context_full_new_chat_without_marking(self):
        """上下文超限不是账号的错：重开对话，不许把账号打进冷却"""
        p = sp.policy(sp.CONTEXT_FULL, "chatgpt.com")
        assert (p.mark_limited, p.rotate, p.new_chat) == (False, False, True)

    def test_need_login_rotates_without_marking(self):
        p = sp.policy(sp.NEED_LOGIN, "chatgpt.com")
        assert (p.mark_limited, p.rotate) == (False, True)

    def test_unreachable_is_machine_level(self):
        """机器级问题：切账号无用，别把冷却记到一个根本没坏的账号上"""
        p = sp.policy(sp.UNREACHABLE, "chatgpt.com")
        assert (p.rotate, p.mark_limited, p.resend) == (False, False, False)

    def test_ok_has_no_policy(self):
        assert sp.policy(sp.OK, "chatgpt.com") is None


# ---------------- AccountStateStore ----------------

@pytest.fixture
def store(tmp_path):
    return sp.AccountStateStore(path=str(tmp_path / "site_state.json"))


class TestAccountStateStore:
    def test_mark_then_skip(self, store):
        store.mark_limited("chatgpt.com", 9222, 3600)
        assert store.is_limited("chatgpt.com", 9222) is True
        assert store.remaining("chatgpt.com", 9222) > 3500
        # 同站不同端口互不影响；不同站同端口也互不影响
        assert store.is_limited("chatgpt.com", 9223) is False
        assert store.is_limited("grok.com", 9222) is False

    def test_key_normalisation(self, store):
        """登记用 URL、查询用 domain 必须是同一个账号"""
        store.mark_limited("https://chatgpt.com/c/abc", 9222, 600)
        assert store.is_limited("chatgpt.com", 9222) is True

    def test_expiry_is_self_healing(self, store):
        store.mark_limited("chatgpt.com", 9222, 3600)
        # 模拟时间流逝：把解除时间改成过去，冷却到期后必须自动放行
        store._data["chatgpt.com|9222"] = time.time() - 1
        store._save()
        assert store.is_limited("chatgpt.com", 9222) is False
        store.mark_limited("chatgpt.com", 9223, 60)
        assert "chatgpt.com|9222" not in store.snapshot(), "过期条目要清掉"

    def test_min_cooldown_floor(self, store):
        """站点没给时间也别只冷却 1 秒"""
        until = store.mark_limited("chatgpt.com", 9222, 0)
        assert until - time.time() >= 59

    def test_persist_across_instances(self, tmp_path):
        path = str(tmp_path / "state.json")
        sp.AccountStateStore(path=path).mark_limited("chatgpt.com", 9222, 3600)
        assert sp.AccountStateStore(path=path).is_limited("chatgpt.com", 9222)

    def test_clear(self, store):
        store.mark_limited("chatgpt.com", 9222, 3600)
        assert store.clear("chatgpt.com", 9222) is True
        assert store.is_limited("chatgpt.com", 9222) is False
        assert store.clear("chatgpt.com", 9222) is False

    def test_corrupt_file_degrades_to_empty(self, tmp_path):
        p = tmp_path / "site_state.json"
        p.write_text("{ this is not json", encoding="utf-8")
        s = sp.AccountStateStore(path=str(p))
        assert s.is_limited("chatgpt.com", 9222) is False


# ---------------- browser_core 接线 ----------------

class TestProbeUsesSitePolicy:
    def _tab(self, facts):
        class T:
            url = facts.get("url", URL_CHATGPT_CONV)

            def run_js(self, script, *a, **k):
                return dict(facts)
        return T()

    def test_probe_returns_usage_limited_with_cooldown(self):
        st = bc.probe_page_state(
            self._tab(_facts(limit_hits=[REAL_HOURLY_LIMIT_EN])), "chatgpt.com")
        assert st["state"] == "usage_limited"
        assert st["cooldown"] == 3600.0
        assert "账号受限" in st["detail"]

    def test_probe_keeps_context_full_as_ok_plus_flag(self):
        """页面仍可输入：state 不变，只带标记（换账号救不了上下文超限）"""
        st = bc.probe_page_state(
            self._tab(_facts(banner=REAL_BANNER_CONTEXT_FULL, context_full=1,
                             limit_text="上下文窗口已达上限 开启新聊天")),
            "chatgpt.com")
        assert st["state"] == "ok"
        assert st.get("context_full") is True

    def test_probe_without_domain_still_works(self):
        """回归：domain 缺省曾触发 KeyError，守卫与上限处理一起静默失效"""
        st = bc.probe_page_state(self._tab(_facts()))
        assert st["state"] == "ok"

    def test_guard_raises_for_usage_limited(self):
        ex = object.__new__(bc.WorkflowExecutor)
        ex.tab = self._tab(_facts(limit_hits=[REAL_HOURLY_LIMIT_EN]))
        with pytest.raises(bc.PageUnavailableError) as ei:
            ex._guard_page_usable("填充", domain="chatgpt.com")
        assert ei.value.kind == "usage_limited"

    def test_context_limit_handler_survives_empty_domain(self):
        """回归：这里曾经 except 吞掉 KeyError，导致"上限→新聊天"永不触发"""
        clicks = []

        class T:
            url = URL_CHATGPT_CONV

            def run_js(self, script, *a, **k):
                if "rich_editors" in script:
                    return _facts(context_full=1,
                                  limit_text="上下文窗口已达上限")
                clicks.append(script)
                return "clicked"

        core = object.__new__(bc.BrowserCore)
        core._fp_last_payload = {"fk": "x" * 500}
        core._fp_attach = {"fk": {"names": ["prompt_1_a.txt"]}}
        core._conv_upload_counts = {"chatgpt.com:x": 4}
        assert core._handle_context_limit(
            T(), {"new_chat_btn": 'css:button[aria-label="新聊天"]'}, "fk") is True
        assert clicks, "必须真的点击新聊天"
        assert "fk" not in core._fp_last_payload, "压缩基准必须作废"


class TestRotateAtStrategyStage:
    """受限账号在策略阶段就该被换掉：此时还没发送任何东西，切号不丢内容，
    而 main 的重试循环在异常抛出后已经没有任何重选账号的机会"""

    def _core(self, tmp_path, states, switch_ok=True, cancelled=False):
        class FakeTab:
            def __init__(self, url):
                self.url = url

            def get(self, u):
                self.url = u

        core = object.__new__(bc.BrowserCore)
        core.port = 9222
        core.instance_name = "acc3"
        core._switched = []
        core._idx = 0
        core._states = states
        store_path = str(tmp_path / "site_state.json")
        core_state = sp.AccountStateStore(path=store_path)
        # 用临时文件替换全局单例，避免测试写进真实 site_state.json
        core._state = core_state
        orig_state = sp.account_state
        sp.account_state = core_state
        core._orig_state = orig_state

        def switch_instance(exclude_port=None, exclude_ports=None,
                            site_domain=None, credit_models=None):
            if not switch_ok or core._idx >= len(core._states) - 1:
                return False
            core._idx += 1
            core.port = 9223 + core._idx
            core._switched.append(core.port)
            return True

        core.switch_instance = switch_instance
        core.get_active_tab = lambda: FakeTab("https://chatgpt.com/")
        core._wait_for_composer = (
            lambda tab, domain: core._states[core._idx])
        core._should_stop_checker = lambda: cancelled
        return core

    def _limited(self):
        return {"state": "usage_limited", "detail": "账号受限",
                "cooldown": 3600.0}

    def _ok(self):
        return {"state": "ok", "detail": "", "raw": {}}

    def test_limited_account_marked_then_rotated(self, tmp_path):
        core = self._core(tmp_path, [self._limited(), self._ok()])
        tab, st = core._rotate_to_usable_account(
            "chatgpt.com", "https://chatgpt.com", self._limited())
        assert st["state"] == "ok"
        assert core.port != 9222 and core._switched, "必须真的切到别的账号"
        assert core._state.is_limited("chatgpt.com", 9222) is True
        assert core._state.until("chatgpt.com", 9222) > time.time()
        sp.account_state = core._orig_state

    def test_all_accounts_limited_gives_up_with_state(self, tmp_path):
        core = self._core(tmp_path, [self._limited(), self._limited()])
        tab, st = core._rotate_to_usable_account(
            "chatgpt.com", "https://chatgpt.com", self._limited())
        assert st["state"] == "usage_limited", "最后一个状态要原样带回给调用方报错"
        assert core._state.is_limited("chatgpt.com", core.port) is True
        sp.account_state = core._orig_state

    def test_no_switch_available_reports_original_state(self, tmp_path):
        core = self._core(tmp_path, [self._limited()], switch_ok=False)
        tab, st = core._rotate_to_usable_account(
            "chatgpt.com", "https://chatgpt.com", self._limited())
        assert st["state"] == "usage_limited"
        assert core._switched == []
        sp.account_state = core._orig_state

    def test_client_cancel_stops_rotating(self, tmp_path):
        """取消后继续切号 = 拉起新 Chrome + 关旧窗口的连环事故来源"""
        core = self._core(tmp_path, [self._limited(), self._ok()],
                          cancelled=True)
        tab, st = core._rotate_to_usable_account(
            "chatgpt.com", "https://chatgpt.com", self._limited())
        assert core._switched == [] and st["state"] == "usage_limited"
        sp.account_state = core._orig_state


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
