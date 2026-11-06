"""chrome_launcher.py - 把浏览器窗口起在用户真正看得见的那个桌面上

问题：本服务由 nssm 以 LocalSystem 身份运行，进程处在 **Session 0**。
Session 0 没有交互式桌面（Vista 起 Windows 就把服务和用户桌面隔离了），
所以从这里 subprocess.Popen 拉起的 Chrome：

  - 进程活着、调试端口也通（工具连得上，仪表盘显示"运行中"）
  - 但窗口画在一个没人能看见的桌面上，任务栏里也没有
  - 用户于是永远无法在这些 profile 里登录，账号看起来"莫名其妙没登录"

修法：从 Session 0 用当前登录用户的 token 把进程起到他自己的会话里
（CreateProcessAsUser + winsta0\\default）。需要 SE_TCB 权限来取用户令牌，
LocalSystem 正好有——这也是为什么这件事只能在服务里做，脚本里做不了。

非 Windows、无人登录、会话被锁屏等情况一律返回失败并给出原因，由调用方
退回原来的 Popen（仍然可用，只是窗口看不见），绝不静默假装成功。
"""

import ctypes
import ctypes.wintypes as wt
import logging
import os
import subprocess
import time
from typing import List, Optional, Tuple

logger = logging.getLogger('browser')

_IS_WIN = os.name == 'nt'

# ---- Win32 常量 ----
_TOKEN_QUERY = 0x0008
_TOKEN_DUPLICATE = 0x0002
_TOKEN_ASSIGN_PRIMARY = 0x0001
_TOKEN_ADJUST_PRIVILEGES = 0x0020
_SECURITY_IMPERSONATION = 2
_TOKEN_PRIMARY = 1
_MAXIMUM_ALLOWED = 0x02000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
_WTS_ACTIVE = 0
_INVALID_SESSION = 0xFFFFFFFF

_kernel32 = ctypes.windll.kernel32 if _IS_WIN else None
_advapi32 = ctypes.windll.advapi32 if _IS_WIN else None
_wts32 = ctypes.windll.wtsapi32 if _IS_WIN else None
_userenv = ctypes.windll.userenv if _IS_WIN else None


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR),
                ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD), ("wReserved", wt.WORD),
                ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


def current_session_id() -> int:
    if not _IS_WIN:
        return -1
    try:
        return int(_kernel32.WTSGetActiveConsoleSessionId())
    except Exception:
        return -1


def _gle() -> int:
    """最近的 Win32 错误码。用 kernel32.GetLastError 而不是
    ctypes.get_last_error：后者只在 use_last_error=True 的 DLL 上有意义。"""
    try:
        return int(_kernel32.GetLastError())
    except Exception:
        return 0


def _enable_privilege(name: str) -> None:
    """CreateProcessAsUser 要求调用方令牌里 SE_INCREASE_QUOTA_NAME 处于启用态；
    LocalSystem 有这个权限但默认未必启用。失败不致命，只让后面的调用报错更清楚。"""
    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.DWORD)]

    class LUID_AND_ATTR(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]

    class TOKEN_PRIVS(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wt.DWORD),
                    ("Privileges", LUID_AND_ATTR * 1)]

    htok = wt.HANDLE()
    if not _advapi32.OpenProcessToken(
            _kernel32.GetCurrentProcess(),
            _TOKEN_ADJUST_PRIVILEGES | _TOKEN_QUERY, ctypes.byref(htok)):
        return
    try:
        luid = LUID()
        if not _advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return
        tp = TOKEN_PRIVS()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 0x0002  # SE_PRIVILEGE_ENABLED
        _advapi32.AdjustTokenPrivileges(htok, False, ctypes.byref(tp), 0,
                                        None, None)
    finally:
        _kernel32.CloseHandle(htok)


def _candidate_sessions() -> List[int]:
    """优先物理控制台会话；拿不到就找状态为 Active 的会话（RDP 登录时控制台
    会话可能根本不存在，只挑控制台会把窗口起进一个没人用的会话里）。"""
    out: List[int] = []
    # WTSGetActiveConsoleSessionId 的返回值按有符号 int 取回时，
    # "无控制台会话"是 -1（0xFFFFFFFF），两种写法都要认
    sid = int(_kernel32.WTSGetActiveConsoleSessionId())
    if sid not in (_INVALID_SESSION, -1):
        out.append(sid)
    try:
        p_info = ctypes.c_void_p()
        n_cnt = wt.DWORD(0)
        if _wts32.WTSEnumerateSessionsW(0, 0, 1, ctypes.byref(p_info),
                                        ctypes.byref(n_cnt)):
            class _SES(ctypes.Structure):
                _fields_ = [("dwState", wt.DWORD),
                            ("lpWinStation", wt.LPWSTR),
                            ("SessionId", wt.DWORD)]
            arr = ctypes.cast(p_info,
                              ctypes.POINTER(_SES * int(n_cnt.value))).contents
            for i in range(int(n_cnt.value)):
                s = int(arr[i].SessionId)
                if int(arr[i].dwState) == _WTS_ACTIVE and s not in out:
                    out.append(s)
            _wts32.WTSFreeMemory(p_info)
    except Exception as e:
        logger.debug(f"[Win] 枚举会话失败: {e}")
    return out


def _argv_to_cmdline(argv: List[str]) -> str:
    """CreateProcess 只吃一条命令行字符串。直接用标准库的 Windows 引用规则
    （subprocess.list2cmdline），自己写容易在反斜杠与引号交界上出错。"""
    return subprocess.list2cmdline([str(a) for a in argv])


def launch_in_user_session(argv: List[str],
                           cwd: Optional[str] = None) -> Tuple[bool, str]:
    """在交互用户会话里启动 argv。返回 (是否成功, 说明)。"""
    if not _IS_WIN:
        return False, "非 Windows，跨会话启动不适用"
    if not argv:
        return False, "argv 为空"
    sess = _candidate_sessions()
    if not sess:
        return False, "没有活跃用户会话（无人登录？屏幕锁定也会看不见窗口）"
    cmdline = _argv_to_cmdline(list(argv))
    cwd_w = str(cwd) if cwd else None
    last = "未尝试"
    _enable_privilege("SeIncreaseQuotaPrivilege")
    for sid in sess:
        h_user = wt.HANDLE()
        if not _wts32.WTSQueryUserToken(sid, ctypes.byref(h_user)):
            last = f"会话 {sid} 取用户令牌失败 err={_gle()}"
            continue
        h_prim = wt.HANDLE()
        if not _advapi32.DuplicateTokenEx(
                h_user, _MAXIMUM_ALLOWED, None, _SECURITY_IMPERSONATION,
                _TOKEN_PRIMARY, ctypes.byref(h_prim)):
            _kernel32.CloseHandle(h_user)
            last = f"会话 {sid} 复制令牌失败 err={_gle()}"
            continue
        env = ctypes.c_void_p()
        made_env = bool(_userenv.CreateEnvironmentBlock(ctypes.byref(env),
                                                       h_prim, False))
        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(_STARTUPINFOW)
        si.lpDesktop = "winsta0\\default"   # 关键：不写这行窗口仍落在服务桌面
        pi = _PROCESS_INFORMATION()
        flags = (_CREATE_UNICODE_ENVIRONMENT | _CREATE_NEW_PROCESS_GROUP
                 | _DETACHED_PROCESS)
        ok = bool(_advapi32.CreateProcessAsUserW(
            h_prim, None, cmdline, None, None, False, flags,
            env if made_env else None, cwd_w, ctypes.byref(si),
            ctypes.byref(pi)))
        err = _gle()
        if made_env:
            _userenv.DestroyEnvironmentBlock(env)
        _kernel32.CloseHandle(h_prim)
        _kernel32.CloseHandle(h_user)
        if ok:
            _kernel32.CloseHandle(pi.hThread)
            _kernel32.CloseHandle(pi.hProcess)
            return True, f"已在用户会话 {sid} 启动，pid={pi.dwProcessId}"
        last = f"会话 {sid} CreateProcessAsUser 失败 err={err}"
    return False, last


def _ps_quote(value: str) -> str:
    """嵌进 PowerShell 单引号字符串里（单引号内是字面量，只有 ' 需要翻倍）。"""
    return str(value).replace("'", "''")


def _run_ps(script: str, timeout: float = 15.0) -> str:
    """跑一段 PowerShell 并取回 stdout。

    值一律内联进脚本字符串，不用 `powershell -Command "..." 参数` 传参：
    那种写法会把后面的参数当成**第二条语句**执行（`9222,9223` 会被原样打印出来），
    $args 根本拿不到，端口归属因此永远查不出来。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return r.stdout or ""
    except Exception as e:
        logger.debug(f"[Win] PowerShell 查询失败: {e}")
        return ""


def port_owner_pid(port: int) -> Optional[int]:
    """占用该 TCP 端口的进程 PID（PowerShell 实现，不依赖第三方库）。"""
    if not _IS_WIN or not port:
        return None
    out = _ps_quote(int(port))
    ps = ("$c = Get-NetTCPConnection -LocalPort %s -State Listen "
          "-ErrorAction SilentlyContinue | Select-Object -First 1; "
          "if ($c) { $c.OwningProcess }" % out)
    s = _run_ps(ps, 8.0).strip()
    return int(s) if s.isdigit() else None


def process_session_id(pid: int) -> Optional[int]:
    if not _IS_WIN or not pid:
        return None
    s = _run_ps("(Get-Process -Id %d -ErrorAction SilentlyContinue).SessionId"
                % int(pid), 8.0).strip()
    return int(s) if s.lstrip('-').isdigit() else None


def window_status(ports: List[int]) -> dict:
    """每个调试端口的窗口到底可不可见：一次 PowerShell 调用批量取回
    （逐端口起进程查太慢，仪表盘每 15s 轮询一次）。"""
    ports = [int(p) for p in ports if p]
    out = {p: {"pid": None, "session": None, "visible": False} for p in ports}
    if not _IS_WIN or not ports:
        return out
    lst = ",".join(str(p) for p in ports)
    ps = ("foreach ($p in @(%s)) { "
          "$c = Get-NetTCPConnection -LocalPort $p -State Listen "
          "-ErrorAction SilentlyContinue | Select-Object -First 1; "
          "if ($c) { $s = (Get-Process -Id $c.OwningProcess "
          "-ErrorAction SilentlyContinue).SessionId; "
          "\"$p,$($c.OwningProcess),$s\" } }" % lst)
    interactive = current_session_id()
    for line in _run_ps(ps, 12.0).splitlines():
        f = [x.strip() for x in line.split(',')]
        if len(f) < 2 or not f[0].isdigit() or not f[1].isdigit():
            continue
        port = int(f[0])
        sess = int(f[2]) if len(f) > 2 and f[2].lstrip('-').isdigit() else None
        out[port] = {"pid": int(f[1]), "session": sess,
                     "visible": bool(sess is not None and interactive >= 0
                                     and sess == interactive)}
    return out


def find_profile_chromes(profile_dir: str) -> List[Tuple[int, int]]:
    """列出用这个 profile 启动的 Chrome 主进程 [(pid, session_id)]。

    只认命令行里带该 --user-data-dir 且不含 --type= 的主进程，
    用户的日常 Chrome 不会被误伤。"""
    if not _IS_WIN or not profile_dir:
        return []
    q = _ps_quote(profile_dir)
    # profile 路径在命令行里可能带引号也可能不带（路径没空格时不会加引号），
    # 两种都认；路径含 + 和 \，-match 右侧是正则，必须 [regex]::Escape，
    # 否则永远匹配不上（曾因此返回空表，看起来像"没有泄漏窗口"）。
    # 不带引号那种必须要求路径后紧跟空格或引号，否则 acc1 会把 acc12 也算进来
    ps = ("$pat = [regex]::Escape('--user-data-dir=\"%s\"') + '|' + "
          "[regex]::Escape('--user-data-dir=%s ') + '|' + "
          "[regex]::Escape('--user-data-dir=%s\"'); "
          "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -notmatch '--type=' -and "
          "$_.CommandLine -match $pat } | "
          "ForEach-Object { \"$($_.ProcessId),$($_.SessionId)\" }"
          % (q, q, q))
    out = []
    for line in _run_ps(ps, 20.0).splitlines():
        f = [x.strip() for x in line.split(',')]
        if len(f) >= 2 and f[0].isdigit() and f[1].lstrip('-').isdigit():
            out.append((int(f[0]), int(f[1])))
    return out


def kill_pid(pid: int, wait: float = 3.0) -> bool:
    """杀掉指定进程（含其 Chrome 子进程树）。"""
    if not pid:
        return False
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                       capture_output=True, text=True, timeout=20,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW',
                                             0))
    except Exception:
        return False
    deadline = time.time() + max(0.0, wait)
    while time.time() < deadline:
        if process_session_id(int(pid)) is None:
            return True
        time.sleep(0.4)
    return process_session_id(int(pid)) is None


def wait_port_open(port: int, timeout: float = 30.0) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(0.8)
        try:
            s.connect(("127.0.0.1", int(port)))
            return True
        except Exception:
            time.sleep(1.0)
        finally:
            try:
                s.close()
            except Exception:
                pass
    return False
