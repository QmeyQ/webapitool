"""分片发送 _chunked_direct_send 单元测试（fake executor，不碰真实网页）"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_core import BrowserCore, WorkflowError, ElementNotFoundError


class FakeTab:
    def __init__(self, reject_over=8000):
        self.url = 'https://chatgpt.com/c/test'
        self.reject_over = reject_over
        self.clicked = []

    def run_js(self, js, *a):
        # 拒收检测 JS：根据最近填充的长度决定是否拒收
        last = getattr(self, '_last_fill_len', 0)
        if '你提交的消息过长' in js or 'data-turn-key' in js:
            return last > self.reject_over
        return False


class FakeExecutor:
    def __init__(self, fail_click_on=None, fail_verify_on=None,
                 reject_over=8000):
        self.tab = FakeTab(reject_over=reject_over)
        self.filled = []      # 实际写入输入框的（含被拒收的）
        self.sent = []        # 点击后真正进入会话的（拒收的不算）
        self.clicked = []
        self.fail_click_on = fail_click_on or set()
        self.fail_verify_on = fail_verify_on or set()
        self.released = []
        self.settled = 0
        self.cancel = False
        self._fill_submitted = False

    def _should_stop_checker(self):
        return self.cancel

    def _execute_fill(self, selector, text, target_key=None, optional=False,
                      msg_selector=""):
        self.filled.append(text)
        self.tab._last_fill_len = len(text)

    def _capture_send_state(self, input_sel, msg_sel):
        return {'i': 0, 'm': 0}

    def _capture_pre_stream_count(self, sel=""):
        self.pre_stream_count = 1

    def execute_step(self, action=None, selector=None, target_key=None,
                     value=None, optional=False, context=None):
        # 真实 execute_step 是生成器函数——调用而不迭代就什么都不会发生
        # （2026-11-05 poe 事故根因）。fake 必须保持生成器签名，
        # 否则测试永远抓不住"只调用不迭代"的回归。
        self.clicked.append(target_key)
        if len(self.clicked) in self.fail_click_on:
            raise ElementNotFoundError('send btn not found')
        # 模拟 chatgpt：超过限长的消息点击后被拒收（未进入会话）
        if self.tab._last_fill_len <= self.tab.reject_over:
            self.sent.append(self.filled[-1])
        yield

    def _verify_and_recover_send(self, executor, selectors, pre):
        if len(self.clicked) in self.fail_verify_on:
            raise WorkflowError('发送未生效')
        yield

    def _wait_generation_settled(self, executor, timeout=90):
        self.settled += 1


def _run_chunks(prompt, limit=12000, ex=None, chunk_limit=None, **ex_kw):
    ex = ex or FakeExecutor(**ex_kw)
    auto = BrowserCore.__new__(BrowserCore)
    auto._should_stop_checker = lambda: ex.cancel
    auto._fp_bind_release = lambda reason='': ex.released.append(reason)
    # 替换真实等待（会在假 tab 上空转到 90s 超时）
    auto._wait_generation_settled = (
        lambda executor, timeout=90: setattr(
            executor, 'settled', executor.settled + 1))
    gen = BrowserCore._chunked_direct_send(
        auto, ex, {'prompt': prompt},
        {'input_box': '#in', 'send_btn': '#send'},
        chunk_limit=chunk_limit)
    list(gen)
    return ex


def test_two_chunks_order_and_headings():
    prompt = 'X' * 15000
    ex = _run_chunks(prompt, reject_over=99999)
    assert len(ex.filled) == 2
    h1, h2 = ex.filled[0], ex.filled[1]
    assert '尚未发送完' in h1[:40] and '请勿回复' in h1[:40]
    assert '已全部发送完毕' in h2[:40] and '统一回复' in h2[:40]
    # 中间片发完才等生成平息，最后一片不等
    assert ex.settled == 1


def test_no_content_loss_normal_split():
    prompt = ''.join(f'段落{i}，编号 SIG{i}。' + '内容' * 300
                     for i in range(1, 120))
    ex = _run_chunks(prompt, limit=12000, reject_over=99999)
    # 真正进入会话的内容（去掉分片头）拼起来 == 原文
    import re
    bodies = [re.sub(r'^\[长消息分片 \d+/\d+\][^\n]*\n\n', '', t)
              for t in ex.sent]
    assert ''.join(bodies) == prompt
    assert len(ex.sent) >= 2


def test_reject_resplits_without_loss():
    prompt = ''.join(f'段落{i}，编号 OMEGA{i}。' + '内容' * 300
                     for i in range(1, 120))
    ex = _run_chunks(prompt, limit=12000, reject_over=8000)
    # 第一片被拒 → 减半重切 → 进入会话的内容一个字都不能丢
    import re
    bodies = [re.sub(r'^\[长消息分片 \d+/\d+\][^\n]*\n\n', '', t)
              for t in ex.sent]
    assert ''.join(bodies) == prompt
    assert len(ex.sent) >= 3
    # 被拒收的那次 fill 不应进入会话
    assert all(len(t) <= 8000 + 200 for t in ex.sent)


def test_chunk_limit_param_overrides_default():
    """站点级 CHUNK_CHARS 生效：15000 文本按 8000 切 2 片"""
    prompt = 'X' * 15000
    ex = _run_chunks(prompt, chunk_limit=8000, reject_over=99999)
    assert len(ex.sent) == 2
    import re
    bodies = [re.sub(r'^\[长消息分片 \d+/\d+\][^\n]*\n\n', '', t)
              for t in ex.sent]
    assert ''.join(bodies) == prompt


def test_click_failure_releases_fp():
    ex = FakeExecutor(reject_over=99999)
    ex.fail_click_on = {1}
    with pytest.raises(WorkflowError):
        _run_chunks('X' * 15000, ex=ex)
    assert ex.released and '发送失败' in ex.released[0]
    assert len(ex.clicked) == 1


def test_verify_failure_releases_fp():
    ex = FakeExecutor(reject_over=99999)
    ex.fail_verify_on = {1}
    with pytest.raises(WorkflowError):
        _run_chunks('X' * 15000, ex=ex)
    assert ex.released and '校验失败' in ex.released[0]


def test_cancel_stops_cleanly():
    prompt = 'X' * 30000
    ex2 = FakeExecutor()
    auto = BrowserCore.__new__(BrowserCore)
    ex2.cancel = True
    auto._should_stop_checker = lambda: True
    auto._fp_bind_release = lambda reason='': None
    gen = BrowserCore._chunked_direct_send(
        auto, ex2, {'prompt': prompt}, {'input_box': '#in', 'send_btn': '#s'})
    list(gen)
    assert len(ex2.clicked) == 0


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
