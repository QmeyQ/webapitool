"""_execute_fill 的"填充期间已提交"检测单测（poe 2026-11-04 事故回归）。

场景：Enter 发送型站点，fill 的输入遇文本换行直接提交，
输入框被清空 → 读回 0。修复前误判"残留旧文本"强制重写 → 幽灵
文本 → 双倍粘贴 + 点击无效 + 校验误判失败。

主路径现为 CDP Input.insertText；检测逻辑在 ele.input 兜底路径
（cdp_ok=False 模拟）与读回校验层，两种路径都覆盖。
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_core import WorkflowExecutor


class FakeEle:
    def __init__(self, on_input=None, land_from=1):
        self.input_calls = 0
        self._on_input = on_input
        self._land_from = land_from

    def clear(self):
        pass

    def input(self, text):
        self.input_calls += 1
        if self._on_input and self.input_calls >= self._land_from:
            self._on_input(text)


class FakeFinder:
    def __init__(self, on_input=None, land_from=1):
        self.ele = FakeEle(on_input=on_input, land_from=land_from)

    def find_with_fallback(self, selector, target_key):
        return self.ele


class FakeTab:
    """run_cdp 可用性由 cdp_ok 控制（模拟后台窗口 CDP 失效）；
    可用时 Input.insertText 注入文本并计入 cdp_writes。"""

    def __init__(self, cdp_ok=True):
        self.set = types.SimpleNamespace(activate=lambda: None)
        self.cdp_ok = cdp_ok
        self.cdp_writes = 0
        self.expected = ""

    def run_js(self, js, *a):
        return self.expected

    def run_cdp(self, *a, **k):
        if not self.cdp_ok:
            raise RuntimeError("cdp unavailable")
        if a and a[0] == "Input.insertText":
            self.expected = k.get("text") or (a[1] if len(a) > 1 else "")
            self.cdp_writes += 1
        return None


def _make_core(msg_counts, cdp_ok=True, rewrite_lands=False):
    """msg_counts: 每次 _capture_send_state 依次返回的 msg_count。
    rewrite_lands=True 时 ele.input 重写会把文本写入读回（模拟重写成功）。"""
    core = WorkflowExecutor.__new__(WorkflowExecutor)
    core.tab = FakeTab(cdp_ok=cdp_ok)
    core.stealth_mode = False
    core._check_cancelled = lambda: False
    core._smart_delay = lambda a, b: None

    def _write(text):
        core.tab.expected = text

    core.finder = FakeFinder(on_input=_write if rewrite_lands else None,
                             land_from=2)
    it = iter(msg_counts)
    core._capture_send_state = (
        lambda input_sel, msg_sel="":
        {"input_len": 0, "msg_count": next(it), "turn_count": -1,
         "url": ""})
    return core


def test_fill_submitted_detected_and_skips_rewrite():
    """CDP 不可用时走 ele.input 兜底；fill 期间消息已发出（基线 0 →
    现在 2）：置 _fill_submitted，不做重写。"""
    core = _make_core([0, 2], cdp_ok=False)
    r = core._execute_fill("#in", "[长消息分片 1/2] 头\n\n正文A\n\n正文B",
                           target_key="input_box", optional=False,
                           msg_selector="[class*='Message_row']")
    assert core._fill_submitted is True
    assert r is True
    assert core.tab.cdp_writes == 0  # 未触发重写
    assert core.finder.ele.input_calls == 1  # 仅主兜底，无重写


def test_fill_empty_readback_without_growth_still_rewrites():
    """读回空但消息数没增长：走原有重写路径（再次 ele.input）。"""
    core = _make_core([0, 0], cdp_ok=False, rewrite_lands=True)
    core._execute_fill("#in", "长文本" * 100, target_key="input_box",
                       optional=False, msg_selector="[class*='Message_row']")
    assert core._fill_submitted is False
    assert core.finder.ele.input_calls >= 2  # 写入 + 重写


def test_fill_normal_readback_no_detection():
    """insertText 主路径读回正常：不触发检测分支（基线只采一条）。"""
    core = _make_core([], cdp_ok=True)
    counts = []
    core._capture_send_state = (
        lambda input_sel, msg_sel="": counts.append(1)
        or {"input_len": 3, "msg_count": 0, "turn_count": -1, "url": ""})
    core._execute_fill("#in", "短消息", target_key="input_box",
                       optional=False, msg_selector="msgsel")
    assert core._fill_submitted is False
    assert core.tab.cdp_writes == 1  # 主路径 insertText 落框
    assert len(counts) == 1  # 读回匹配，未采第二条状态


def test_fill_no_msg_selector_keeps_old_behavior():
    """不传 msg_selector：无检测（旧行为），读回空仍走重写。"""
    core = _make_core([0], cdp_ok=False, rewrite_lands=True)
    core._execute_fill("#in", "长文本" * 100, target_key="input_box",
                       optional=False)
    assert core._fill_submitted is False
    assert core.finder.ele.input_calls >= 2  # 写入 + 重写


def test_fill_insert_text_main_path_lands_clean():
    """insertText 主路径：文本完整落框（读回匹配），零重写。"""
    core = _make_core([], cdp_ok=True)
    text = "[分片头] HEAD\n\n" + "正文内容。" * 500
    core._execute_fill("#in", text, target_key="input_box",
                       optional=False, msg_selector="msgsel")
    assert core._fill_submitted is False
    assert core.tab.cdp_writes == 1
    # 去空白归一化后应与原文一致
    norm = lambda s: __import__("re").sub(r"\s+", "", s or "")
    assert norm(core.tab.expected) == norm(text)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
