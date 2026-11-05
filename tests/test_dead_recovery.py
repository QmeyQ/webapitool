"""回复异常分类决策与重发测试"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from browser_core import WorkflowExecutor


class TestDeadRecoveryAction:
    def test_below_limit_no_action(self):
        assert WorkflowExecutor._dead_recovery_action(
            10, 30, False, False, True) is None

    def test_retry_button_wins(self):
        # 有重试按钮 → 点重试（不管是否已重发过）
        assert WorkflowExecutor._dead_recovery_action(
            35, 30, True, False, True) == 'click_retry'
        assert WorkflowExecutor._dead_recovery_action(
            130, 120, True, True, True) == 'click_retry'

    def test_resend_once_when_no_retry_btn(self):
        # 无重试按钮、没重发过、有 payload → 重发
        assert WorkflowExecutor._dead_recovery_action(
            35, 30, False, False, True) == 'resend'

    def test_break_after_resend(self):
        # 重发过仍死 → 交 worker 换实例
        assert WorkflowExecutor._dead_recovery_action(
            35, 30, False, True, True) == 'break'

    def test_break_without_payload(self):
        # 没有 payload（无重发能力）→ 直接 break
        assert WorkflowExecutor._dead_recovery_action(
            35, 30, False, False, False) == 'break'


class FakeTab:
    def __init__(self, has_retry=False):
        self.has_retry = has_retry
        self.clicked = 0

    def run_js(self, js, *a):
        if 'b.click()' in js:
            self.clicked += 1
            return self.has_retry
        if 'return false;' in js and 'querySelectorAll' in js:
            return self.has_retry
        return False


def _mk(auto_with=None):
    auto = WorkflowExecutor.__new__(WorkflowExecutor)
    auto.tab = FakeTab()
    return auto


class TestDetectAndClick:
    def test_detect_retry(self):
        auto = _mk()
        auto.tab = FakeTab(has_retry=True)
        assert auto._detect_retry_button() is True

    def test_detect_no_retry(self):
        auto = _mk()
        assert auto._detect_retry_button() is False

    def test_click_retry(self):
        auto = _mk()
        auto.tab = FakeTab(has_retry=True)
        assert auto._click_page_retry_button() is True
        assert auto.tab.clicked == 1


class TestResend:
    def test_resend_uses_recorded_payload(self):
        auto = _mk()
        auto._last_send_payload = 'HELLO-PAYLOAD'
        auto._last_send_input_sel = '#in'
        auto._last_send_btn_sel = '#send'
        fills = []

        auto._execute_fill = (lambda sel, text, target_key=None,
                              optional=False: fills.append((sel, text)))
        clicks = []
        auto._execute_click = (lambda sel, target_key=None,
                               optional=False: clicks.append(sel))
        assert auto._resend_last_message() is True
        assert fills == [('#in', 'HELLO-PAYLOAD')]
        assert clicks == ['#send']

    def test_resend_without_payload_fails(self):
        auto = _mk()
        assert auto._resend_last_message() is False

    def test_resend_fill_error_returns_false(self):
        auto = _mk()
        auto._last_send_payload = 'X'
        auto._last_send_input_sel = '#in'
        auto._last_send_btn_sel = '#send'

        def _boom(*a, **k):
            raise RuntimeError('fill fail')
        auto._execute_fill = _boom
        assert auto._resend_last_message() is False


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
