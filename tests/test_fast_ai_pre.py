"""_fast_ai_pre_arrived（AI 秒回先于流监听基线）检测单测。

场景：poe 等站点 AI 秒回（~2s），发送校验+拒收检查耗完时完整回复
已渲染完毕——流监听基线已包含回复，计数不会再增长，按"增长"判定
白等 180s 误判超时（2026-11-05 poe 分片测试实测三轮烧号）。"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_core import WorkflowExecutor


def _core(pre_count, payload):
    core = WorkflowExecutor.__new__(WorkflowExecutor)
    core._pre_stream_count = pre_count
    core._last_send_payload = payload
    return core


SNAP = lambda count, text: {"groups_count": count, "text": text}


def test_ai_reply_already_on_screen():
    """两片发出后 AI 已回复"已收到全部分片"（基线 6 > 发送前 0，
    最后一条不含 payload 开头）→ 判定秒回。"""
    core = _core(0, "[长消息分片 2/2] 以上消息已全部发送完毕，"
                 "请根据全部内容统一回复。\n\n正文TAIL9")
    assert core._fast_ai_pre_arrived(SNAP(6, "已收到全部分片")) is True


def test_last_message_is_our_user_chunk():
    """最后一条是我们的用户消息（含 payload 开头）→ 不触发，
    继续等 AI。"""
    payload = "[长消息分片 2/2] 以上消息已全部发送完毕。\n\nTAIL9"
    core = _core(0, payload)
    assert core._fast_ai_pre_arrived(
        SNAP(6, payload[:60] + "查看更多")) is False


def test_count_not_beyond_pre_baseline():
    """计数未超过发送前基线（如多轮旧消息场景）→ 不触发。"""
    core = _core(6, "老问题 TAIL9")
    assert core._fast_ai_pre_arrived(SNAP(6, "已收到全部分片")) is False


def test_no_pre_baseline_or_payload():
    """无发送前基线（-1）或无 payload → 永不触发（旧行为）。"""
    core = _core(-1, "消息")
    assert core._fast_ai_pre_arrived(SNAP(6, "已收到全部分片")) is False
    core2 = _core(0, "")
    assert core2._fast_ai_pre_arrived(SNAP(6, "已收到全部分片")) is False


def test_whitespace_normalized_head_match():
    """payload 与消息文本的空白差异需归一化后再比较。"""
    payload = "[长消息分片 1/2]  消息尚未发送完\n\n请勿回复。"
    core = _core(0, payload)
    # 消息上屏后空白被压缩/换行变化，仍应识别为我们的消息
    assert core._fast_ai_pre_arrived(
        SNAP(4, "[长消息分片 1/2] 消息尚未发送完 请勿回复。")) is False


if __name__ == "__main__":
    sys.exit(1 if __import__("pytest").main([__file__, "-v"]) else 0)
