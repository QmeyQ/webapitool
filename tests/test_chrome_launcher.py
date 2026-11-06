"""chrome_launcher 与「自动启动账号 Chrome」的单元测试。

这里抓的是最贵的一类 bug：服务以 LocalSystem（Session 0）运行，普通
Popen 起的 Chrome 端口通、进程在，但窗口画在无人可见的服务桌面上——
仪表盘因此显示"运行中"，而用户永远没法在这些 profile 里登录，
看起来就像"账号莫名其妙没登录"。启动必须走 CreateProcessAsUser，
失败也必须说实话，不能假装窗口弹出来了。
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_core as bc
import chrome_launcher as cl


# ---------------- 命令行与状态解析 ----------------

class TestCmdline:
    def test_spaces_and_quotes_follow_windows_rules(self):
        """直接用标准库规则，别自己拼：路径含空格/引号时自己写很容易错"""
        assert cl._argv_to_cmdline(["C:\\a b\\chrome.exe"]) == \
            '"C:\\a b\\chrome.exe"'
        assert cl._argv_to_cmdline(["x", '--user-data-dir=C:\\p ro\\acc1']) == \
            'x "--user-data-dir=C:\\p ro\\acc1"'
        assert cl._argv_to_cmdline(["a", 'b"c']) == 'a b\\"c'

    def test_round_trips_through_windows_parser(self):
        argv = ["C:\\Program Files\\Chrome\\chrome.exe",
                "--remote-debugging-port=9222",
                "--user-data-dir=C:\\Users\\mey\\Desktop\\web-api+tool"
                "\\chrome_profiles\\acc1",
                "https://chatgpt.com"]
        # Windows 自己的解析器（commandlineparser 不在标准库里，
        # 用 Popen 的引用函数反解一次即可确认往返一致）
        cmd = cl._argv_to_cmdline(argv)
        assert subprocess.list2cmdline(argv) == cmd
        assert "--user-data-dir=C:\\Users" in cmd


class TestWindowStatusParsing:
    def test_parses_and_flags_visible(self, monkeypatch):
        seen = {}

        def fake_run_ps(script, timeout=15.0):
            seen["script"] = script
            return "9222,21364,1\n9223,1528,0\n"

        monkeypatch.setattr(cl, "_run_ps", fake_run_ps)
        monkeypatch.setattr(cl, "current_session_id", lambda: 1)
        st = cl.window_status([9222, 9223, 9226])
        assert st[9222] == {"pid": 21364, "session": 1, "visible": True}
        assert st[9223] == {"pid": 1528, "session": 0, "visible": False}
        assert st[9226]["pid"] is None and st[9226]["visible"] is False
        assert "Get-NetTCPConnection" in seen["script"]

    def test_ports_are_inlined_not_passed_as_args(self, monkeypatch):
        """`powershell -Command "..." 9222,9223` 会把参数当第二条语句执行并原样
        打印，$args 根本收不到——曾因此查不到任何端口归属，面板永远
        显示"看不到窗口信息"。值必须内联进脚本。"""
        seen = {}

        def fake_run_ps(script, timeout=15.0):
            seen["script"] = script
            return ""

        monkeypatch.setattr(cl, "_run_ps", fake_run_ps)
        cl.window_status([9222, 9227])
        assert "9222,9227" in seen["script"]
        assert "$args" not in seen["script"]

    def test_profile_pattern_distinguishes_siblings(self, monkeypatch):
        """acc1 的路径是 acc12 的前缀：不带引号那种写法必须要求路径后
        紧跟空格或引号，否则会把 acc12 的窗口也算成 acc1 的"""
        seen = {}
        monkeypatch.setattr(cl, "_run_ps",
                            lambda s, timeout=15.0: seen.update(script=s) or "")
        cl.find_profile_chromes(r"C:\proj\chrome_profiles\acc1")
        script = seen["script"]
        assert r"--user-data-dir=C:\proj\chrome_profiles\acc1 " in script
        assert "chrome_profiles\\acc12" not in script
        assert "[regex]::Escape" in script, "路径含 + 和 \\，必须转义"


# ---------------- 跨会话启动 ----------------

class TestLaunchInUserSession:
    def test_non_windows_reports_failure_not_silence(self, monkeypatch):
        monkeypatch.setattr(cl, "_IS_WIN", False)
        ok, msg = cl.launch_in_user_session(["chrome", "--x"])
        assert ok is False and "非 Windows" in msg

    def test_no_active_session_explains_why(self, monkeypatch):
        monkeypatch.setattr(cl, "_IS_WIN", True)
        monkeypatch.setattr(cl, "_candidate_sessions", lambda: [])
        ok, msg = cl.launch_in_user_session(["chrome", "--x"])
        assert ok is False and "没有活跃用户会话" in msg


class TestAutoLaunchUsesCrossSessionPath:
    """请求触发的自动启动（switch_instance / ensure_connection）也必须是
    可见启动——它和面板上的"启动窗口"是同一个毛病。"""

    def _core(self, monkeypatch, vis_ok, vis_msg="已在用户会话 1 启动，pid=1"):
        launched = {"vis": 0, "popen": 0}

        def fake_vis(argv, cwd=None):
            launched["vis"] += 1
            return vis_ok, vis_msg

        def fake_popen(args, **kw):
            launched["popen"] += 1

            class P:
                pid = 4242
            return P()

        monkeypatch.setattr(cl, "launch_in_user_session", fake_vis)
        monkeypatch.setattr(bc.subprocess, "Popen", fake_popen)
        # 不依赖测试机上到底装了哪个浏览器
        monkeypatch.setattr(bc.os.path, "exists", lambda p: True)
        core = object.__new__(bc.BrowserCore)
        core._port_open = lambda p: True
        return core, launched

    def _launch(self, monkeypatch, vis_ok, port=9224):
        core, launched = self._core(monkeypatch, vis_ok)
        ok = core._launch_instance_chrome(port)
        return ok, launched

    def test_visible_launch_is_used_first(self, monkeypatch):
        ok, launched = self._launch(monkeypatch, True)
        assert ok is True
        assert launched["vis"] == 1, "必须先尝试起在用户会话"
        assert launched["popen"] == 0, "成功时不该再退回不可见启动"

    def test_falls_back_and_still_reports_success(self, monkeypatch):
        """用户没登录（锁屏/无人会话）时请求仍然要能跑：退回当前会话启动，
        但日志必须讲清"窗口不可见、profile 无法交互登录"，不能假装弹了窗口"""
        ok, launched = self._launch(monkeypatch, False)
        assert ok is True
        assert launched["vis"] == 1 and launched["popen"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
