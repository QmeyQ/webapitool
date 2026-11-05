"""API 集成测试：模拟 CodeArts / Trae IDE 调用 /v1/chat/completions。

覆盖 IDE 交互契约：
- 流式 SSE：首块 role:assistant、content 增量、tool_calls chunk、finish_reason、[DONE]
- 非流式：tool_calls 聚合进 message（不丢失）
- 连续对话：assistant tool_calls + tool 结果消息透传
- /v1/models、/health 等 IDE 启动期探测接口
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_mod
import browser_core as bc
from browser_core import SSEFormatter


def ide_tools():
    """模拟 IDE 声明的工具清单。"""
    return [
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object",
                           "properties": {"filePath": {"type": "string"}},
                           "required": ["filePath"]}}},
        {"type": "function", "function": {
            "name": "run_command",
            "description": "Run a shell command",
            "parameters": {"type": "object",
                           "properties": {"command": {"type": "string"}},
                           "required": ["command"]}}},
    ]


class StubBrowser:
    """替身浏览器：按预设脚本吐 SSE chunk，记录收到的请求。"""

    def __init__(self, script=None):
        self.script = script or []
        self.calls = []

    def set_stop_checker(self, fn):
        pass

    def health_check(self):
        return {"connected": True, "tab_url": "about:blank",
                "tab_title": "t", "port": 9222, "error": None}

    def close(self):
        pass

    def execute_workflow(self, messages, stream=True, tools=None,
                         tool_choice=None, model=None, think_hint=False):
        self.calls.append({"messages": messages, "tools": tools,
                           "tool_choice": tool_choice, "stream": stream,
                           "model": model, "think_hint": think_hint})
        for chunk in self.script:
            if callable(chunk):
                yield chunk(self.calls[-1])
            else:
                yield chunk

    def execute_workflow_locked(self, messages, stream=True, tools=None,
                                tool_choice=None, model=None,
                                think_hint=False):
        # Stub：与 execute_workflow 同行为（锁由真实实现持有）
        yield from self.execute_workflow(messages, stream=stream,
                                         tools=tools,
                                         tool_choice=tool_choice,
                                         model=model,
                                         think_hint=think_hint)


def text_script(content="你好，我是网页 AI。"):
    return [
        SSEFormatter.pack_role_chunk(),
        SSEFormatter.pack_chunk(content),
        SSEFormatter.pack_finish(finish_reason="stop"),
    ]


def tool_script(tool_name="read_file", args={"filePath": "a.py"}):
    return [
        SSEFormatter.pack_role_chunk(),
        SSEFormatter.pack_chunk("我来读取文件。"),
        SSEFormatter.pack_tool_calls_chunk([
            {"id": "call_test001", "type": "function",
             "function": {"name": tool_name,
                          "arguments": json.dumps(args, ensure_ascii=False)}}]),
        SSEFormatter.pack_finish(finish_reason="tool_calls"),
    ]


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    stub = StubBrowser()
    monkeypatch.setattr(main_mod, "get_browser", lambda auto_connect=True: stub)
    with TestClient(main_mod.app) as c:
        yield c, stub


def parse_sse(raw_text):
    events = []
    for block in raw_text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            payload = block[6:].strip()
            if payload == "[DONE]":
                events.append("[DONE]")
            elif payload:
                events.append(json.loads(payload))
    return events


# ============ 基础接口（IDE 启动期探测） ============

def test_models_endpoint(client):
    c, _ = client
    r = c.get("/v1/models")
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert any(m["id"] == "web-browser" for m in data["data"])


def test_root_endpoint(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert r.json()["service"]


def test_health_endpoint(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "healthy"


# ============ 流式纯文本对话 ============

def test_stream_plain_text(client):
    c, stub = client
    stub.script = text_script("你好，我是网页 AI。")
    r = c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = parse_sse(r.text)
    assert events[-1] == "[DONE]"
    deltas = [e for e in events if e != "[DONE]"]
    # 首块携带 role:assistant
    first = deltas[0]["choices"][0]["delta"]
    assert first.get("role") == "assistant"
    # content 增量拼出全文
    content = "".join(e["choices"][0]["delta"].get("content", "")
                      for e in deltas if e.get("choices"))
    assert "网页 AI" in content
    # 结束块 finish_reason=stop
    finish = [e for e in deltas if e["choices"][0].get("finish_reason")]
    assert finish and finish[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_forwards_tools_and_tool_choice(client):
    c, stub = client
    stub.script = text_script()
    c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "读文件"}],
        "tools": ide_tools(), "tool_choice": "auto"})
    assert len(stub.calls) == 1
    assert stub.calls[0]["tools"] == ide_tools()
    assert stub.calls[0]["tool_choice"] == "auto"


# ============ 流式工具调用（IDE 核心交互） ============

def test_stream_tool_calls(client):
    c, stub = client
    stub.script = tool_script()
    r = c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "读 a.py"}],
        "tools": ide_tools()})
    events = parse_sse(r.text)
    tc_events = [e for e in events if e != "[DONE]"
                 and e.get("choices")
                 and e["choices"][0]["delta"].get("tool_calls")]
    assert len(tc_events) >= 1
    tc = tc_events[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["id"] == "call_test001"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "read_file"
    assert isinstance(tc["function"]["arguments"], str)
    assert json.loads(tc["function"]["arguments"]) == {"filePath": "a.py"}
    finish = [e for e in events if e != "[DONE]"
              and e.get("choices") and e["choices"][0].get("finish_reason")]
    assert finish[-1]["choices"][0]["finish_reason"] == "tool_calls"


# ============ 非流式对话 ============

def test_non_stream_plain_text(client):
    c, stub = client
    stub.script = text_script("非流式回答。")
    r = c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "非流式回答。"
    assert data["choices"][0]["finish_reason"] == "stop"


def test_non_stream_tool_calls_not_lost(client):
    """IDE 非流式模式：tool_calls 必须聚合进 message，不能静默丢失。"""
    c, stub = client
    stub.script = tool_script()
    r = c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": False,
        "messages": [{"role": "user", "content": "读 a.py"}],
        "tools": ide_tools()})
    assert r.status_code == 200
    msg = r.json()["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {
        "filePath": "a.py"}
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"


# ============ 连续对话（工具结果回传） ============

def test_multi_turn_tool_result_roundtrip(client):
    """IDE 第二轮请求带 assistant tool_calls + tool 结果，必须完整透传给网页端。"""
    c, stub = client
    stub.script = text_script("文件内容是……")
    messages = [
        {"role": "user", "content": "读 a.py"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_x1", "type": "function",
             "function": {"name": "read_file",
                          "arguments": '{"filePath": "a.py"}'}}]},
        {"role": "tool", "tool_call_id": "call_x1", "content": "print('hi')"},
    ]
    r = c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": False,
        "messages": messages, "tools": ide_tools()})
    assert r.status_code == 200
    forwarded = stub.calls[0]["messages"]
    assert len(forwarded) == 3
    assert forwarded[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert forwarded[2]["role"] == "tool"
    assert forwarded[2]["content"] == "print('hi')"


def test_new_conversation_fresh_messages(client):
    """新对话：首轮请求只有 system + user，原样透传。"""
    c, stub = client
    stub.script = text_script()
    messages = [
        {"role": "system", "content": "你是编程助手"},
        {"role": "user", "content": "新任务"},
    ]
    c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True, "messages": messages})
    assert stub.calls[0]["messages"] == messages


# ============ IDE 附加字段兼容 ============

def test_extra_openai_fields_accepted(client):
    """IDE 常见附加字段（temperature/max_tokens/top_p 等）不能导致 422。"""
    c, stub = client
    stub.script = text_script()
    r = c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.2, "max_tokens": 4096,
        "top_p": 0.9, "frequency_penalty": 0.0,
        "presence_penalty": 0.0, "user": "ide-user"})
    assert r.status_code == 200
    assert "[DONE]" in r.text


def test_missing_messages_rejected(client):
    c, _ = client
    r = c.post("/v1/chat/completions", json={"model": "gpt-4o"})
    assert r.status_code == 422


# ============ OpenClaw 接入（stream_options.include_usage） ============

def test_stream_include_usage_chunk(client):
    """OpenClaw 依赖 include_usage 终块：choices=[] 且带 usage，位于 [DONE] 之前。"""
    c, stub = client
    stub.script = text_script("统计测试。")
    r = c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "stream_options": {"include_usage": True}})
    events = parse_sse(r.text)
    assert events[-1] == "[DONE]"
    # 独立 usage 终块：choices=[] 且数值非零（finish 块自带的 0 usage 不算）
    usage_events = [e for e in events if e != "[DONE]"
                    and e.get("usage") and not e.get("choices")
                    and e["usage"]["total_tokens"] > 0]
    assert len(usage_events) == 1
    assert usage_events[0]["usage"]["prompt_tokens"] >= 1
    assert usage_events[0]["usage"]["completion_tokens"] >= 1
    assert usage_events[0]["usage"]["total_tokens"] == (
        usage_events[0]["usage"]["prompt_tokens"]
        + usage_events[0]["usage"]["completion_tokens"])


def test_stream_no_usage_by_default(client):
    c, stub = client
    stub.script = text_script()
    r = c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    events = parse_sse(r.text)
    # 默认无独立 usage 终块（finish 块内嵌全 0 usage 除外）
    assert all(e == "[DONE]" or e.get("choices") for e in events)


def test_non_stream_usage_estimated(client):
    c, stub = client
    stub.script = text_script("12345678")
    r = c.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    usage = r.json()["usage"]
    assert usage["prompt_tokens"] >= 1
    assert usage["completion_tokens"] == 2
    assert usage["total_tokens"] == usage["prompt_tokens"] + 2


# ============ SSE 心跳（OpenClaw 空闲超时） ============

def test_keepalive_heartbeat(client, monkeypatch):
    """下游长时间无输出时应周期性发 ': keep-alive' 注释行，客户端不误判断线。"""
    import asyncio
    import main as main_mod

    async def slow_stream(request, body, ctx):
        await asyncio.sleep(0.3)
        yield "data: {\"choices\":[{\"delta\":{\"content\":\"late\"}}]}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(main_mod, "_stream_with_lifecycle", slow_stream)
    body = main_mod.ChatRequest(model="gpt-4o", stream=True,
                                messages=[{"role": "user", "content": "hi"}])
    gen = main_mod._keepalive_stream(None, body, None, heartbeat_interval=0.05)
    async def _run():
        return [c async for c in gen]
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(_run())
    finally:
        loop.close()
    heartbeats = [c for c in out if c.startswith(": keep-alive")]
    assert len(heartbeats) >= 2
    assert any("late" in c for c in out)
    assert out[-1] == "data: [DONE]\n\n"


# ============ 模型路由 + 站点管线分离 ============

def test_match_site_domain():
    f = bc.match_site_domain
    domains = ["chat.deepseek.com", "chat.z.ai", "www.kimi.com"]
    assert f("chat.z.ai", domains) == "chat.z.ai"
    assert f("deepseek", domains) == "chat.deepseek.com"
    assert f("z.ai", domains) == "chat.z.ai"
    assert f("zai", domains) == "chat.z.ai"
    assert f("kimi", domains) == "www.kimi.com"
    assert f("web-browser", domains) is None
    assert f("glm-4.6", domains) is None
    assert f("", domains) is None
    assert f(None, domains) is None
    assert f("DEEPSEEK", domains) == "chat.deepseek.com"


def test_match_site_domain_compound_ids():
    """复合 model id（带 "/"）按段匹配站点。"""
    f = bc.match_site_domain
    domains = ["poe.com", "chat.deepseek.com"]
    assert f("poe/poe.com", domains) == "poe.com"
    assert f("poe.com/Assistant", domains) == "poe.com"
    assert f("poe/GPT-5-Mini", domains) == "poe.com"
    assert f("gpt-5-mini", domains) is None


def test_extract_model_slug():
    f = bc.extract_model_slug
    assert f("poe.com/Assistant", "poe.com") == "Assistant"
    assert f("poe/GPT-5-Mini", "poe.com") == "GPT-5-Mini"
    assert f("poe.com", "poe.com") == ""
    assert f("poe/poe.com", "poe.com") == ""
    assert f("poe", "poe.com") == ""
    assert f("web-browser", "poe.com") == ""
    assert f("", "poe.com") == ""
    assert f(None, "poe.com") == ""


def test_get_site_model_candidates():
    cfg = {"entry_url": "https://poe.com/Assistant",
           "FALLBACK_MODELS": ["GPT-5-Mini", "Assistant"]}
    assert bc.get_site_model_candidates(cfg) == ["Assistant", "GPT-5-Mini"]
    # 无路径段的主模型 → 站点级 ""
    assert bc.get_site_model_candidates(
        {"entry_url": "https://chat.z.ai"}) == [""]
    assert bc.get_site_model_candidates(None) == [""]


class TestCreditStateStoreModelGranularity:
    """积分记录 (domain, port, model) 粒度 + 旧格式兼容。"""

    @pytest.fixture(autouse=True)
    def _tmp_file(self, tmp_path, monkeypatch):
        self.store_file = tmp_path / "credit_state.json"
        monkeypatch.setattr(bc.CreditStateStore, "_FILE", self.store_file)
        self.store_file.write_text("{}", encoding="utf-8")

    def test_legacy_site_level_record_blocks_all_models(self):
        s = bc.CreditStateStore
        # 旧格式站点级记录（poe.com:9222，无 model 段）
        self.store_file.write_text(json.dumps({
            "poe.com:9222": {"marked_at": 1, "exhausted_until": 9999999999},
        }, ensure_ascii=False), encoding="utf-8")
        assert s.is_exhausted("poe.com", 9222) is True
        assert s.is_exhausted("poe.com", 9222, "Assistant") is True
        assert s.is_exhausted("poe.com", 9222, "GPT-5-Mini") is True
        assert s.is_exhausted("poe.com", 9333) is False

    def test_per_model_records_are_independent(self):
        s = bc.CreditStateStore
        s.mark_exhausted("poe.com", 9222, "Assistant")
        assert s.is_exhausted("poe.com", 9222, "Assistant") is True
        # 其他模型不受影响
        assert s.is_exhausted("poe.com", 9222, "GPT-5-Mini") is False
        # 模型级优先于站点级
        assert s.exhausted_until("poe.com", 9222, "Assistant") > 0
        # 模型级 key 独立存在（不覆盖站点级）
        data = json.loads(self.store_file.read_text(encoding="utf-8"))
        assert "poe.com:9222:Assistant" in data

    def test_clear_all_models_for_port(self):
        s = bc.CreditStateStore
        s.mark_exhausted("poe.com", 9222, "Assistant")
        s.mark_exhausted("poe.com", 9222, "GPT-5-Mini")
        assert s.clear("poe.com", 9222) is True  # model=None 清全部
        data = json.loads(self.store_file.read_text(encoding="utf-8"))
        assert not [k for k in data if k.startswith("poe.com:9222")]
        assert s.is_exhausted("poe.com", 9222, "Assistant") is False

    def test_clear_single_model(self):
        s = bc.CreditStateStore
        s.mark_exhausted("poe.com", 9222, "Assistant")
        s.mark_exhausted("poe.com", 9222, "GPT-5-Mini")
        assert s.clear("poe.com", 9222, "Assistant") is True
        assert s.is_exhausted("poe.com", 9222, "Assistant") is False
        assert s.is_exhausted("poe.com", 9222, "GPT-5-Mini") is True

    def test_records_for_port(self):
        s = bc.CreditStateStore
        s.mark_exhausted("poe.com", 9222, "Assistant")
        s.mark_exhausted("poe.com", 9333, "GPT-5-Mini")
        recs = s.records_for_port(9222)
        assert len(recs) == 1
        assert recs[0]["domain"] == "poe.com"
        assert recs[0]["model"] == "Assistant"
        assert recs[0]["exhausted_until"] > 0

    def test_expired_record_auto_cleared(self, monkeypatch):
        s = bc.CreditStateStore
        # 写一条已过期的模型级记录
        self.store_file.write_text(json.dumps({
            "poe.com:9222:Assistant": {
                "marked_at": 1, "exhausted_until": 1},
        }, ensure_ascii=False), encoding="utf-8")
        assert s.is_exhausted("poe.com", 9222, "Assistant") is False
        data = json.loads(self.store_file.read_text(encoding="utf-8"))
        assert "poe.com:9222:Assistant" not in data


def test_request_manager_per_site_locks():
    """不同站点可并行持锁；同站点互斥。"""
    import threading
    import time as _time
    from request_manager import RequestManager

    rm = RequestManager()
    ctx_a = rm.create_request()
    ctx_b = rm.create_request()
    ctx_c = rm.create_request()

    ok = []
    t1 = threading.Thread(target=lambda: ok.append(
        rm._get_lock("chat.z.ai").acquire(timeout=0.1)))
    t1.start()
    t1.join()

    t2 = threading.Thread(target=lambda: ok.append(
        rm._get_lock("chat.deepseek.com").acquire(timeout=0.1)))
    t2.start()
    t2.join()

    t3 = threading.Thread(target=lambda: ok.append(
        rm._get_lock("chat.z.ai").acquire(timeout=0.2)))
    t3.start()
    t3.join()

    # A(z.ai) 成功、B(deepseek) 独立成功、C(z.ai) 被互斥挡住
    assert ok == [True, True, False]
    assert rm.is_locked() is True
    rm._get_lock("chat.z.ai").release()
    rm._get_lock("chat.deepseek.com").release()
    assert rm.is_locked() is False


def test_acquire_release_site_key_roundtrip():
    """acquire 写入 ctx.site_key，release 按站点释放。"""
    import asyncio
    from request_manager import RequestManager

    rm = RequestManager()
    ctx = rm.create_request()
    acquired = asyncio.new_event_loop().run_until_complete(
        rm.acquire(ctx, timeout=1.0, site_key="chat.z.ai"))
    assert acquired
    assert ctx.site_key == "chat.z.ai"
    rm.release(ctx, success=True)
    assert rm.is_locked() is False
    assert rm._current_by_site.get("chat.z.ai") is None
