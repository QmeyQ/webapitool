"""分片发送 _chunked_direct_send 单元测试（fake executor，不碰真实网页）"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_core import BrowserCore, WorkflowError, ElementNotFoundError


class FakeTab:
    """假会话：每条发出去的消息是一个 turn，拒收面板长在**它自己那个 turn** 里。

    真实 chatgpt 就是这样的（`[data-turn-key]` 逐轮渲染），中转必须靠"这个
    turn 里有没有本片的分片头"来判断面板是不是在说我们这一片——上一版 fake
    只看最后一次填充长度，等于把真实判定条件整个绕过了。
    """

    REJECT = "你提交的消息过长，请编辑后重新发送"

    def __init__(self, reject_over=8000, stale_reject=False):
        self.url = 'https://chatgpt.com/c/test'
        self.reject_over = reject_over
        self.turns = []
        self.landed = []          # 真的进入会话的消息
        if stale_reject:          # 上一轮遗留的拒收面板，一直挂在会话里
            self.turns.append("（上一轮的一条消息）" + self.REJECT)

    def send(self, text: str) -> bool:
        if len(text) > self.reject_over:
            self.turns.append(text + "\n" + self.REJECT)
            return False
        self.turns.append(text)
        self.landed.append(text)
        return True

    def run_js(self, js, *a):
        if '你提交的消息过长' not in js and 'data-turn-key' not in js:
            return False
        import json as _json
        import re as _re
        m = _re.search(r'const probe = ("(?:[^"\\]|\\.)*")', js)
        # json.dumps 默认把中文转成 \uXXXX，探针要还原成真字符再比
        try:
            probe = _json.loads(m.group(1)) if m else ''
        except Exception:
            probe = m.group(1) if m else ''
        for idx in range(len(self.turns) - 1,
                         max(-1, len(self.turns) - 4), -1):
            tx = self.turns[idx]
            if self.REJECT in tx:
                return {"k": f"turn-{idx}", "ours": 1 if probe in tx else 0}
        return {"k": "", "ours": 0}


class FakeExecutor:
    def __init__(self, fail_click_on=None, fail_verify_on=None,
                 reject_over=8000, stale_reject=False, no_land_on=None):
        self.tab = FakeTab(reject_over=reject_over,
                           stale_reject=stale_reject)
        self.filled = []      # 实际写入输入框的（含被拒收的）
        self.clicked = []
        self.fail_click_on = fail_click_on or set()
        self.fail_verify_on = fail_verify_on or set()
        # 点了、没报错，但消息根本没进会话（站点生成期间把内容当草稿丢掉）
        self.no_land_on = no_land_on or set()
        self.core = None    # 由 _run_chunks/_driver 挂上 BrowserCore，便于断言断点进度
        self.settled = 0
        self.sendable_waits = 0   # "没按钮 + 上一条没回复"那道闸门的次数
        self.cancel = False
        self._fill_submitted = False
        # 闸门两个输入的真实默认：chatgpt 生成期间没有可见可点的发送按钮，
        # 而上一条通常很快就回（回了一点就算"有回复"）
        self.btn_visible = False
        self.prev_replied = True

    @property
    def sent(self):
        return self.tab.landed

    def _should_stop_checker(self):
        return self.cancel

    def _execute_fill(self, selector, text, target_key=None, optional=False,
                      msg_selector=""):
        self.filled.append(text)

    def _capture_send_state(self, input_sel, msg_sel):
        # msg_count/turn_count 跟着"真正进入会话的片数"走：报错后要靠它分清
        # "点动了只是没回复"还是"根本没点动"，前者绝不重投
        n = len(self.tab.landed)
        return {'i': 0, 'm': 0, 'msg_count': n, 'turn_count': n}

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
        if len(self.clicked) in self.no_land_on:
            yield
            return
        # 超过站点限长的消息：点击后被判拒收，不进会话
        self.tab.send(self.filled[-1])
        yield

    def _verify_and_recover_send(self, executor, selectors, pre):
        if len(self.clicked) in self.fail_verify_on:
            raise WorkflowError('发送未生效')
        yield

    def _wait_generation_settled(self, executor, timeout=90):
        self.settled += 1

    def _get_latest_message_snapshot(self, selector):
        return {"groups_count": len(self.tab.landed), "text_len": 10,
                "think_len": 0, "is_generating": False, "text": "x"}


def _bind_gate(auto, ex):
    """把闸门两个输入接成假信号：按钮可见性 / 上一条有没有回复。"""
    auto._chunk_btn_visible = lambda executor, send_sel: ex.btn_visible
    auto._chunk_prev_replied = lambda executor, msg_sel: ex.prev_replied
    auto._wait_generation_settled = (
        lambda executor, timeout=90: setattr(
            executor, 'settled', executor.settled + 1))
    auto._wait_until_sendable = (
        lambda executor, sel, **kw: setattr(
            ex, 'sendable_waits', ex.sendable_waits + 1) or True)
    return auto


def _run_chunks(prompt, limit=12000, ex=None, chunk_limit=None, fk="", **ex_kw):
    ex = ex or FakeExecutor(**ex_kw)
    auto = BrowserCore.__new__(BrowserCore)
    ex.core = auto          # 便于断言断点进度
    auto._should_stop_checker = lambda: ex.cancel
    _bind_gate(auto, ex)
    gen = BrowserCore._chunked_direct_send(
        auto, ex, {'prompt': prompt, 'session_fk': fk,
                   'chunk_send_gap': 0},
        {'input_box': '#in', 'send_btn': '#send'},
        chunk_limit=chunk_limit)
    list(gen)
    return ex


def _head(text: str) -> str:
    """分片头 = 第一个空行之前的部分（正文从那儿才开始）。"""
    return text.split("\n\n", 1)[0]


def test_two_chunks_order_and_headings():
    prompt = 'X' * 15000
    ex = _run_chunks(prompt, reject_over=99999)
    assert len(ex.filled) == 2
    h1, h2 = _head(ex.filled[0]), _head(ex.filled[1])
    assert h1.startswith('[长消息分片 1/2]') and h2.startswith('[长消息分片 2/2]')
    # 三条等待规则（2026-11-07 用户逐条确认）：没点动才等；点动了无论有没有
    # 回复都不等。正常投递一片都没点失败，所以一次都不该等。
    assert ex.sendable_waits == 0, (
        f"两片都点动了就不该等任何输出，实际等了 {ex.sendable_waits} 次")


def test_every_chunk_head_says_earlier_chunks_exist():
    """真实事故（2026-11-07 用户实测）：分片之间模型不知道前文还有片，
    每一片都被当成独立的新问题，回的全是"对上一片"的反应，几万字从来没有
    在它眼里合成一条消息。所以每一片的头部都要写明共几片、前面已到几片。"""
    prompt = 'X' * 36000                     # 3 片
    ex = _run_chunks(prompt, reject_over=99999)
    assert len(ex.filled) == 3
    h1, h2, h3 = (_head(t) for t in ex.filled)
    assert '切成 3 片' in h1 and '请勿回答' in h1, h1
    assert '前 1 片已在本对话里发出' in h2 and '3 条消息合起来' in h2, h2
    assert '前 2 片已在本对话里发出' in h3 or '已全部发送完毕' in h3, h3
    # 最后一片必须把它自己说成"整条消息的收尾"，并明确要求统一处理
    assert '已全部发送完毕' in h3 and '统一处理' in h3, h3


def test_send_failure_reclicks_without_waiting():
    """没点动（发送按钮点不到、消息没进会话）：立刻重投同一片——
    上一条是有回复的，按规则一次都不等。"""
    ex = FakeExecutor(reject_over=99999)
    ex.fail_click_on = {1}                   # 只有第 1 次点击失败
    _run_chunks('X' * 15000, ex=ex, fk='fk1')
    assert ex.sendable_waits == 0, (
        f"上一条有回复就不该等，实际等了 {ex.sendable_waits} 次")
    assert len(ex.filled) == 3 and len(ex.sent) == 2, (
        f"第 1 片重投一次后两片都该送达：filled={len(ex.filled)} "
        f"sent={len(ex.sent)}")
    assert _head(ex.filled[1]).startswith('[长消息分片 1/2]'), \
        "重投的是第 1 片，不是跳过它发第 2 片"
    assert _head(ex.filled[2]).startswith('[长消息分片 2/2]'), \
        "重投成功后要继续发最后一片"


def test_silent_and_missing_reclicks_without_waiting():
    """点下去了但复核发现没进会话：重投同一片；上一条是有回复的 → 不等。"""
    ex = FakeExecutor(reject_over=99999, no_land_on={1}, fail_verify_on={1})
    _run_chunks('X' * 15000, ex=ex, fk='fk1')
    assert ex.sendable_waits == 0, f"实际等了 {ex.sendable_waits} 次"
    assert len(ex.sent) == 2 and len(ex.clicked) == 3, (
        f"clicked={len(ex.clicked)} sent={len(ex.sent)}")


def test_blind_gate_waits_only_on_the_second_hit():
    """用户定的那张表（上一条有没有回复 × 本条看不看得到按钮）：

    | 上一条有回复 | 本条点得动 | 行为 |
    |---|---|---|
    | 是 | 是 | 不等 |
    | 是 | 否 | 不等 |
    | 否 | 是 | 不等 |
    | 否 | 否 | 再点一次；第二次还这样才等 |
    """
    prompt = 'X' * 36000                       # 3 片

    def _run(btn, replied):
        ex = FakeExecutor(reject_over=99999)
        ex.btn_visible, ex.prev_replied = btn, replied
        _run_chunks(prompt, ex=ex)
        return ex

    assert _run(True, True).sendable_waits == 0
    assert _run(False, True).sendable_waits == 0, "上一条有回复 → 永不等待"
    assert _run(True, False).sendable_waits == 0, "本条点得动 → 永不等待"
    blind = _run(False, False)
    assert blind.sendable_waits == 1, (
        f"没按钮+没回复要连着两次才等：第 1 片先照点，第 2 片才等一次，"
        f"实际等了 {blind.sendable_waits} 次")
    assert len(blind.sent) == 3, "等完之后三片仍要全部送达"

    # 第 1 片没有"上一条"，不参与计数；四片里只有第 3 片凑满"连续两次"，
    # 等过一次即清零，第 4 片重新从"第一次成立"算起——绝不是每轮都等。
    four = FakeExecutor(reject_over=99999)
    four.btn_visible, four.prev_replied = False, False
    _run_chunks('X' * 48000, ex=four)          # 4 片
    assert four.sendable_waits == 1, (
        f"第 1 片不计数，四片只该等那一次，实际 {four.sendable_waits} 次")
    assert len(four.sent) == 4, "四片仍要全部送达"


def test_give_up_after_two_redeliveries():
    """重投有上限：站点一直不接收时不能无限点下去（每次重投都是真实消息）"""
    ex = FakeExecutor(reject_over=99999)
    ex.fail_click_on = {1, 2, 3}
    with pytest.raises(WorkflowError) as ei:
        _run_chunks('X' * 15000, ex=ex, fk='fk1')
    assert '分片 1/2' in str(ei.value)
    assert len(ex.clicked) == 3, f"实际点击 {len(ex.clicked)} 次"
    assert ex.core._chunk_progress.get('fk1', {}).get('sent', 0) == 0


def test_landed_but_unverified_is_never_redelivered():
    """点动了、消息确实进了会话，只是站点没有回复：不等输出、也不重投——
    重投会让会话里出现两份同样的分片（用户实测过）。"""
    ex = FakeExecutor(reject_over=99999)
    ex.fail_verify_on = {1}                  # 第 1 片点击成功但校验报错
    _run_chunks('X' * 15000, ex=ex, fk='fk1')
    assert len(ex.clicked) == 2, f"两片各点一次，实际 {len(ex.clicked)} 次"
    assert len(ex.sent) == 2
    assert ex.sendable_waits == 0, "点动了就不该等"


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


def test_stale_reject_panel_is_not_attributed_to_our_chunk():
    """真实事故（2026-11-06 18:43）：会话里那块"消息过长"面板属于上一轮，
    代码把它当成我们这一片被拒 → 降半到底后原地重发十几次、346s 才失败，
    白烧十几条真实消息。现在要求面板所在 turn 里含本片分片头才算拒收。"""
    prompt = '段落内容 ' * 4000          # 20000 字符 → 2 片
    ex = FakeExecutor(reject_over=99999, stale_reject=True)
    _run_chunks(prompt, ex=ex, limit=12000)
    assert len(ex.clicked) == len(ex.filled) == 2, (
        f"每片只该发一次：填充 {len(ex.filled)} 片却点击 {len(ex.clicked)} 次"
        "（事故里是十几次）")
    assert len(set(t[:40] for t in ex.filled)) == len(ex.filled), \
        "不该出现同一片反复重填"


def test_reject_reshalving_is_capped():
    """降半重切最多 2 次：每次重切都要真的发消息，无上限就是无底洞"""
    prompt = '段落内容 ' * 4000
    ex = FakeExecutor(reject_over=0)     # 任何长度都被真拒（ours=1）
    with pytest.raises(WorkflowError) as ei:
        _run_chunks(prompt, ex=ex, limit=12000)
    msg = str(ei.value)
    assert '降半重切' in msg or '降到下限' in msg
    assert len(ex.clicked) <= 6, f"实际点击 {len(ex.clicked)} 次"


def test_chunk_limit_param_overrides_default():
    """站点级 CHUNK_CHARS 生效：15000 文本按 8000 切 2 片"""
    prompt = 'X' * 15000
    ex = _run_chunks(prompt, chunk_limit=8000, reject_over=99999)
    assert len(ex.sent) == 2
    import re
    bodies = [re.sub(r'^\[长消息分片 \d+/\d+\][^\n]*\n\n', '', t)
              for t in ex.sent]
    assert ''.join(bodies) == prompt


def test_click_failure_does_not_advance_progress():
    """第 1 片始终点不出去（重投到上限）：报错要带片号，断点更不能前进——
    否则重试会从"以为已送达"的下一片开始，前面那片内容就永久丢了。"""
    ex = FakeExecutor(reject_over=99999)
    ex.fail_click_on = {1, 2, 3}
    with pytest.raises(WorkflowError) as ei:
        _run_chunks('X' * 15000, ex=ex, fk='fk1')
    assert '分片 1/2' in str(ei.value)
    assert len(ex.sent) == 0
    assert (ex.core._chunk_progress.get('fk1') or {}).get('sent', 0) == 0



def test_verify_failure_does_not_advance_progress():
    """校验判定"发送未生效"且复核确认消息没进会话（=没点动）：
    重投到上限后必须报错，且断点停在 0——重试时这片要重发，
    否则内容就永久缺一段。"""
    ex = FakeExecutor(reject_over=99999, no_land_on={1, 2, 3},
                      fail_verify_on={1, 2, 3})
    with pytest.raises(WorkflowError) as ei:
        _run_chunks('X' * 15000, ex=ex, fk='fk1')
    assert '发送未生效' in str(ei.value)
    assert (ex.core._chunk_progress.get('fk1') or {}).get('sent', 0) == 0
    assert len(ex.sent) == 0
    assert len(ex.clicked) == 3, f"点了 {len(ex.clicked)} 次仍没点动就该收手"


def test_client_cancel_does_not_stop_chunk_delivery():
    """客户端点"停止"也要把分片发完。

    "停止"的本意是别再等回答，不是别再送内容：半途撂下会让会话里只有前 k 片，
    后面几万字根本没送达。真正该中止的是异常（填充失败/页面不可用/
    站点拒收到下限/校验不过），那些路径各自抛出。"""
    prompt = 'X' * 30000                       # 3 片
    ex2 = FakeExecutor(reject_over=99999)
    auto = BrowserCore.__new__(BrowserCore)
    auto._should_stop_checker = lambda: True   # 从一开始就是"已取消"
    auto._wait_generation_settled = lambda executor, timeout=90: None
    auto._wait_until_sendable = lambda executor, sel, **kw: True
    list(BrowserCore._chunked_direct_send(
        auto, ex2, {'prompt': prompt, 'chunk_send_gap': 0},
        {'input_box': '#in', 'send_btn': '#s'}))
    assert len(ex2.filled) == 3, "取消状态下 3 片仍要全部送达"
    assert len(ex2.clicked) == 3


def test_delivery_never_suspends_on_a_yield():
    """投递期间不得挂在中途 yield 上：外层一 close() 就会把发送拦腰掐断。

    所以站点提示一类的"外发消息"必须先攒着，整份送完再交出去。"""
    class YieldsWarning(FakeExecutor):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.warned = 0

        def execute_step(self, action=None, selector=None, target_key=None,
                         value=None, optional=False, context=None):
            for x in super().execute_step(action=action, selector=selector,
                                          target_key=target_key, value=value,
                                          optional=optional, context=context):
                yield x
            if self.warned == 0:
                self.warned += 1
                yield "站点提示：本次发送较慢"

    ex = YieldsWarning(reject_over=99999)
    auto = BrowserCore.__new__(BrowserCore)
    auto._should_stop_checker = lambda: False
    auto._wait_generation_settled = lambda executor, timeout=90: None
    gen = BrowserCore._chunked_direct_send(
        auto, ex, {'prompt': 'X' * 15000, 'chunk_send_gap': 0},
        {'input_box': '#in', 'send_btn': '#send'})
    first = next(gen)                       # 第一条外发消息
    assert first == "站点提示：本次发送较慢"
    assert len(ex.filled) == 2, (
        f"外发第一条消息时只填了 {len(ex.filled)}/2 片——"
        "投递不该中途让出控制权")
    assert list(gen) == []


def _driver(ex, store):
    auto = BrowserCore.__new__(BrowserCore)
    auto._should_stop_checker = lambda: ex.cancel
    auto._chunk_progress = store
    return _bind_gate(auto, ex)


def _send_resume(ex, prompt, store, fk='fk1', limit=12000):
    list(BrowserCore._chunked_direct_send(
        _driver(ex, store), ex,
        {'prompt': prompt, 'session_fk': fk, 'chunk_send_gap': 0},
        {'input_box': '#in', 'send_btn': '#send'}, chunk_limit=limit))


def test_chunk_cap_rejects_absurd_size():
    """超过单请求最多片数直接拒单：不开永远发不完的 96 片任务，
    也不再去试必然被站点拒绝的 1MB 附件"""
    from browser_core import TooLongError
    prompt = 'Y' * (12000 * 40)      # 约 40 片 > 默认上限 30
    ex = FakeExecutor(reject_over=99999)
    with pytest.raises(TooLongError):
        _send_resume(ex, prompt, {})
    assert ex.filled == [], '拒单必须发生在任何填充之前'


def test_resume_after_abnormal_stop_skips_delivered_chunks():
    """中途异常终止（第 3 片发不出去）：断点记下真正送达的 2 片，
    下一轮从第 3 片继续，不重发 1、2 片"""
    prompt = 'Z' * 60000             # 5 片
    store = {}
    ex1 = FakeExecutor(reject_over=99999)
    ex1.fail_click_on = {3, 4, 5}    # 第 3 片连重投都送不出去
    with pytest.raises(WorkflowError):
        _send_resume(ex1, prompt, store)
    assert len(ex1.sent) == 2, '前两片真的送达了'
    assert store['fk1']['sent'] == 2, '断点只该记真正送达的 2 片'

    ex2 = FakeExecutor(reject_over=99999)
    _send_resume(ex2, prompt, store)
    assert len(ex2.filled) == 3, '第二轮只应补发剩下 3 片'
    assert '[长消息分片 3/5]' in ex2.filled[0][:40], ex2.filled[0][:40]
    assert 'fk1' not in store, '全部送达后断点必须清除'


def test_stale_progress_does_not_skip():
    """片数变了（换了一份 prompt）就不能按旧断点跳过，否则内容凭空缺失"""
    import time as _t
    prompt = 'W' * 60000
    store = {'fk1': {'sent': 3, 'n': 9, 'ts': _t.time()}}
    ex = FakeExecutor(reject_over=99999)
    _send_resume(ex, prompt, store)
    assert len(ex.filled) == 5, 'n 不匹配时必须从第 1 片发满'


def test_progress_from_other_conversation_is_ignored():
    """断点属于另一个对话时必须从头发：否则前几片根本不在当前会话里"""
    prompt = 'V' * 60000
    store = {'fk1': {'sent': 2, 'n': 5, 'ts': __import__('time').time(),
                     'conv': 'https://chatgpt.com/c/OLD'}}
    ex = FakeExecutor(reject_over=99999)
    _send_resume(ex, prompt, store)
    assert len(ex.filled) == 5, '对话不一致，5 片都要发'
    assert '[长消息分片 1/5]' in ex.filled[0][:40]


def test_invalidate_clears_all_delivered_state():
    """会话重开：压缩基准、附件登记、分片进度都要作废（新对话里它们都不存在）"""
    auto = BrowserCore.__new__(BrowserCore)
    auto._fp_last_payload = {'fk1': 'x'}
    auto._fp_attach = {'fk1': {'names': ['a.txt']}}
    auto._chunk_progress = {'fk1': {'sent': 2, 'n': 5, 'ts': 0}}
    auto._invalidate_session_baseline('fk1', '测试')
    assert 'fk1' not in auto._fp_last_payload
    assert 'fk1' not in auto._fp_attach
    assert 'fk1' not in auto._chunk_progress


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
