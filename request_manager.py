"""
request_manager.py - 请求生命周期管理器

职责：
- 请求队列管理（FIFO）
- 并发控制（全局锁）
- 请求状态追踪
- 取消信号管理
- 客户端断开检测
"""

import asyncio
import threading
import time
import uuid
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Callable, Any
from collections import OrderedDict

logger = logging.getLogger('request_manager')


# ================= 请求状态 =================

class RequestStatus(Enum):
    """请求状态枚举"""
    QUEUED = "queued"       # 在队列中等待
    RUNNING = "running"     # 正在执行
    COMPLETED = "completed" # 正常完成
    CANCELLED = "cancelled" # 被取消
    FAILED = "failed"       # 执行失败


# ================= 请求上下文 =================

@dataclass
class RequestContext:
    """
    请求上下文 - 贯穿请求整个生命周期
    
    核心职责：
    1. 唯一标识请求
    2. 追踪请求状态
    3. 传递取消信号
    """
    request_id: str
    status: RequestStatus = RequestStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    
    # 取消控制
    _cancel_flag: bool = field(default=False, repr=False)
    cancel_reason: Optional[str] = None
    
    # 线程安全
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    
    def should_stop(self) -> bool:
        """
        检查是否应该停止（线程安全）
        
        供 browser_core 在循环中调用
        """
        with self._lock:
            return self._cancel_flag
    
    def request_cancel(self, reason: str = "unknown"):
        """
        请求取消（设置标志，不立即生效）
        
        实际停止由 browser_core 在检查点执行
        """
        with self._lock:
            if self._cancel_flag:
                return  # 已经取消过
            
            self._cancel_flag = True
            self.cancel_reason = reason
            
            if self.status == RequestStatus.RUNNING:
                self.status = RequestStatus.CANCELLED
            
            logger.info(f"请求 [{self.request_id}] 收到取消信号 (原因: {reason})")
    
    def mark_running(self):
        """标记为运行中"""
        with self._lock:
            self.status = RequestStatus.RUNNING
            self.started_at = time.time()
    
    def mark_completed(self):
        """标记为完成"""
        with self._lock:
            if self.status == RequestStatus.RUNNING:
                self.status = RequestStatus.COMPLETED
            self.finished_at = time.time()
    
    def mark_failed(self, reason: str = None):
        """标记为失败"""
        with self._lock:
            self.status = RequestStatus.FAILED
            self.finished_at = time.time()
            if reason:
                self.cancel_reason = reason
    
    def get_duration(self) -> float:
        """获取执行时长"""
        end = self.finished_at or time.time()
        start = self.started_at or self.created_at
        return end - start
    
    def is_terminal(self) -> bool:
        """是否已结束（不可再变化）"""
        return self.status in (
            RequestStatus.COMPLETED,
            RequestStatus.CANCELLED,
            RequestStatus.FAILED
        )


# ================= 请求管理器 =================

class RequestManager:
    """
    请求管理器 - 单例模式
    
    核心功能：
    1. FIFO 队列：先来先服务
    2. 全局锁：同时只执行一个请求
    3. 状态追踪：每个请求有完整生命周期
    4. 取消传递：可靠地取消正在执行的请求
    
    使用方式：
        ctx = request_manager.create_request()
        try:
            acquired = await request_manager.acquire(ctx)
            if acquired:
                # 执行工作
                pass
        finally:
            request_manager.release(ctx)
    """
    
    _instance: Optional['RequestManager'] = None
    _instance_lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        # 执行锁：按站点（模型）分离，不同站点可并行执行
        self._exec_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

        # 请求追踪（有序字典，FIFO）
        self._requests: OrderedDict[str, RequestContext] = OrderedDict()
        self._requests_lock = threading.Lock()

        # 当前执行的请求（全局展示用；per-site 精确值在 _current_by_site）
        self._current_request_id: Optional[str] = None
        self._current_by_site: Dict[str, str] = {}
        
        # 配置
        self._max_queue_size = 20       # 最大排队数
        self._max_history = 100         # 最大历史记录
        self._lock_timeout = 300.0      # 锁超时（秒）
        
        self._initialized = True
        logger.info("RequestManager 初始化完成")
    
    # ================= 请求创建 =================
    
    def create_request(self) -> RequestContext:
        """
        创建新请求
        
        Returns:
            RequestContext 实例
        """
        request_id = self._generate_id()
        ctx = RequestContext(request_id=request_id)
        
        with self._requests_lock:
            self._requests[request_id] = ctx
            self._cleanup_old_requests()
        
        logger.info(f"请求 [{request_id}] 已创建")
        return ctx
    
    def _generate_id(self) -> str:
        """生成唯一请求 ID"""
        timestamp = int(time.time() * 1000) % 100000
        unique = uuid.uuid4().hex[:4]
        return f"{timestamp:05d}-{unique}"
    
    def _cleanup_old_requests(self):
        """清理旧请求记录"""
        while len(self._requests) > self._max_history:
            oldest_id, oldest_ctx = next(iter(self._requests.items()))
            if oldest_ctx.is_terminal():
                del self._requests[oldest_id]
            else:
                break  # 不删除未完成的请求
    
    # ================= 锁管理 =================
    
    def _get_lock(self, site_key: str) -> threading.Lock:
        """获取站点对应的执行锁（惰性创建）。"""
        key = site_key or ""
        with self._locks_guard:
            lock = self._exec_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._exec_locks[key] = lock
            return lock

    async def acquire(self, ctx: RequestContext,
                      timeout: float = None,
                      site_key: str = None) -> bool:
        """
        异步获取执行锁
        
        Args:
            ctx: 请求上下文
            timeout: 超时时间（秒），None 使用默认值
        
        Returns:
            是否成功获取锁
        """
        timeout = timeout or self._lock_timeout
        site_key = site_key or ""
        ctx.site_key = site_key

        # 检查队列大小
        queue_size = self._get_queue_size()
        if queue_size >= self._max_queue_size:
            logger.warning(f"请求 [{ctx.request_id}] 被拒绝：队列已满 ({queue_size})")
            ctx.mark_failed("queue_full")
            return False

        # 打印等待日志
        current_of_site = self._current_by_site.get(site_key)
        if current_of_site:
            logger.info(f"请求 [{ctx.request_id}] 等待中，"
                       f"当前执行: [{current_of_site}] (站点: {site_key or '默认'})")

        # 在线程池中等待锁
        try:
            acquired = await asyncio.wait_for(
                asyncio.to_thread(self._sync_acquire, timeout, site_key,
                                  lambda: ctx.should_stop()),
                timeout=timeout + 1  # 额外 1 秒缓冲
            )

            if acquired:
                self._current_request_id = ctx.request_id
                self._current_by_site[site_key] = ctx.request_id
                ctx.mark_running()
                logger.info(f"请求 [{ctx.request_id}] 开始执行 (站点: {site_key or '默认'})")
                return True
            else:
                logger.warning(f"请求 [{ctx.request_id}] 获取锁超时")
                ctx.mark_failed("lock_timeout")
                return False
        
        except asyncio.TimeoutError:
            logger.warning(f"请求 [{ctx.request_id}] 等待超时")
            ctx.mark_failed("acquire_timeout")
            return False
        
        except asyncio.CancelledError:
            logger.info(f"请求 [{ctx.request_id}] 在等待时被取消")
            ctx.request_cancel("cancelled_while_waiting")
            raise
    
    def _sync_acquire(self, timeout: float, site_key: str = "",
                      cancel_check: Optional[Callable[[], bool]] = None) -> bool:
        """同步获取锁（在线程池中执行）。

        不能直接 lock.acquire(timeout=N) 阻塞整段超时：协程被取消后
        asyncio.wait_for 只取消 await，底层线程仍卡在 acquire 排队，
        之后若拿到锁将无人消费、永不释放（实测导致后续请求全部
        lock_timeout）。改为 0.5s 短轮询 + 取消检查；拿到锁后二次
        检查，已取消立即释放。"""
        lock = self._get_lock(site_key)
        deadline = time.time() + timeout
        while True:
            if cancel_check and cancel_check():
                return False
            if lock.acquire(timeout=0.5):
                if cancel_check and cancel_check():
                    try:
                        lock.release()
                    except RuntimeError:
                        pass
                    return False
                return True
            if time.time() >= deadline:
                return False
    
    def release(self, ctx: RequestContext, success: bool = True):
        """
        释放执行锁
        
        Args:
            ctx: 请求上下文
            success: 是否成功完成
        """
        try:
            # 释放该请求站点对应的锁
            site_key = getattr(ctx, "site_key", "") or ""
            lock = self._get_lock(site_key)
            if lock.locked():
                lock.release()
            self._current_by_site.pop(site_key, None)

            # 更新状态
            if self._current_request_id == ctx.request_id:
                self._current_request_id = None
            
            # 设置最终状态（如果还未设置）
            if ctx.status == RequestStatus.RUNNING:
                if success:
                    ctx.mark_completed()
                else:
                    ctx.mark_failed()
            
            # 日志
            duration = ctx.get_duration()
            logger.info(f"请求 [{ctx.request_id}] 结束 "
                       f"(状态: {ctx.status.value}, 耗时: {duration:.2f}s)")
        
        except RuntimeError as e:
            logger.warning(f"释放锁异常: {e}")
    
    # ================= 取消控制 =================
    
    def cancel_request(self, request_id: str, 
                       reason: str = "manual") -> bool:
        """
        取消指定请求
        
        Args:
            request_id: 请求 ID
            reason: 取消原因
        
        Returns:
            是否成功发送取消信号
        """
        with self._requests_lock:
            ctx = self._requests.get(request_id)
        
        if not ctx:
            logger.debug(f"请求 [{request_id}] 不存在")
            return False
        
        if ctx.is_terminal():
            logger.debug(f"请求 [{request_id}] 已结束")
            return False
        
        ctx.request_cancel(reason)
        return True
    
    def cancel_current(self, reason: str = "cancel_current") -> bool:
        """取消当前正在执行的请求"""
        current_id = self._current_request_id
        if current_id:
            return self.cancel_request(current_id, reason)
        return False
    
    # ================= 状态查询 =================
    
    def get_request(self, request_id: str) -> Optional[RequestContext]:
        """获取请求上下文"""
        with self._requests_lock:
            return self._requests.get(request_id)
    
    def is_locked(self) -> bool:
        """检查是否有任意站点锁被占用"""
        with self._locks_guard:
            return any(lock.locked() for lock in self._exec_locks.values())
    
    def get_current_request_id(self) -> Optional[str]:
        """获取当前执行的请求 ID"""
        return self._current_request_id
    
    def _get_queue_size(self) -> int:
        """获取等待中的请求数量"""
        with self._requests_lock:
            return sum(
                1 for ctx in self._requests.values()
                if ctx.status == RequestStatus.QUEUED
            )
    
    def get_status(self) -> Dict[str, Any]:
        """获取管理器状态"""
        with self._requests_lock:
            status_counts = {}
            for ctx in self._requests.values():
                s = ctx.status.value
                status_counts[s] = status_counts.get(s, 0) + 1
            
            return {
                "is_locked": self.is_locked(),
                "current_request": self._current_request_id,
                "queue_size": status_counts.get("queued", 0),
                "total_tracked": len(self._requests),
                "status_counts": status_counts
            }
    
    # ================= 紧急操作 =================
    
    def force_release(self) -> bool:
        """
        强制释放锁（紧急情况使用）
        
        Returns:
            是否执行了释放
        """
        logger.warning("⚠️ 执行强制释放锁")
        
        released = False
        
        # 取消当前请求
        if self._current_request_id:
            ctx = self.get_request(self._current_request_id)
            if ctx:
                ctx.request_cancel("force_release")
            self._current_request_id = None

        with self._locks_guard:
            self._current_by_site.clear()

        # 强制释放所有站点锁
        try:
            with self._locks_guard:
                locks = list(self._exec_locks.values())
            for lock in locks:
                if lock.locked():
                    lock.release()
                    released = True
        except RuntimeError:
            pass

        return released


# ================= 全局单例 =================

request_manager = RequestManager()


# ================= 辅助函数 =================

async def watch_client_disconnect(request, ctx: RequestContext,
                                   check_interval: float = 0.5):
    """
    监控客户端连接状态
    
    在后台运行，检测到断开时设置取消标志
    
    Args:
        request: FastAPI Request 对象
        ctx: 请求上下文
        check_interval: 检查间隔（秒）
    """
    try:
        while not ctx.is_terminal():
            # Starlette 的断开检测
            if await request.is_disconnected():
                ctx.request_cancel("client_disconnected")
                logger.info(f"请求 [{ctx.request_id}] 客户端已断开")
                break
            
            await asyncio.sleep(check_interval)
    
    except asyncio.CancelledError:
        # 正常取消（请求结束时）
        pass
    except Exception as e:
        logger.debug(f"断开检测异常: {e}")
