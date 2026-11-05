"""
main.py - FastAPI 主入口

【修改声明】本文件修改自 Universal Web-to-API（AGPL-3.0）上游项目。
修改清单见 CHANGES.md，最后修改日期 2026-11-05。本程序整体仍以 AGPL-3.0 发布。

职责：
- HTTP 服务启动
- 路由定义
- 中间件配置
- 集成 RequestManager 进行并发控制
"""

import json
import os
import time
import logging
import asyncio
import faulthandler
import subprocess

_fault_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "faulthandler.log")
_fault_log = open(_fault_log_path, "w", encoding="utf-8", buffering=1)
faulthandler.enable(_fault_log)
import sys
import threading
import traceback


def _dump_uncaught(exc_type, exc, tb):
    try:
        _fault_log.write("\n[UNCAUGHT] "
                         + "".join(traceback.format_exception(exc_type, exc, tb)))
        _fault_log.flush()
    except Exception:
        pass


sys.excepthook = _dump_uncaught


def _thread_hook(a):
    _dump_uncaught(a.exc_type, a.exc_value, a.exc_traceback)


threading.excepthook = _thread_hook

import uuid
import queue
import threading
import re
from pathlib import Path
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
from collections import deque
import threading

from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse, FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from browser_core import (get_browser, BrowserConnectionError,
                          BrowserConstants, CreditStateStore,
                          CREDIT_EXHAUSTED_RE, get_site_model_candidates,
                          TooLongError)
from config_engine import config_engine, ConfigConstants
from request_manager import request_manager, RequestContext, RequestStatus, watch_client_disconnect
from data_models import (
    ChatCompletionRequest, 
    HealthCheckResult,
    ModelsResponse,
    ModelInfo,
    SiteConfig
)

# Windows asyncio 事件循环修复（避免 ConnectionResetError 刷屏）
import sys
if sys.platform == 'win32':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# 显式忽略客户端断开的 ConnectionResetError
def _ignore_connection_reset(loop, context):
    exc = context.get('exception')
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)

try:
    _loop = asyncio.get_event_loop()
    _loop.set_exception_handler(_ignore_connection_reset)
except Exception:
    pass


# ================= 环境变量配置 =================

class AppConfig:
    """应用配置"""
    HOST = os.getenv("APP_HOST", "127.0.0.1")
    PORT = int(os.getenv("APP_PORT", "8199"))
    DEBUG = os.getenv("APP_DEBUG", "false").lower() == "true"
    
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")
    CORS_ENABLED = os.getenv("CORS_ENABLED", "true").lower() == "true"
    
    AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() == "true"
    AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
    
    BROWSER_PORT = int(os.getenv("BROWSER_PORT", "9222"))
    
    DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() == "true"
    DASHBOARD_FILE = os.getenv("DASHBOARD_FILE", "dashboard.html")
    DASHBOARD_JS_FILE = os.getenv("DASHBOARD_JS_FILE", "dashboard.js")
    
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# ================= 日志配置 =================

logging.basicConfig(
    level=getattr(logging, AppConfig.LOG_LEVEL),
    format='[%(asctime)s] %(levelname)s [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 文件日志：无论进程以何种方式启动（WMI/计划任务/无控制台），
# 日志始终落盘到 server.log，避免排查问题时丢失输出
from logging.handlers import RotatingFileHandler
_file_handler = RotatingFileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log"),
    maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] %(levelname)s [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'))
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger('main')


# ================= 日志收集器 =================

class LogCollector:
    """收集日志用于前端展示"""
    
    def __init__(self, max_logs=500):
        self.logs = deque(maxlen=max_logs)
        self.lock = threading.Lock()
    
    def add(self, level: str, message: str):
        with self.lock:
            self.logs.append({
                "timestamp": time.time(),
                "level": level,
                "message": message
            })
    
    def get_recent(self, since: float = 0):
        with self.lock:
            recent = [log for log in self.logs if log["timestamp"] > since]
            return list(recent)
    
    def clear(self):
        with self.lock:
            self.logs.clear()


log_collector = LogCollector()

# 最近一次成功路由到的站点（web-browser 等未命中模型跟随它，
# 保证 OpenClaw 标题生成等后续调用与主调用落在同一站点）
_last_used_site = None
_last_site_lock = threading.Lock()


class WebLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            log_collector.add(record.levelname, msg)
        except Exception:
            self.handleError(record)


web_handler = WebLogHandler()
web_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(web_handler)


# ================= Lifespan =================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("Universal Web-to-API 服务启动中...")
    logger.info(f"监听地址: http://{AppConfig.HOST}:{AppConfig.PORT}")
    logger.info(f"调试模式: {AppConfig.DEBUG}")
    logger.info(f"认证: {'启用' if AppConfig.AUTH_ENABLED else '禁用'}")
    logger.info(f"浏览器端口: {AppConfig.BROWSER_PORT}")
    logger.info("=" * 60)
    
    # 浏览器健康检查（延迟初始化，不阻塞启动）
    try:
        browser = get_browser(auto_connect=False)
        health = browser.health_check()
        if health["connected"]:
            logger.info(f"✅ 浏览器已连接: {health['tab_url']}")
        else:
            logger.warning(f"⚠️ 浏览器未连接: {health.get('error', '未知')}")
    except Exception as e:
        logger.warning(f"⚠️ 浏览器检查跳过: {e}")
    
    logger.info("")
    logger.info("🚀 服务已就绪！")
    if AppConfig.DASHBOARD_ENABLED:
        logger.info(f"   Dashboard: http://{AppConfig.HOST}:{AppConfig.PORT}/dashboard")
    logger.info(f"   健康检查: http://{AppConfig.HOST}:{AppConfig.PORT}/health")
    logger.info("")

    yield

    logger.info("服务正在关闭...")
    
    try:
        browser = get_browser(auto_connect=False)
        browser.close()
    except Exception as e:
        logger.debug(f"关闭浏览器: {e}")
    
    logger.info("👋 服务已停止")


# ================= FastAPI 应用 =================

app = FastAPI(
    title="Universal Web-to-API",
    description="将任意 AI Web 界面转换为 OpenAI 兼容 API",
    version="2.0.0",
    docs_url="/docs" if AppConfig.DEBUG else None,
    redoc_url="/redoc" if AppConfig.DEBUG else None,
    lifespan=lifespan
)


# ================= CORS =================

if AppConfig.CORS_ENABLED:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=AppConfig.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ================= 请求模型 =================

class ChatRequest(BaseModel):
    # extra="allow"：保留所有未知字段（CodeArts/IDE 各家客户端附加参数
    # 不再被静默丢弃，兼容性优先）
    model_config = ConfigDict(extra="allow")
    model: str = Field(default="gpt-3.5-turbo")
    messages: list = Field(...)
    stream: Optional[bool] = Field(default=True)
    temperature: Optional[float] = Field(default=0.7, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    tools: Optional[list] = Field(default=None)
    tool_choice: Optional[object] = Field(default=None)
    stream_options: Optional[dict] = Field(default=None)
    # 深度思考参数（各家风格全收：OpenAI / Qwen / Anthropic / DeepSeek）
    reasoning_effort: Optional[str] = Field(default=None)
    enable_thinking: Optional[bool] = Field(default=None)
    thinking: Optional[dict] = Field(default=None)
    reasoning: Optional[object] = Field(default=None)

    def wants_deep_think(self) -> bool:
        """思考意图判定：任一思考参数表达"要思考"即为 True"""
        try:
            if self.enable_thinking:
                return True
            effort = (self.reasoning_effort or "").strip().lower()
            if effort and effort not in ("none", "minimal", "off"):
                return True
            if self.thinking:
                if isinstance(self.thinking, dict):
                    return self.thinking.get("type", "enabled") != "disabled"
                return True
            r = self.reasoning
            if r is True:
                return True
            if isinstance(r, str) and r.strip().lower() not in (
                    "", "none", "off"):
                return True
            if isinstance(r, dict):
                return r.get("enabled", True) is not False
        except Exception:
            pass
        return False
class ConfigUpdateRequest(BaseModel):
    config: Dict[str, Any] = Field(...)


# ================= 认证 =================

async def verify_auth(authorization: Optional[str] = Header(None)) -> bool:
    if not AppConfig.AUTH_ENABLED:
        return True
    
    if not AppConfig.AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="服务配置错误")
    
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="未提供认证令牌",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    token = authorization.replace("Bearer ", "").strip()
    
    if token != AppConfig.AUTH_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="认证令牌无效",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    return True


# ================= 核心 API =================

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatRequest,
    authenticated: bool = Depends(verify_auth)
):
    """
    OpenAI 兼容的聊天补全接口
    
    集成 RequestManager 进行：
    1. 请求排队（FIFO）
    2. 并发控制
    3. 客户端断开检测
    4. 优雅取消
    """

    # ========== 0. 积分耗尽预检（HTTP 层快速失败） ==========
    # 仅当所有账号在全部配置站点（积分耗尽后可切换的故障转移链）
    # 上都耗尽时才返回 429，避免客户端（如 OpenClaw）对空流反复
    # 重试直到超时；只要还有一个可用组合就交给 worker 正常流转
    try:
        import browser_core as _bc
        _all_sites = list(config_engine.sites.keys())
        if _all_sites:
            _browser = get_browser(auto_connect=False)
            _insts = [
                i for i in (_browser.list_instances() or [])
                if int(i.get("port") or 0)
                and (_debug_port_alive(int(i.get("port")))
                     or i.get("user_data_dir"))]
            _any_avail = False
            _until = 0.0
            for s in _all_sites:
                _cands = _bc.get_site_model_candidates(
                    config_engine.sites.get(s) or {})
                for i in _insts:
                    p = int(i.get("port") or 0)
                    if any(not _bc.CreditStateStore.is_exhausted(s, p, m)
                           for m in _cands):
                        _any_avail = True
                        break
                    for m in _cands:
                        _until = max(
                            _until,
                            _bc.CreditStateStore.exhausted_until(s, p, m))
                if _any_avail:
                    break
            if _insts and not _any_avail:
                _reset = time.strftime(
                    '%m-%d %H:%M', time.localtime(_until)) if _until \
                    else '每日重置'
                raise HTTPException(
                    status_code=429,
                    detail=f"所有账号在全部配置站点的积分均已耗尽，"
                           f"预计 {_reset} 重置后可用")
    except HTTPException:
        raise
    except Exception:
        pass  # 预检失败不影响正常流程

    # 创建请求上下文
    ctx = request_manager.create_request()
    logger.info(f"请求 [{ctx.request_id}] 开始...")
    
    if body.stream:
        return StreamingResponse(
            _keepalive_stream(request, body, ctx),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    else:
        return await _non_stream_with_lifecycle(request, body, ctx)


async def _keepalive_stream(
    request: Request,
    body: ChatRequest,
    ctx: RequestContext,
    heartbeat_interval: float = 15.0,
):
    """网页 AI 首字节等待期（DeepSeek 深度思考可达 90s+）发 SSE 心跳注释行。

    OpenAI 兼容客户端忽略 ': ' 注释行；无心跳时 OpenClaw 等客户端会在
    空闲超时后断开并报"无法验证 AI"。生产者任务 + 队列隔离，
    心跳超时不会取消下游生成器。"""
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    async def _produce():
        try:
            async for chunk in _usage_aware_stream(request, body, ctx):
                await queue.put(chunk)
        except Exception as exc:
            await queue.put(exc)
        finally:
            await queue.put(_DONE)

    task = asyncio.create_task(_produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(),
                                              timeout=heartbeat_interval)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        if not task.done():
            task.cancel()


async def _usage_aware_stream(
    request: Request,
    body: ChatRequest,
    ctx: RequestContext
):
    """OpenClaw 等客户端依赖 stream_options.include_usage 终块（choices 为空、含 usage）。"""
    from browser_core import SSEFormatter

    include_usage = bool((body.stream_options or {}).get("include_usage"))
    prompt_chars = sum(len(str(m.get("content") or "")) for m in body.messages)
    completion_chars = 0
    async for chunk in _stream_with_lifecycle(request, body, ctx):
        if (include_usage and isinstance(chunk, str)
                and '"finish_reason"' in chunk and "data: [DONE]" in chunk):
            finish_part = chunk.split("data: [DONE]")[0].rstrip()
            usage = {
                "prompt_tokens": max(prompt_chars // 4, 1),
                "completion_tokens": max(completion_chars // 4, 1),
            }
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            yield finish_part + "\n\n" + SSEFormatter.pack_usage_chunk(usage) + "data: [DONE]\n\n"
            continue
        if isinstance(chunk, str) and '"content"' in chunk:
            try:
                for event in chunk.split("\n\n"):
                    event = event.strip()
                    if event.startswith("data: ") and "[DONE]" not in event:
                        data = json.loads(event[6:])
                        delta = (data.get("choices") or [{}])[0].get("delta", {})
                        if delta.get("content"):
                            completion_chars += len(delta["content"])
            except (json.JSONDecodeError, IndexError, AttributeError):
                pass
        yield chunk


async def _stream_with_lifecycle(
    request: Request,
    body: ChatRequest,
    ctx: RequestContext
):
    """
    流式响应 + 完整生命周期管理
    
    关键修复：
    1. 生成器在独立线程执行（不阻塞事件循环）
    2. 主协程定期检查连接状态
    3. 通过队列传递数据
    """
    
    disconnect_task = None
    worker_thread = None
    acquired = False
    
    try:
        # ========== 0. 模型 → 站点路由 ==========
        def _resolve_site_key(model: str) -> str:
            try:
                import browser_core as _bc
                key = _bc.match_site_domain(
                    model, list(config_engine.sites.keys()))
                if key:
                    global _last_used_site
                    with _last_site_lock:
                        _last_used_site = key
                    return key
                # 未命中 model id（如 web-browser）：映射到最近一次
                # 成功使用的站点。同一客户端（如 OpenClaw 标题生成）
                # 的后续调用紧随主调用，跟最近站点最可靠；活动 tab
                # 可能停在别的站点，会造成标题/正文内容错位。
                with _last_site_lock:
                    if _last_used_site:
                        return _last_used_site
                # 默认站点（browser_config.json DEFAULT_SITE，
                # 未配置时取 sites.json 第一个键）
                default_site = BrowserConstants.get("DEFAULT_SITE")
                if default_site and default_site in config_engine.sites:
                    return default_site
                try:
                    browser = get_browser(auto_connect=False)
                    tab = browser.get_active_tab()
                    if tab is not None:
                        return _bc.detect_tab_site_domain(
                            getattr(tab, "url", ""),
                            list(config_engine.sites.keys())) or ""
                except Exception:
                    pass
                return ""
            except Exception:
                return ""

        site_key = _resolve_site_key(getattr(body, "model", ""))

        # ========== 1. 获取锁（按站点分离，不同站点可并行） ==========
        # 等待上限 300s：同站点前一请求可能是 GLM/DeepSeek 深度思考（3-5 分钟），
        # 60s 会让后续请求误报"服务繁忙: lock_timeout"
        try:
            acquired = await request_manager.acquire(ctx, timeout=300.0,
                                                     site_key=site_key)
        except asyncio.CancelledError:
            logger.info(f"请求 [{ctx.request_id}] 在排队时被取消")
            yield _pack_error("请求已取消", "cancelled")
            yield _pack_done()
            return
        
        if not acquired:
            reason = ctx.cancel_reason or "获取锁失败"
            logger.warning(f"请求 [{ctx.request_id}] {reason}")
            yield _pack_error(f"服务繁忙: {reason}", "busy")
            yield _pack_done()
            return
        
        # ========== 2. 启动断开检测 ==========
        disconnect_task = asyncio.create_task(
            watch_client_disconnect(request, ctx, check_interval=0.3)
        )
        
        # ========== 3. 设置浏览器停止检查器 ==========
        browser = get_browser(auto_connect=False)
        browser.set_stop_checker(ctx.should_stop)
        
        # ========== 4. 创建队列 + 工作线程 ==========
        chunk_queue = queue.Queue(maxsize=100)  # 限制队列大小防止内存溢出
        
        def _chunk_has_output(chunk) -> bool:
            if not isinstance(chunk, str) or not chunk.startswith("data: "):
                return False
            if chunk == "data: [DONE]":
                return False
            try:
                d = json.loads(chunk[6:])
                delta = (d.get("choices") or [{}])[0].get("delta", {})
                if delta.get("content"):
                    return True
                if delta.get("reasoning_content"):
                    return True
                if delta.get("tool_calls"):
                    return True
            except (json.JSONDecodeError, IndexError, TypeError):
                return False
            return False

        def _recover_page(site_domain: str = "", exclude_ports=None,
                          credit_models=None):
            """发送失败自恢复：先尝试切换浏览器实例（多账号故障转移，
            site_domain 指定时跳过候选模型积分全部耗尽的账号，
            exclude_ports 跳过本请求已尝试过的账号），切换成功直接用
            新账号重试；无备用实例时刷新会话页重试。"""
            old_port = getattr(browser, "port", None)
            try:
                if browser.switch_instance(exclude_port=old_port,
                                           site_domain=site_domain or None,
                                           exclude_ports=exclude_ports,
                                           credit_models=credit_models):
                    logger.warning(
                        f"请求 [{ctx.request_id}] 已切换到备用账号实例 "
                        f"{getattr(browser, 'instance_name', '?')}:"
                        f"{getattr(browser, 'port', '?')}")
                    time.sleep(2)
                    return True
            except Exception as e:
                logger.warning(f"实例切换失败: {e}")
            try:
                tab = getattr(browser, "tab", None)
                if tab is not None:
                    logger.warning(f"请求 [{ctx.request_id}] 零输出，刷新页面自恢复")
                    tab.refresh()
                    time.sleep(4)
            except Exception as e:
                logger.warning(f"页面自恢复失败: {e}")
            return False

        def _chunk_content_text(chunk) -> str:
            """提取 content delta 文本（仅正文，不含 reasoning/tool_calls）"""
            if not isinstance(chunk, str) or not chunk.startswith("data: "):
                return ""
            try:
                d = json.loads(chunk[6:])
                delta = (d.get("choices") or [{}])[0].get("delta", {})
                c = delta.get("content")
                return c if isinstance(c, str) else ""
            except (json.JSONDecodeError, IndexError, TypeError):
                return ""

        # 积分耗尽检测仅对短回复可靠：超过阈值视为正常长回复，直接放行
        _CREDIT_BUF_LIMIT = 400

        def worker():
            """在独立线程中执行同步生成器"""
            try:
                logger.info(f"请求 [{ctx.request_id}] 工作线程启动")
                try:
                    _n_inst = len(browser.list_instances())
                except Exception:
                    _n_inst = 2
                # "模型" = 用户 sites.json 配置的站点（poe.com、chatgpt.com...）。
                # 故障转移链：请求站点 → 配置里的其他站点（按配置顺序）。
                # 每个账号按 (站点, 模型) 分开记录积分；耗尽顺序：
                # 同站点先换账号，站点全部账号耗尽才切下一个站点
                site_chain = [site_key] if site_key else []
                for s in config_engine.sites.keys():
                    if s != site_key:
                        site_chain.append(s)
                # 客户端显式指定子模型（如 poe.com/Assistant）→ 该站点用它
                try:
                    import browser_core as _bcw
                    req_slug = _bcw.extract_model_slug(
                        getattr(body, "model", "") or "", site_key)
                except Exception:
                    req_slug = ""

                def _site_models(site):
                    """站点候选模型：请求站点用显式 slug，其余用主模型"""
                    if site == site_key and req_slug:
                        return [req_slug]
                    try:
                        return get_site_model_candidates(
                            config_engine.sites.get(site) or {})
                    except Exception:
                        return [""]

                max_attempts = (max(2, _n_inst) * max(1, len(site_chain))
                                if site_key else 2)
                last_credit_reset = 0.0
                tried = set()  # (port, site, model) 组合（防乒乓循环）
                site_idx = 0 if site_key else -1
                cur_site = site_key

                def _pick_model(site, port):
                    """当前账号在该站点第一个未耗尽且未试过的模型；无则 None"""
                    for mdl in _site_models(site):
                        if (port, site, mdl) in tried:
                            continue
                        if not CreditStateStore.is_exhausted(site, port, mdl):
                            return mdl
                    return None

                # 积分预检：默认实例可能已被标记耗尽/不可用。不预检会
                # 先拉起它的窗口、路由后才发现不可用、再切号关窗——
                # 每次请求空转一次"拉起+关窗"（2026-11-05 用户反馈
                # "老是拉起新实例而且拉起的实例还没使用"的根因）。
                # switch_instance 内部会跳过积分未重置的账号，只拉起
                # 真正可用的实例。
                if site_key:
                    try:
                        _p0 = int(getattr(browser, 'port', 0) or 0)
                        if _p0 and _pick_model(site_key, _p0) is None:
                            _tried0 = {p for p, s, m in tried
                                       if s == site_key}
                            logger.warning(
                                f"请求 [{ctx.request_id}] 预检：账号 :{_p0} "
                                f"在 {site_key} 均不可用，路由前先切号"
                                f"（避免拉起后弃用）")
                            _recover_page(
                                site_domain=site_key,
                                exclude_ports=_tried0,
                                credit_models=_site_models(site_key))
                    except Exception as _e:
                        logger.debug(f"积分预检异常: {_e}")

                for attempt in range(max_attempts):
                    if ctx.should_stop():
                        # 客户端已取消：停止重试，禁止继续切账号/关窗口
                        logger.info(
                            f"请求 [{ctx.request_id}] 已取消，停止账号重试")
                        break
                    port_now = int(getattr(browser, 'port', 0) or 0)
                    cur_site = site_chain[site_idx] if site_key else ""
                    model_slug = (_pick_model(cur_site, port_now)
                                  if site_key else "")
                    if site_key and model_slug is None:
                        # 当前账号在该站点均不可用 → 先换账号
                        _tried_ports = {p for p, s, m in tried
                                        if s == cur_site}
                        logger.warning(
                            f"请求 [{ctx.request_id}] 账号 :{port_now} "
                            f"在 {cur_site} 均不可用，尝试切换备用账号")
                        if _recover_page(
                                site_domain=cur_site,
                                exclude_ports=_tried_ports,
                                credit_models=_site_models(cur_site)):
                            continue
                        # 该站点所有账号都不可用 → 切下一个配置站点
                        if site_idx + 1 < len(site_chain):
                            site_idx += 1
                            logger.warning(
                                f"请求 [{ctx.request_id}] {cur_site} 所有账号"
                                f"积分均已耗尽，自动切换到站点 "
                                f"{site_chain[site_idx]} 继续")
                            continue
                        # 所有站点都耗尽：报明确错误（含重置时间）
                        _until = 0.0
                        for p, s, m in tried:
                            _until = max(
                                _until,
                                CreditStateStore.exhausted_until(s, p, m))
                        last_credit_reset = _until
                        _reset = time.strftime(
                            '%m-%d %H:%M', time.localtime(_until)) \
                            if _until else '每日重置'
                        chunk_queue.put((
                            "ERROR",
                            f"所有账号在全部配置站点的积分均已耗尽"
                            f"或不可用，预计 {_reset} 重置后可用"))
                        break

                    if site_key:
                        tried.add((port_now, cur_site, model_slug))
                    # 目标站点/子模型作为 model id 触发浏览器层路由
                    exec_model = getattr(body, "model", None)
                    if site_key:
                        exec_model = (f"{cur_site}/{model_slug}"
                                      if model_slug else cur_site)

                    saw_output = False
                    content_acc = []  # 本次尝试的正文累计（耗尽判定用）
                    # 短回复缓冲：积分耗尽回复很短，整体按序缓冲，
                    # 流结束后判定——耗尽则拦截并切号，正常则原序放行
                    buf_all = []
                    buf_content_len = 0
                    buf_flushed = False

                    def _flush_buf():
                        nonlocal buf_flushed
                        for c in buf_all:
                            chunk_queue.put(c)
                        del buf_all[:]
                        buf_flushed = True

                    gen = browser.execute_workflow_locked(
                        body.messages,
                        stream=True,
                        tools=body.tools,
                        tool_choice=body.tool_choice,
                        model=exec_model,
                        think_hint=body.wants_deep_think(),
                    )

                    for chunk in gen:
                        # 检查取消标志
                        if ctx.should_stop():
                            logger.info(f"请求 [{ctx.request_id}] 工作线程检测到取消")
                            gen.close()  # 显式释放生成器内持有的 browser 层锁
                            break

                        if _chunk_has_output(chunk):
                            saw_output = True
                        ct = _chunk_content_text(chunk)
                        if ct:
                            content_acc.append(ct)
                        if not buf_flushed:
                            buf_all.append(chunk)
                            buf_content_len += len(ct)
                            # 超过阈值或出现工具调用 → 一定是正常回复，放行
                            if buf_content_len >= _CREDIT_BUF_LIMIT \
                                    or 'tool_calls' in chunk:
                                _flush_buf()
                            continue
                        # 放入队列
                        chunk_queue.put(chunk)

                    # 流结束：若仍在缓冲（短回复），先做耗尽判定
                    if not buf_flushed:
                        buf_text = "".join(content_acc)
                        if (saw_output and site_key
                                and CREDIT_EXHAUSTED_RE.search(buf_text)
                                and len(buf_text.strip()) < _CREDIT_BUF_LIMIT):
                            # 仅模型回复命中耗尽文案才记录（超时/报错不计），
                            # 且只记当前 (账号, 站点, 模型) 组合
                            last_credit_reset = CreditStateStore.mark_exhausted(
                                cur_site, port_now, model_slug)
                            logger.warning(
                                f"请求 [{ctx.request_id}] 检测到积分耗尽回复，"
                                f"标记 {browser.instance_name}:{port_now} "
                                f"· {cur_site}"
                                f"{(' · ' + model_slug) if model_slug else ''}，"
                                f"自动改用其他账号/站点继续")
                            if ctx.should_stop():
                                gen.close()
                                break
                            # 回到循环顶部重选：同账号下一候选模型，
                            # 全部耗尽才换账号；组合全耗尽则报错
                            continue
                        _flush_buf()

                    if ctx.should_stop() or saw_output:
                        gen.close()
                        break
                    if ctx.should_stop():
                        # 客户端已取消：不切账号、不重试（此前取消后的
                        # 重试会启动新 chrome + 关旧窗口，触发连环切号）
                        gen.close()
                        break
                    if attempt < max_attempts - 1:
                        if not _recover_page(
                                site_domain=cur_site,
                                exclude_ports={p for p, s, m in tried
                                               if s == cur_site}):
                            logger.warning(
                                f"请求 [{ctx.request_id}] 重试后仍零输出"
                                f"（网页端可能未响应）")
                            chunk_queue.put((
                                "ERROR",
                                "网页端无响应：所有可用账号均未产出回复，"
                                "请稍后重试或检查账号积分/登录状态"))
                            break
                    else:
                        logger.warning(
                            f"请求 [{ctx.request_id}] 重试后仍零输出（网页端可能未响应）")
            except TooLongError as e:
                # 消息过长拒收：标记 (账号, 站点)，仪表盘可见可清除；
                # 不切账号重试（长文本换账号同样超限）
                logger.warning(f"请求 [{ctx.request_id}] 消息过长拒收: {e}")
                try:
                    CreditStateStore.mark_too_long(
                        cur_site, int(getattr(browser, 'port', 0) or 0))
                except Exception:
                    pass
                chunk_queue.put(("ERROR", str(e)))
            except Exception as e:
                logger.error(f"请求 [{ctx.request_id}] 工作线程异常: {e}")
                try:
                    gen.close()
                except Exception:
                    pass
                chunk_queue.put(("ERROR", str(e)))

            finally:
                # 发送结束标记
                chunk_queue.put(None)
                logger.debug(f"请求 [{ctx.request_id}] 工作线程结束")
        
        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        
        logger.info(f"请求 [{ctx.request_id}] 正在执行工作流...")
        
        # ========== 5. 从队列读取并发送 ==========
        while True:
            # 非阻塞地检查连接状态
            if await request.is_disconnected():
                logger.info(f"请求 [{ctx.request_id}] 检测到客户端断开")
                ctx.request_cancel("client_disconnected")
                break
            
            # 带超时地从队列获取
            try:
                chunk = await asyncio.to_thread(chunk_queue.get, timeout=0.5)
            except queue.Empty:
                # 超时，继续循环检查连接
                continue
            
            # 结束标记
            if chunk is None:
                logger.debug(f"请求 [{ctx.request_id}] 收到结束标记")
                break
            
            # 错误标记
            if isinstance(chunk, tuple) and chunk[0] == "ERROR":
                logger.error(f"请求 [{ctx.request_id}] 收到错误: {chunk[1]}")
                ctx.mark_failed(chunk[1])
                yield _pack_error(f"执行错误: {chunk[1]}", "internal_error")
                break
            
            # 正常数据
            yield chunk
            
            # 让出控制权，允许其他协程运行
            await asyncio.sleep(0)
        
        # 如果没有被取消且没有错误，标记完成
        if not ctx.should_stop() and ctx.status == RequestStatus.RUNNING:
            ctx.mark_completed()
    
    except asyncio.CancelledError:
        logger.info(f"请求 [{ctx.request_id}] 协程被取消")
        ctx.request_cancel("coroutine_cancelled")
        raise
    
    except Exception as e:
        logger.error(f"请求 [{ctx.request_id}] 异常: {e}", exc_info=True)
        ctx.mark_failed(str(e))
        yield _pack_error(f"执行错误: {str(e)}", "internal_error")
    
    finally:
        # ========== 6. 清理 ==========
        
        # 如果工作线程还在运行，设置取消标志
        if worker_thread and worker_thread.is_alive():
            ctx.request_cancel("cleanup")
            
            # 等待线程结束（最多 2 秒）
            worker_thread.join(timeout=2.0)
            
            if worker_thread.is_alive():
                logger.warning(f"请求 [{ctx.request_id}] 工作线程未能及时结束")
        
        # 清空队列
        try:
            while not chunk_queue.empty():
                chunk_queue.get_nowait()
        except:
            pass
        
        # 取消断开检测
        if disconnect_task:
            disconnect_task.cancel()
            try:
                await disconnect_task
            except asyncio.CancelledError:
                pass
        
        # 释放锁
        if acquired:
            request_manager.release(ctx, success=(ctx.status == RequestStatus.COMPLETED))
        
        logger.info(f"请求 [{ctx.request_id}] 结束 (状态: {ctx.status.value})")


async def _non_stream_with_lifecycle(
    request: Request,
    body: ChatRequest,
    ctx: RequestContext
) -> JSONResponse:
    """非流式响应 + 生命周期管理"""
    
    collected_content = []
    collected_reasoning = []  # 深度思考增量（reasoning_content 双字段兼容）
    collected_tool_calls = {}  # index -> {id, name, arguments}
    finish_reason = "stop"
    error_data = None

    async for chunk in _stream_with_lifecycle(request, body, ctx):
        if not isinstance(chunk, str):
            continue
        # 单个 chunk 可能包含多个 SSE 事件（如 finish 块 + [DONE]），按边界拆分
        for event in chunk.split("\n\n"):
            event = event.strip()
            if not event.startswith("data: "):
                continue
            data_str = event[6:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            if "error" in data:
                error_data = data
                break

            if "choices" in data and data["choices"]:
                choice = data["choices"][0]
                delta = choice.get("delta", {})
                content = delta.get("content", "")
                if content:
                    collected_content.append(content)
                _rc = (delta.get("reasoning_content")
                       or delta.get("reasoning") or "")
                if _rc:
                    collected_reasoning.append(_rc)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = collected_tool_calls.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
        if error_data:
            break

    if error_data:
        return JSONResponse(content=error_data, status_code=500)

    full_content = "".join(collected_content)
    message = {"role": "assistant", "content": full_content}
    full_reasoning = "".join(collected_reasoning)
    if full_reasoning:
        # 与流式 pack_reasoning_chunk 一致的双字段下发，IDE 按自身
        # 支持的字段独立展示/折叠思考过程
        message["reasoning_content"] = full_reasoning
        message["reasoning"] = full_reasoning
    if collected_tool_calls:
        message["tool_calls"] = [
            {
                "id": slot["id"] or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": slot["name"],
                             "arguments": slot["arguments"] or "{}"},
            }
            for _, slot in sorted(collected_tool_calls.items())
        ]
        if not full_content:
            message["content"] = None
    prompt_chars = sum(len(str(m.get("content") or "")) for m in body.messages)
    completion_chars = len(full_content) + sum(
        len(tc["function"].get("arguments") or "")
        for tc in message.get("tool_calls", []))
    response = {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason
        }],
        "usage": {
            "prompt_tokens": max(prompt_chars // 4, 1),
            "completion_tokens": max(completion_chars // 4, 1),
        }
    }
    response["usage"]["total_tokens"] = (
        response["usage"]["prompt_tokens"] + response["usage"]["completion_tokens"])

    return JSONResponse(content=response)


def _pack_error(message: str, code: str = "error") -> str:
    """打包 SSE 错误"""
    data = {
        "error": {
            "message": message,
            "type": "execution_error",
            "code": code
        }
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _pack_done() -> str:
    """打包 SSE 结束标记"""
    return "data: [DONE]\n\n"


# ================= 模型列表 =================

@app.get("/v1/models")
async def list_models(authenticated: bool = Depends(verify_auth)):
    # 模型 id = 各配置站点域名（客户端按 model id 路由到对应站点），
    # 另含 web-browser（活动 tab 兜底）。OpenClaw 等客户端会对照此列表
    # 校验/归一化 model id，缺了会导致 "This model was not found" 或被改写。
    model_ids = list(config_engine.sites.keys()) + ["web-browser"]
    seen = set()
    data = []
    for mid in model_ids:
        if not mid or mid in seen:
            continue
        seen.add(mid)
        data.append({
            "id": mid,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "universal-web-api"
        })
    return {"object": "list", "data": data}


# ================= 健康检查 =================

@app.get("/health")
async def health_check():
    try:
        browser = get_browser(auto_connect=False)
        browser_health = browser.health_check()
    except Exception as e:
        browser_health = {"connected": False, "error": str(e)}
    
    # 请求管理器状态
    rm_status = request_manager.get_status()
    
    response = {
        "service": "healthy",
        "version": "2.0.0",
        "browser": browser_health,
        "request_manager": rm_status,
        "config": {
            "sites_loaded": len(config_engine.sites),
            "auth_enabled": AppConfig.AUTH_ENABLED
        },
        "timestamp": int(time.time())
    }
    
    status_code = 200 if browser_health.get("connected") else 503
    return JSONResponse(content=response, status_code=status_code)


# ================= 配置管理 API =================

@app.get("/api/config")
async def get_config(authenticated: bool = Depends(verify_auth)):
    try:
        if os.path.exists(ConfigConstants.CONFIG_FILE):
            with open(ConfigConstants.CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        return {}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail="配置文件格式错误")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config")
async def save_config(
    request: ConfigUpdateRequest,
    authenticated: bool = Depends(verify_auth)
):
    try:
        with open(ConfigConstants.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(request.config, f, indent=2, ensure_ascii=False)
        
        config_engine.reload_config()
        
        return {
            "status": "success",
            "message": "配置已保存",
            "sites_count": len(request.config)
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/config/{domain}")
async def delete_site_config(
    domain: str,
    authenticated: bool = Depends(verify_auth)
):
    success = config_engine.delete_site_config(domain)
    
    if success:
        return {"status": "success", "message": f"已删除: {domain}"}
    else:
        raise HTTPException(status_code=404, detail=f"配置不存在: {domain}")


@app.get("/api/config/{domain}")
async def get_site_config(
    domain: str,
    authenticated: bool = Depends(verify_auth)
):
    if domain in config_engine.sites:
        return config_engine.sites[domain]
    else:
        raise HTTPException(status_code=404, detail=f"配置不存在: {domain}")



# ================= 系统设置 API =================

@app.get("/api/settings/env")
async def get_env_config(authenticated: bool = Depends(verify_auth)):
    """
    读取 .env 文件配置
    """
    try:
        env_path = Path(".env")
        config = {}
        
        if env_path.exists():
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    
                    # 跳过注释和空行
                    if not line or line.startswith('#'):
                        continue
                    
                    # 解析 KEY=VALUE
                    if '=' not in line:
                        continue
                    
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # 类型转换
                    if value.lower() == 'true':
                        value = True
                    elif value.lower() == 'false':
                        value = False
                    elif value.isdigit():
                        value = int(value)
                    elif re.match(r'^\d+\.\d+$', value):
                        value = float(value)
                    
                    config[key] = value
        
        return {"config": config}
    
    except Exception as e:
        logger.error(f"读取环境配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"读取失败: {str(e)}")


@app.post("/api/settings/env")
async def save_env_config(
    request: Request,
    authenticated: bool = Depends(verify_auth)
):
    """
    保存 .env 配置（保留注释结构）
    """
    try:
        data = await request.json()
        new_config = data.get("config", {})
        
        env_path = Path(".env")
        lines = []
        
        # 读取现有文件
        if env_path.exists():
            with open(env_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        
        new_lines = []
        
        for line in lines:
            stripped = line.strip()
            
            # 保留注释和空行
            if not stripped or stripped.startswith('#'):
                new_lines.append(line)
                continue
            
            # 处理配置行
            if '=' in stripped:
                key = stripped.split('=', 1)[0].strip()
                
                if key in new_config:
                    # 替换值
                    value = new_config[key]
                    
                    # 类型转换
                    if isinstance(value, bool):
                        value = 'true' if value else 'false'
                    elif isinstance(value, (int, float)):
                        value = str(value)
                    
                    new_lines.append(f"{key}={value}\n")
                else:
                    # 保留原行
                    new_lines.append(line)
            else:
                new_lines.append(line)
        
        # 写回文件
        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        logger.info(f"环境配置已保存: {len(new_config)} 项")
        
        return {
            "status": "success",
            "message": "环境配置已保存（部分配置需重启生效）",
            "updated_count": len(new_config)
        }
    
    except Exception as e:
        logger.error(f"保存环境配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")


@app.get("/api/settings/browser-constants")
async def get_browser_constants(authenticated: bool = Depends(verify_auth)):
    """
    读取浏览器常量配置
    """
    try:
        config_path = Path("browser_config.json")
        
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        else:
            # 返回默认值
            from browser_core import BrowserConstants
            
            # 如果 BrowserConstants 还没有 get_defaults 方法，返回硬编码的默认值
            config = {
                'DEFAULT_PORT': 9222,
                'CONNECTION_TIMEOUT': 10,
                'STEALTH_DELAY_MIN': 0.1,
                'STEALTH_DELAY_MAX': 0.3,
                'ACTION_DELAY_MIN': 0.15,
                'ACTION_DELAY_MAX': 0.3,
                'DEFAULT_ELEMENT_TIMEOUT': 3,
                'FALLBACK_ELEMENT_TIMEOUT': 1,
                'ELEMENT_CACHE_MAX_AGE': 5.0,
                'STREAM_CHECK_INTERVAL_MIN': 0.1,
                'STREAM_CHECK_INTERVAL_MAX': 1.0,
                'STREAM_CHECK_INTERVAL_DEFAULT': 0.3,
                'STREAM_SILENCE_THRESHOLD': 8.0,
                'STREAM_MAX_TIMEOUT': 600,
                'STREAM_INITIAL_WAIT': 180,
                'STREAM_RERENDER_WAIT': 0.5,
                'STREAM_CONTENT_SHRINK_TOLERANCE': 3,
                'STREAM_MIN_VALID_LENGTH': 10,
                'STREAM_STABLE_COUNT_THRESHOLD': 8,
                'STREAM_SILENCE_THRESHOLD_FALLBACK': 12,
                'MAX_MESSAGE_LENGTH': 100000,
                'MAX_MESSAGES_COUNT': 100,
                'STREAM_INITIAL_ELEMENT_WAIT': 10,
                'STREAM_MAX_ABNORMAL_COUNT': 5,
                'STREAM_MAX_ELEMENT_MISSING': 10,
                'STREAM_CONTENT_SHRINK_THRESHOLD': 0.3,
            }
        
        return {"config": config}
    
    except Exception as e:
        logger.error(f"读取浏览器常量失败: {e}")
        raise HTTPException(status_code=500, detail=f"读取失败: {str(e)}")


@app.post("/api/settings/browser-constants")
async def save_browser_constants(
    request: Request,
    authenticated: bool = Depends(verify_auth)
):
    """
    保存浏览器常量配置
    """
    try:
        data = await request.json()
        config = data.get("config", {})
        
        config_path = Path("browser_config.json")
        
        # 保存到文件
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        # 尝试热重载
        try:
            from browser_core import BrowserConstants
            if hasattr(BrowserConstants, 'reload'):
                BrowserConstants.reload()
                logger.info("浏览器常量已热重载")
            else:
                logger.warning("BrowserConstants 不支持热重载，需重启服务")
        except Exception as reload_error:
            logger.warning(f"热重载失败: {reload_error}")
        
        logger.info(f"浏览器常量已保存: {len(config)} 项")
        
        return {
            "status": "success",
            "message": "浏览器常量已保存",
            "updated_count": len(config)
        }
    
    except Exception as e:
        logger.error(f"保存浏览器常量失败: {e}")
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")
# ================= 多账号实例管理 API =================

def _find_chrome_exe() -> Optional[str]:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _read_browser_config_raw() -> dict:
    p = Path("browser_config.json")
    if p.exists():
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _write_browser_config_raw(cfg: dict):
    p = Path("browser_config.json")
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    try:
        from browser_core import BrowserConstants
        if hasattr(BrowserConstants, 'reload'):
            BrowserConstants.reload()
    except Exception:
        pass


def _debug_port_alive(port: int) -> bool:
    if not port:
        return False
    import socket
    s = socket.socket()
    s.settimeout(0.8)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except Exception:
        return False
    finally:
        s.close()


@app.get("/api/instances")
async def get_instances(authenticated: bool = Depends(verify_auth)):
    """多账号实例列表（含端口可达状态 + 各配置站点积分状态，全局跨站点）。"""
    cfg = _read_browser_config_raw()
    instances = cfg.get("INSTANCES") or [
        {"name": "main", "port": cfg.get("DEFAULT_PORT", 9222)}]
    sites = list(config_engine.sites.keys())
    now = time.time()
    out = []
    for inst in instances:
        port = int(inst.get("port") or 0)
        credit_sites = []
        for rec in CreditStateStore.records_for_port(port):
            # 只展示当前配置的站点，避免已删除站点的残留记录
            if rec["domain"] in sites:
                rec = dict(rec)
                rec["kind"] = "credit"
                credit_sites.append(rec)
        # 消息过长拒收标记（chatgpt 上传额度耗尽 → 大文本直输超限）
        for rec in CreditStateStore.too_long_records_for_port(port):
            if rec["domain"] in sites:
                credit_sites.append({
                    "domain": rec["domain"],
                    "model": "",
                    "kind": "toolong",
                    "exhausted_until": rec["exhausted_until"],
                })
        credit_sites.sort(key=lambda r: (r["domain"], r["model"]))
        out.append({
            "name": inst.get("name"),
            "port": port,
            "user_data_dir": inst.get("user_data_dir"),
            "alive": _debug_port_alive(port),
            "credit_sites": credit_sites,
        })
    return {"instances": out, "chrome_path": _find_chrome_exe(), "sites": sites}


@app.delete("/api/credit-state")
async def clear_credit_state(domain: str, port: int,
                             model: Optional[str] = None,
                             kind: Optional[str] = None,
                             authenticated: bool = Depends(verify_auth)):
    """手动清除某账号在某站点的状态记录（立即恢复该账号可用）。

    kind=toolong 清除消息过长拒收标记；默认清积分耗尽记录。
    model 不传时清除该账号在该站点全部模型的记录。"""
    if kind == "toolong":
        removed = CreditStateStore.clear_too_long(domain, port)
    else:
        removed = CreditStateStore.clear(domain, port, model=model)
    return {"status": "success", "removed": removed}


@app.post("/api/instances")
async def create_instance(request: Request,
                          authenticated: bool = Depends(verify_auth)):
    """新增账号：自动分配调试端口 + 独立 profile 目录，
    启动新的 Chrome 窗口并打开默认站点（poe.com）供登录。"""
    global config_engine
    try:
        data = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        default_site = BrowserConstants.get("DEFAULT_SITE") or "chatgpt.com"
        cfg = _read_browser_config_raw()
        instances = cfg.setdefault("INSTANCES", []) or []
        if not instances:
            instances = [{"name": "main", "port": cfg.get("DEFAULT_PORT", 9222)}]
            cfg["INSTANCES"] = instances
        used_ports = {int(i.get("port") or 0) for i in instances}
        port = (max(used_ports) + 1) if used_ports else 9223
        while port in used_ports or _debug_port_alive(port):
            port += 1
        base = (data.get("name") or "").strip() or f"acc{len(instances) + 1}"
        name = base
        n = 2
        names = {i.get("name") for i in instances}
        while name in names:
            name = f"{base}{n}"
            n += 1
        profile_dir = os.path.abspath(os.path.join("chrome_profiles", name))
        os.makedirs(profile_dir, exist_ok=True)
        chrome = _find_chrome_exe()
        if not chrome:
            raise HTTPException(status_code=500,
                                detail="未找到 Chrome/Edge，请先安装 Chrome")
        subprocess.Popen(
            [chrome, f"--remote-debugging-port={port}",
             f"--user-data-dir={profile_dir}",
             "--no-first-run", "--no-default-browser-check",
             f"https://{default_site}"],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        inst = {"name": name, "port": port, "user_data_dir": profile_dir}
        instances.append(inst)
        _write_browser_config_raw(cfg)
        logger.info(f"[Instance] 新增账号实例 {name}:{port}，profile={profile_dir}，"
                    f"已打开 https://{default_site} 供登录")
        return {"status": "success", "instance": {**inst, "alive": True},
                "message": f"浏览器窗口已启动（{name}, 端口 {port}），"
                           f"请在弹出的窗口里登录 {default_site} 账号"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"新增实例失败: {e}")
        raise HTTPException(status_code=500, detail=f"新增失败: {str(e)}")


@app.post("/api/instances/{name}/launch")
async def launch_instance(name: str, authenticated: bool = Depends(verify_auth)):
    """启动已配置但未运行的账号浏览器窗口。"""
    try:
        cfg = _read_browser_config_raw()
        inst = next((i for i in (cfg.get("INSTANCES") or [])
                     if i.get("name") == name), None)
        if not inst:
            raise HTTPException(status_code=404, detail=f"实例 {name} 不存在")
        port = int(inst.get("port") or 0)
        if _debug_port_alive(port):
            return {"status": "success", "message": f"{name} 已在运行（端口 {port}）"}
        profile_dir = inst.get("user_data_dir") or os.path.abspath(
            os.path.join("chrome_profiles", name))
        os.makedirs(profile_dir, exist_ok=True)
        chrome = _find_chrome_exe()
        if not chrome:
            raise HTTPException(status_code=500,
                                detail="未找到 Chrome/Edge，请先安装 Chrome")
        default_site = BrowserConstants.get("DEFAULT_SITE") or "chatgpt.com"
        subprocess.Popen(
            [chrome, f"--remote-debugging-port={port}",
             f"--user-data-dir={profile_dir}",
             "--no-first-run", "--no-default-browser-check",
             f"https://{default_site}"],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        logger.info(f"[Instance] 已启动实例 {name}:{port}")
        return {"status": "success",
                "message": f"已启动 {name}（端口 {port}），窗口稍后弹出"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"启动实例失败: {e}")
        raise HTTPException(status_code=500, detail=f"启动失败: {str(e)}")


@app.delete("/api/instances/{name}")
async def delete_instance(name: str, authenticated: bool = Depends(verify_auth)):
    """移除账号实例配置（可选同时关闭对应浏览器窗口）。"""
    try:
        cfg = _read_browser_config_raw()
        instances = cfg.get("INSTANCES") or []
        target = next((i for i in instances if i.get("name") == name), None)
        if not target:
            raise HTTPException(status_code=404, detail=f"实例 {name} 不存在")
        port = int(target.get("port") or 0)
        # 关闭对应浏览器进程（按监听该端口的进程）
        if port and _debug_port_alive(port):
            try:
                ps = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                     "-ErrorAction SilentlyContinue).OwningProcess"],
                    capture_output=True, text=True, timeout=10)
                pid = ps.stdout.strip().splitlines()[-1].strip() if ps.stdout.strip() else ""
                if pid.isdigit():
                    subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                                   capture_output=True, timeout=10)
                    logger.info(f"[Instance] 已关闭实例 {name} 进程 (pid {pid})")
            except Exception as e:
                logger.warning(f"[Instance] 关闭实例 {name} 进程失败: {e}")
        instances = [i for i in instances if i.get("name") != name]
        cfg["INSTANCES"] = instances
        _write_browser_config_raw(cfg)
        logger.info(f"[Instance] 已移除账号实例 {name}")
        return {"status": "success", "message": f"实例 {name} 已移除"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"移除实例失败: {e}")
        raise HTTPException(status_code=500, detail=f"移除失败: {str(e)}")


# ================= 日志 API =================

@app.get("/api/logs")
async def get_logs(since: float = 0, authenticated: bool = Depends(verify_auth)):
    logs = log_collector.get_recent(since)
    return {"logs": logs, "timestamp": time.time()}


@app.delete("/api/logs")
async def clear_logs(authenticated: bool = Depends(verify_auth)):
    log_collector.clear()
    return {"status": "success"}


# ================= 调试 API =================

@app.post("/api/debug/test-selector")
async def test_selector(
    request: Request,
    authenticated: bool = Depends(verify_auth)
):
    if not AppConfig.DEBUG:
        raise HTTPException(status_code=403, detail="调试功能未启用")
    
    try:
        data = await request.json()
        selector = data.get("selector", "")
        timeout = data.get("timeout", 2)
        highlight = data.get("highlight", False)
        
        if not selector:
            raise HTTPException(status_code=400, detail="缺少 selector")
        
        browser = get_browser()
        tab = browser.get_active_tab()
        elements = tab.eles(selector, timeout=timeout)
        
        if not elements:
            return {"success": False, "count": 0, "message": "元素未找到"}
        
        if not isinstance(elements, list):
            elements = [elements]
        
        result = {
            "success": True,
            "count": len(elements),
            "elements": []
        }
        
        for idx, ele in enumerate(elements):
            result["elements"].append({
                "index": idx,
                "tag": ele.tag,
                "text": ele.text[:100] if ele.text else ""
            })
            
            if highlight:
                try:
                    tab.run_js(f"""
                        (function() {{
                            const elements = document.querySelectorAll('{selector}');
                            if (elements[{idx}]) {{
                                const el = elements[{idx}];
                                el.style.outline = '3px solid red';
                                el.style.outlineOffset = '2px';
                                setTimeout(() => {{
                                    el.style.outline = '';
                                    el.style.outlineOffset = '';
                                }}, 5000);
                            }}
                        }})();
                    """)
                except Exception:
                    pass
        
        return result
    
    except Exception as e:
        return {"success": False, "count": 0, "message": str(e)}


@app.get("/api/debug/request-status")
async def request_status(authenticated: bool = Depends(verify_auth)):
    """查看请求管理器状态"""
    return request_manager.get_status()


@app.post("/api/debug/force-release")
async def force_release(authenticated: bool = Depends(verify_auth)):
    """强制释放锁（紧急情况）"""
    if not AppConfig.DEBUG:
        raise HTTPException(status_code=403, detail="调试功能未启用")
    
    was_locked = request_manager.is_locked()
    released = request_manager.force_release()
    is_now_locked = request_manager.is_locked()
    
    logger.warning(f"手动解锁: was={was_locked}, released={released}, now={is_now_locked}")
    
    return {
        "was_locked": was_locked,
        "released": released,
        "is_now_locked": is_now_locked
    }


@app.post("/api/debug/cancel-current")
async def cancel_current(authenticated: bool = Depends(verify_auth)):
    """取消当前正在执行的请求"""
    current_id = request_manager.get_current_request_id()
    
    if not current_id:
        return {"cancelled": False, "message": "没有正在执行的请求"}
    
    success = request_manager.cancel_current("manual_cancel")
    
    return {
        "cancelled": success,
        "request_id": current_id
    }


# ================= Dashboard =================

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    if not AppConfig.DASHBOARD_ENABLED:
        raise HTTPException(status_code=403, detail="Dashboard 未启用")
    
    try:
        dashboard_path = Path(AppConfig.DASHBOARD_FILE)
        
        if dashboard_path.exists():
            content = dashboard_path.read_text(encoding="utf-8")
            return HTMLResponse(content=content)
        else:
            return HTMLResponse(
                content="<h1>Dashboard 文件未找到</h1>",
                status_code=404
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dashboard.js")
async def dashboard_js():
    js_path = Path(AppConfig.DASHBOARD_JS_FILE)
    if js_path.exists():
        return FileResponse(js_path, media_type="application/javascript")
    else:
        raise HTTPException(status_code=404)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/")
async def root():
    return {
        "service": "Universal Web-to-API",
        "version": "2.0.0",
        "endpoints": {
            "chat": "/v1/chat/completions",
            "models": "/v1/models",
            "health": "/health",
            "dashboard": "/dashboard"
        }
    }


# ================= 异常处理 =================

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=404,
        content={"error": {"message": "接口不存在", "path": str(request.url.path)}}
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception):
    logger.error(f"内部错误: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "服务器内部错误"}}
    )


# ================= 主入口 =================

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "=" * 60)
    print("环境变量配置（可选）:")
    print("  APP_HOST=0.0.0.0          # 监听地址")
    print("  APP_PORT=8199             # 监听端口")
    print("  APP_DEBUG=true            # 调试模式")
    print("  AUTH_ENABLED=true         # 启用认证")
    print("  AUTH_TOKEN=your-secret    # 认证令牌")
    print("  BROWSER_PORT=9222         # 浏览器端口")
    print("=" * 60 + "\n")
    
    uvicorn.run(
        app,
        host=AppConfig.HOST,
        port=AppConfig.PORT,
        log_level=AppConfig.LOG_LEVEL.lower(),
        access_log=False
    )
