"""
data_models.py - 数据模型定义

【修改声明】本文件修改自 Universal Web-to-API（AGPL-3.0）上游项目。
修改清单见 CHANGES.md，最后修改日期 2026-11-05。本程序整体仍以 AGPL-3.0 发布。
"""

from typing import TypedDict, List, Optional, Literal, Dict, Any


# ================= 动作类型 =================

ActionType = Literal[
    "FILL_INPUT",
    "CLICK",
    "STREAM_WAIT",
    "STREAM_OUTPUT",
    "KEY_PRESS",
    "WAIT",
    "JS_EXEC",
    "UPLOAD_FILE",   # 新增：把上下文中的文件上传到页面
]


# ================= 选择器字段 =================

REQUIRED_SELECTOR_KEYS = [
    "input_box",
    "send_btn",
    "result_container",
]

OPTIONAL_SELECTOR_KEYS = [
    "new_chat_btn",
    "message_wrapper",
    "generating_indicator",
    "upload_btn",       # 新增：点击触发上传对话框的按钮
    "file_input",       # 新增：<input type="file">，DrissionPage 通过 CDP 注入
]

ALL_SELECTOR_KEYS = REQUIRED_SELECTOR_KEYS + OPTIONAL_SELECTOR_KEYS


# ================= 工作流 =================

class WorkflowStep(TypedDict):
    action: ActionType
    target: str
    optional: bool
    value: Optional[str]


# ================= 流式监控 =================

class StreamConfig(TypedDict, total=False):
    silence_threshold: float
    initial_wait: float
    enable_wrapper_search: bool
    rerender_wait: float
    content_shrink_tolerance: int


# ================= 上传配置（新增） =================

class UploadConfig(TypedDict, total=False):
    enabled: bool                      # 是否启用超长提示词文件回退
    max_prompt_length: int             # 直接输入的最大字符数，超过走文件
    max_file_size: int                 # 单文件上限（字节），超过走分块
    chunk_size: int                    # 分块大小（字符）
    upload_dir: str                    # 落盘目录
    file_ext: str                      # 文件扩展名（txt / md）
    short_prompt_template: str         # 单文件模式的短提示（支持 {filename}）
    chunked_prompt_template: str       # 分块模式的短提示（支持 {count}）
    wait_after_upload: float           # 上传后等待时间（秒）
    max_upload_files: int              # 最多上传文件数


# ================= 站点配置 =================

class SiteConfig(TypedDict, total=False):
    # 选择器值支持 str（单 UI）或 list[str]（多 UI 变体，任一命中即用，
    # 运行时由 config_engine.normalize_selector_group 归一化为 CSS 逗号分组）
    selectors: Dict[str, Optional[str | List[str]]]
    workflow: List[WorkflowStep]
    stealth: bool
    stream_config: StreamConfig
    upload_config: UploadConfig       # 新增


# ================= 验证器 =================

class SelectorValidationResult(TypedDict):
    key: str
    selector: Optional[str]
    valid: bool
    reason: Optional[str]
    repaired: Optional[str]


class AIAnalysisResult(TypedDict, total=False):
    input_box: Optional[str]
    send_btn: Optional[str]
    result_container: Optional[str]
    new_chat_btn: Optional[str]
    message_wrapper: Optional[str]
    generating_indicator: Optional[str]
    upload_btn: Optional[str]         # 新增
    file_input: Optional[str]         # 新增


class HealthCheckResult(TypedDict):
    status: Literal["healthy", "unhealthy"]
    connected: bool
    port: int
    tab_url: Optional[str]
    tab_title: Optional[str]
    error: Optional[str]


class PageStatusResult(TypedDict):
    ready: bool
    reason: Optional[str]


class ChatMessage(TypedDict):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatCompletionRequest(TypedDict):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool]
    temperature: Optional[float]
    max_tokens: Optional[int]
    tools: Optional[List[Dict[str, Any]]]        # 新增
    tool_choice: Optional[Any]                    # 新增


# ================= SSE 响应 =================

class DeltaContent(TypedDict, total=False):
    content: str


class StreamChoice(TypedDict):
    index: int
    delta: DeltaContent
    finish_reason: Optional[Literal["stop", None]]


class StreamResponse(TypedDict):
    id: str
    object: str
    created: int
    model: str
    choices: List[StreamChoice]


class MessageContent(TypedDict):
    role: Literal["assistant"]
    content: str


class NonStreamChoice(TypedDict):
    index: int
    message: MessageContent
    finish_reason: Literal["stop"]


class UsageInfo(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class NonStreamResponse(TypedDict):
    id: str
    object: str
    created: int
    model: str
    choices: List[NonStreamChoice]
    usage: UsageInfo


class ErrorDetail(TypedDict):
    message: str
    type: str
    code: str


class ErrorResponse(TypedDict):
    error: ErrorDetail


class ModelInfo(TypedDict):
    id: str
    object: str
    created: int
    owned_by: str


class ModelsResponse(TypedDict):
    object: str
    data: List[ModelInfo]


# ================= 工具函数 =================

def validate_workflow_step(step: Dict[str, Any]) -> bool:
    required_keys = {"action", "target", "optional"}
    return all(key in step for key in required_keys)


def validate_upload_config(config: Dict[str, Any]) -> bool:
    """验证 upload_config 结构"""
    if not isinstance(config, dict):
        return False
    if "enabled" in config and not isinstance(config["enabled"], bool):
        return False
    for k in ("max_prompt_length", "max_file_size", "chunk_size", "max_upload_files"):
        if k in config:
            v = config[k]
            if not isinstance(v, int) or v <= 0:
                return False
    if "wait_after_upload" in config:
        v = config["wait_after_upload"]
        if not isinstance(v, (int, float)) or v < 0:
            return False
    if "upload_dir" in config and not isinstance(config["upload_dir"], str):
        return False
    if "file_ext" in config and not isinstance(config["file_ext"], str):
        return False
    for k in ("short_prompt_template", "chunked_prompt_template"):
        if k in config and not isinstance(config[k], str):
            return False
    return True


def validate_site_config(config: Dict[str, Any]) -> bool:
    if "selectors" not in config or "workflow" not in config:
        return False
    if not isinstance(config["selectors"], dict):
        return False
    if not isinstance(config["workflow"], list):
        return False
    for step in config["workflow"]:
        if not validate_workflow_step(step):
            return False

    if "stream_config" in config:
        sc = config["stream_config"]
        if not isinstance(sc, dict):
            return False
        if "silence_threshold" in sc:
            v = sc["silence_threshold"]
            if not isinstance(v, (int, float)) or v <= 0:
                return False
        if "initial_wait" in sc:
            v = sc["initial_wait"]
            if not isinstance(v, (int, float)) or v <= 0:
                return False
        if "enable_wrapper_search" in sc:
            if not isinstance(sc["enable_wrapper_search"], bool):
                return False

    # 新增：upload_config 校验
    if "upload_config" in config:
        if not validate_upload_config(config["upload_config"]):
            return False

    return True


def get_default_stream_config() -> StreamConfig:
    return {
        "silence_threshold": 2.5,
        "initial_wait": 30.0,
        "enable_wrapper_search": True,
        "rerender_wait": 0.5,
        "content_shrink_tolerance": 3,
    }


def get_default_upload_config() -> UploadConfig:
    """新增：上传回退默认值"""
    return {
        "enabled": True,
        "max_prompt_length": 2000,
        "max_file_size": 5 * 1024 * 1024,
        "chunk_size": 50000,
        "upload_dir": "uploads",
        "file_ext": "txt",
        "short_prompt_template": "请阅读我上传的文件 {filename}，并按照文件中的要求完成任务。",
        "chunked_prompt_template": "我上传了 {count} 个分片文件（文件名带 _partNNofNN 序号），请按文件名顺序依次阅读、合并理解后完成任务。",
        "wait_after_upload": 1.5,
        "max_upload_files": 10,
    }


def merge_stream_config(
    site_config: Optional[StreamConfig],
    defaults: Optional[StreamConfig] = None
) -> StreamConfig:
    if defaults is None:
        defaults = get_default_stream_config()
    if site_config is None:
        return defaults.copy()
    result = defaults.copy()
    result.update(site_config)
    return result


def merge_upload_config(
    site_config: Optional[UploadConfig],
    defaults: Optional[UploadConfig] = None
) -> UploadConfig:
    """新增：合并上传配置"""
    if defaults is None:
        defaults = get_default_upload_config()
    if site_config is None:
        return defaults.copy()
    result = defaults.copy()
    result.update(site_config)
    return result

    # ================= 工具调用（新增） =================

class FunctionCall(TypedDict):
    name: str
    arguments: str  # JSON 字符串


class ToolCall(TypedDict):
    id: str
    type: Literal["function"]
    function: FunctionCall


class ToolDefinition(TypedDict, total=False):
    type: Literal["function"]
    function: Dict[str, Any]  # {name, description, parameters}