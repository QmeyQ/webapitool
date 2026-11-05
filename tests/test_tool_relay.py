"""中转层核心测试：ToolSchemaConformer / ToolCallStreamParser / ToolRoundBuilder。

模拟 CodeArts / Trae IDE 的工具调用与交互模式，验证：
- tools 定义归一化（OpenAI / Anthropic / Gemini 风格）
- 工具名与参数名对齐、类型强转、必填补占位
- 网页 AI 输出的 5 种工具调用格式解析（围栏 JSON / 裸 JSON / XML / DSML / tool_call 标签）
- 流式分片、think 剥离、标记不泄漏
"""
import json
import sys
import types
from pathlib import Path

import pytest

if "DrissionPage" not in sys.modules:
    _dp = types.ModuleType("DrissionPage")
    _dp.ChromiumPage = object
    sys.modules["DrissionPage"] = _dp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_core as bc

Conformer = bc.ToolSchemaConformer
Parser = bc.ToolCallStreamParser


def ide_tools():
    """模拟 IDE 声明的工具清单（Trae/CodeArts 常见命名风格）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from workspace",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string"},
                        "lineStart": {"type": "integer"},
                    },
                    "required": ["filePath"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "str_replace",
                "description": "Replace text in file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "oldStr": {"type": "string"},
                        "newStr": {"type": "string"},
                    },
                    "required": ["oldStr", "newStr"],
                },
            },
        },
    ]


def args_of(call):
    raw = call["function"]["arguments"]
    return json.loads(raw) if isinstance(raw, str) else raw


# ============ normalize_tools ============

def test_normalize_openai():
    tools = Conformer.normalize_tools(ide_tools())
    assert [t["function"]["name"] for t in tools] == ["read_file", "str_replace"]
    assert all(t["type"] == "function" for t in tools)


def test_normalize_anthropic():
    tools = Conformer.normalize_tools([
        {"name": "search", "description": "d",
         "input_schema": {"type": "object",
                          "properties": {"q": {"type": "string"}},
                          "required": ["q"]}},
    ])
    assert tools[0]["function"]["name"] == "search"
    assert tools[0]["function"]["parameters"]["properties"]["q"]["type"] == "string"


def test_normalize_gemini_upper_types():
    tools = Conformer.normalize_tools([
        {"name": "calc",
         "parameters": {"type": "OBJECT",
                        "properties": {"A": {"type": "INTEGER"}},
                        "required": ["A"]}},
    ])
    p = tools[0]["function"]["parameters"]
    assert p["type"] == "object"
    assert p["properties"]["A"]["type"] == "integer"


def test_normalize_json_string_params():
    tools = Conformer.normalize_tools([
        {"type": "function", "function": {
            "name": "echo",
            "parameters": json.dumps({
                "type": "object",
                "properties": {"t": {"type": "string"}},
                "required": ["t"]})}},
    ])
    assert tools[0]["function"]["parameters"]["properties"]["t"]["type"] == "string"


def test_normalize_invalid_and_empty():
    assert Conformer.normalize_tools(None) == []
    assert Conformer.normalize_tools([]) == []
    assert Conformer.normalize_tools([{"x": 1}, "bad", 42]) == []


def test_normalize_fills_defaults():
    p = Conformer.normalize_tools([{"name": "noop"}])[0]["function"]["parameters"]
    assert p["type"] == "object"
    assert p["properties"] == {}
    assert p["required"] == []


# ============ match_tool ============

def test_match_exact():
    t = Conformer.normalize_tools(ide_tools())
    assert Conformer.match_tool("read_file", t)["name"] == "read_file"


def test_match_normalized_variants():
    t = Conformer.normalize_tools(ide_tools())
    for v in ("readFile", "read-file", "READ_FILE", "readfile"):
        fn = Conformer.match_tool(v, t)
        assert fn is not None and fn["name"] == "read_file", v


def test_match_unique_contains():
    t = Conformer.normalize_tools(ide_tools())
    assert Conformer.match_tool("read", t)["name"] == "read_file"


def test_match_ambiguous_returns_none():
    t = Conformer.normalize_tools([{"name": "file_read"}, {"name": "file_write"}])
    assert Conformer.match_tool("file", t) is None


def test_match_unknown_returns_none():
    t = Conformer.normalize_tools(ide_tools())
    assert Conformer.match_tool("zzz_unknown", t) is None
    assert Conformer.match_tool(None, t) is None
    assert Conformer.match_tool("read_file", []) is None


# ============ conform_args ============

def test_conform_rename_and_coerce():
    t = Conformer.normalize_tools(ide_tools())
    fn = Conformer.match_tool("read_file", t)
    out = Conformer.conform_args(fn, {"file_path": "a.py", "line_start": "12"})
    assert out["filePath"] == "a.py"
    assert out["lineStart"] == 12
    assert isinstance(out["lineStart"], int)


def test_conform_drop_extra_and_fill_required():
    t = Conformer.normalize_tools(ide_tools())
    fn = Conformer.match_tool("str_replace", t)
    out = Conformer.conform_args(fn, {"old_str": "a", "junk": 1})
    assert "junk" not in out
    assert out["oldStr"] == "a"
    assert "newStr" in out


def test_conform_bool_array_object():
    t = Conformer.normalize_tools([{
        "type": "function", "function": {
            "name": "flags",
            "parameters": {"type": "object", "properties": {
                "on": {"type": "boolean"},
                "items": {"type": "array"},
                "obj": {"type": "object"}},
                "required": []}}}])
    fn = Conformer.match_tool("flags", t)
    out = Conformer.conform_args(fn, {"on": "true", "items": "a", "obj": '{"k":1}'})
    assert out["on"] is True
    assert out["items"] == ["a"]
    assert out["obj"] == {"k": 1}


def test_conform_defaults_by_type():
    t = Conformer.normalize_tools([{
        "type": "function", "function": {
            "name": "mix",
            "parameters": {"type": "object", "properties": {
                "n": {"type": "integer"},
                "b": {"type": "boolean"},
                "s": {"type": "string"}},
                "required": ["n", "b", "s"]}}}])
    fn = Conformer.match_tool("mix", t)
    out = Conformer.conform_args(fn, {})
    assert out["n"] == 0 and out["b"] is False and out["s"] == ""


def test_conform_scalar_into_single_required():
    t = Conformer.normalize_tools([{
        "type": "function", "function": {
            "name": "single",
            "parameters": {"type": "object",
                           "properties": {"path": {"type": "string"}},
                           "required": ["path"]}}}])
    fn = Conformer.match_tool("single", t)
    assert Conformer.conform_args(fn, "a/b.py") == {"path": "a/b.py"}


def test_conform_none_schema_passthrough():
    a = {"a": 1}
    assert Conformer.conform_args(None, a) is a


# ============ JSON 解析 ============

def test_parse_single():
    p = Parser()
    calls = p._parse_json_multi(
        '{"name": "read_file", "arguments": {"filePath": "a.py"}}')
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert calls[0]["type"] == "function"
    assert calls[0]["id"].startswith("call_")
    assert args_of(calls[0]) == {"filePath": "a.py"}


def test_parse_array():
    p = Parser()
    raw = '[{"name":"a","arguments":{}},{"name":"b","arguments":{}}]'
    assert [c["function"]["name"] for c in p._parse_json_multi(raw)] == ["a", "b"]


def test_parse_wrapped_tool_calls():
    p = Parser()
    raw = '{"tool_calls": [{"name": "read_file", "arguments": {"filePath": "x"}}]}'
    calls = p._parse_json_multi(raw)
    assert len(calls) == 1 and calls[0]["function"]["name"] == "read_file"


def test_parse_repair_single_quote_trailing_comma():
    p = Parser()
    calls = p._parse_json_multi(
        "{'name': 'read_file', 'arguments': {'filePath': 'a'},}")
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert args_of(calls[0])["filePath"] == "a"


def test_parse_bare_keys():
    p = Parser()
    calls = p._parse_json_multi('{name: "read_file", arguments: {filePath: "a"}}')
    assert len(calls) == 1 and calls[0]["function"]["name"] == "read_file"


def test_parse_invalid_returns_empty():
    p = Parser()
    assert p._parse_json_multi("not json at all") == []
    assert p._parse_json_multi("") == []


def test_parse_string_arguments():
    p = Parser()
    raw = '{"name": "read_file", "arguments": "{\\"filePath\\": \\"a.py\\"}"}'
    calls = p._parse_json_multi(raw)
    assert len(calls) == 1
    assert args_of(calls[0])["filePath"] == "a.py"


def test_parse_function_wrapper_shape():
    p = Parser()
    raw = '{"type":"function","function":{"name":"read_file","arguments":"{\\"filePath\\":\\"z\\"}"}}'
    calls = p._parse_json_multi(raw)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert args_of(calls[0])["filePath"] == "z"


# ============ schema 对齐（带 IDE 工具清单） ============

def test_conform_call_renames_tool_and_args():
    p = Parser(known_tools=ide_tools())
    calls = p._parse_json_multi(
        '{"name": "readFile", "arguments": {"file_path": "a.py"}}')
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert args_of(calls[0])["filePath"] == "a.py"


def test_conform_call_drops_unknown_tool():
    p = Parser(known_tools=ide_tools())
    assert p._parse_json_multi('{"name": "ghost_xyz", "arguments": {}}') == []


def test_no_known_tools_passthrough():
    p = Parser()
    calls = p._parse_json_multi('{"name": "anything", "arguments": {"a": 1}}')
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "anything"


def test_infer_tool_from_pure_args():
    p = Parser(known_tools=ide_tools())
    calls = p._parse_json_multi('{"filePath": "a.py"}')
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"


# ============ 流式 feed / flush ============

def feed_all(p, text, chunk=5):
    plain, calls = [], []
    for i in range(0, len(text), chunk):
        pl, cl = p.feed(text[i:i + chunk])
        plain.extend(pl)
        calls.extend(cl)
    pl, cl = p.flush()
    plain.extend(pl)
    calls.extend(cl)
    return plain, calls


def test_feed_fenced_tool_call():
    p = Parser()
    text = '```tool_call\n{"name": "read_file", "arguments": {"filePath": "a"}}\n```'
    plain, calls = feed_all(p, text)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert "```" not in "".join(plain)


def test_feed_bare_json():
    p = Parser()
    plain, calls = feed_all(p, '{"name": "read_file", "arguments": {"filePath": "b"}}')
    assert len(calls) == 1
    assert "read_file" not in "".join(plain)


def test_feed_chunked_never_loses_call():
    p = Parser()
    text = '```tool_call\n{"name": "read_file", "arguments": {"filePath": "c"}}\n```'
    plain, calls = feed_all(p, text, chunk=3)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert args_of(calls[0])["filePath"] == "c"
    assert "```" not in "".join(plain)


def test_feed_think_stripped():
    p = Parser()
    plain, calls = feed_all(
        p, "<think>secret reasoning</think>可见正文")
    joined = "".join(plain)
    assert "secret" not in joined
    assert "可见正文" in joined
    assert calls == []


def test_feed_think_split_across_chunks():
    p = Parser()
    plain, calls = feed_all(
        p, "<thi", chunk=4)  # placeholder replaced below
    # 逐小片喂入完整文本
    p2 = Parser()
    text = "<think>hidden</think>answer"
    plain2, _ = feed_all(p2, text, chunk=2)
    joined = "".join(plain2)
    assert "hidden" not in joined
    assert "answer" in joined


def test_feed_plain_text_passthrough():
    p = Parser()
    plain, calls = feed_all(p, "就是一段普通回答，没有工具调用。")
    assert calls == []
    assert "普通回答" in "".join(plain)


def test_feed_json_example_not_stolen():
    p = Parser(known_tools=ide_tools())
    plain, calls = feed_all(p, '```json\n{"hello": "world"}\n```')
    assert calls == []
    assert "hello" in "".join(plain)


def test_feed_multiple_calls_array():
    p = Parser()
    raw = ('```tool_call\n[{"name": "a", "arguments": {}},'
           '{"name": "b", "arguments": {}}]\n```')
    plain, calls = feed_all(p, raw)
    assert [c["function"]["name"] for c in calls] == ["a", "b"]


def test_feed_xml_style_tool_call():
    p = Parser()
    raw = ('<tool_call>{"name": "read_file", '
           '"arguments": {"filePath": "x.py"}}</tool_call>')
    plain, calls = feed_all(p, raw)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"


def test_flush_unclosed_tool_call_best_effort():
    p = Parser()
    plain, calls = p.feed('```tool_call\n{"name": "read_file", "arguments": {"filePath": "y"}}')
    pl, cl = p.flush()
    all_calls = calls + cl
    assert len(all_calls) == 1
    assert all_calls[0]["function"]["name"] == "read_file"


# ============ 真实场景：华为 CodeArts 工具调用 ============

BS = chr(92)


def test_windows_path_bad_unicode_escape():
    """C:\\universalweb 的 \\u 是合法 unicode 前缀，修复器必须先修非法 \\uXXXX。"""
    p = Parser()
    raw = ('<tool_call>\n{"name": "glob", "arguments": '
           '{"pattern": ".codeartsdoer/specs/**/*.md", '
           '"path": "C:' + BS + 'Users' + BS + 'mey' + BS + 'Desktop'
           + BS + 'universalweb-api-main"}}\n</tool_call>')
    plain, calls = feed_all(p, raw)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "glob"
    assert args_of(calls[0])["path"] == "C:\\Users\\mey\\Desktop\\universalweb-api-main"
    assert "universalweb" not in "".join(plain)


def test_windows_path_u_prefix_in_bare_json():
    p = Parser()
    raw = ('{"name": "bash", "arguments": '
           '{"command": "dir C:' + BS + 'Users", '
           '"workdir": "C:' + BS + 'utils' + BS + 'update"}}')
    plain, calls = feed_all(p, raw)
    assert len(calls) == 1
    assert args_of(calls[0])["workdir"] == "C:\\utils\\update"


def test_valid_unicode_escape_not_broken():
    """真正的 unicode 转义 \\u4e2d 不能被误伤。"""
    p = Parser()
    raw = '{"name": "echo", "arguments": {"text": "\\u4e2d\\u6587"}}'
    plain, calls = feed_all(p, raw)
    assert len(calls) == 1
    assert args_of(calls[0])["text"] == "中文"


def test_ide_tool_names_case_sensitive_known_tools():
    """华为 CodeArts 工具名风格：deleteFile / todowrite 大小写混杂，须能对齐。"""
    tools = [{"type": "function", "function": {
        "name": "deleteFile",
        "parameters": {"type": "object",
                       "properties": {"file_paths": {"type": "array"}},
                       "required": ["file_paths"]}}}][0]
    tools = [tools, {"type": "function", "function": {
        "name": "todowrite",
        "parameters": {"type": "object",
                       "properties": {"todos": {"type": "array"}},
                       "required": ["todos"]}}}]
    p = Parser(known_tools=tools)
    calls = p._parse_json_multi(
        '{"name": "DeleteFile", "arguments": {"filePaths": ["a.txt"]}}')
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "deleteFile"
    assert "file_paths" in args_of(calls[0])


# ============ DSML ============

def test_dsml_fullwidth_full_form():
    p = Parser()
    raw = ('<｜｜DSML｜｜calls>'
           '<｜｜DSML｜｜invoke name="read_file">'
           '<｜｜DSML｜｜parameter name="filePath" string="true">a.py'
           '</｜｜DSML｜｜parameter>'
           '</｜｜DSML｜｜invoke>'
           '</｜｜DSML｜｜calls>')
    plain, calls = feed_all(p, raw)
    assert len(calls) == 1, f"calls={calls} plain={plain}"
    assert calls[0]["function"]["name"] == "read_file"
    assert args_of(calls[0])["filePath"] == "a.py"
    assert "DSML" not in "".join(plain)


def test_dsml_halfwidth_ascii():
    p = Parser()
    raw = ('<||DSML||calls><||DSML||invoke name="read_file">'
           '<||DSML||parameter name="filePath" string="true">b.py'
           '</||DSML||parameter></||DSML||invoke></||DSML||calls>')
    plain, calls = feed_all(p, raw)
    assert len(calls) == 1
    assert args_of(calls[0])["filePath"] == "b.py"


def test_dsml_unclosed_flush_best_effort():
    p = Parser()
    plain, calls = p.feed(
        '<｜｜DSML｜｜invoke name="read_file">'
        '<｜｜DSML｜｜parameter name="filePath" string="true">a.py')
    pl, cl = p.flush()
    all_calls = calls + cl
    assert len(all_calls) >= 1
    assert all_calls[0]["function"]["name"] == "read_file"
    assert args_of(all_calls[0])["filePath"] == "a.py"


# ============ ToolRoundBuilder ============

def test_round_builder_with_tools():
    text = bc.ToolRoundBuilder.build(
        [{"role": "user", "content": "hi"}], tools=ide_tools())
    assert isinstance(text, str)
    assert "read_file" in text
    assert "filePath" in text
    # 工具表以完整 JSON 原样附上（含 description/parameters）
    assert "description" in text
    assert "parameters" in text


def test_round_builder_without_tools():
    text = bc.ToolRoundBuilder.build([{"role": "user", "content": "hi"}])
    assert isinstance(text, str)
    assert "hi" in text


def test_round_builder_tool_result_roundtrip():
    msgs = [
        {"role": "user", "content": "读文件"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": '{"filePath": "a.py"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
    ]
    text = bc.ToolRoundBuilder.build(msgs, tools=ide_tools())
    assert "file body" in text
    assert "read_file" in text
