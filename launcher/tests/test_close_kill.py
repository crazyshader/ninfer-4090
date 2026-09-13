"""回归：启动器关闭时 ninfer-serve.exe（CUDA 进程）残留（用户报告，2026-09）。

根因（本机实测，RTX 4090 / Windows 11 / PySide6 6.11）：

1. Windows 上 QProcess.terminate()（即 TerminateProcess）对 CUDA 进程可能**静默失败**——
   进程处于 GPU 驱动临界区时 OS 会推迟其终止，5 秒宽限期结束时进程仍活着；
2. 旧关闭流程在「5 秒宽限 + taskkill 后等 3 秒」之后就宣布停止完成、直接让启动器
   退出，残留进程继续占着 23 GB 显存（用户看到的「残留 ninfer-serve.exe」）。

本文件钉死修复后的行为：

1. OS 真值原语（process_terminated / terminate_process_hard）：对无效 PID 不崩、
   对真实存活进程能终止并被 OS 确认；
2. terminate / taskkill 都失效时（模拟 CUDA 驱动态），stop_and_wait 必须进入强杀
   升级阶段（taskkill → Win32 强杀 + OS 校验），进程最终退出时正常完成收尾；
3. 升级时限用尽仍杀不死时，必须打出显著警告——宁可带警告退出也不静默残留；
4. 状态失同步（状态机说 stopped 但进程实际活着）时，停止流程仍必须执行杀进程
   （closeEvent 的门控也相应改为「状态机 OR OS 层存活」，见 TestCloseGate）。
"""

import time

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from ninfer_launcher.core.process import (
    ServerProcess,
    ServerState,
    process_terminated,
    terminate_process_hard,
)

# QProcess.start 不走 PATH 查找，必须给完整路径（System32 下的 ping.exe）
_PING = r"C:\Windows\System32\ping.exe"
_PING_ARGS = ["-n", "120", "127.0.0.1"]
_INVALID_PID = 0x7FFFFFFE  # 远超 Windows PID 上限，OpenProcess 必然失败


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication(["test"])


def _pump(app: QApplication, ms: int) -> None:
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def _make_proc(**overrides) -> ServerProcess:
    """快速构造一个可注入替身、时间尺度缩小的 ServerProcess（测试专用）。"""
    params = dict(
        vram_reader=lambda: None,  # 无显存读数 -> 降级收尾，0.05 秒即完成
        terminate_grace=0.05,
        settle_timeout=0.1,
        settle_fallback_wait=0.05,
        kill_escalation=0.4,
        kill_check_interval_ms=50,
    )
    params.update(overrides)
    return ServerProcess(**params)


class _LogSpy:
    """挂在 log_line 信号上的侦听器：stop_and_wait 期间的全部日志行。"""

    def __init__(self, proc: ServerProcess) -> None:
        self.lines: list[str] = []
        proc.log_line.connect(self._on_line)

    def _on_line(self, line) -> None:
        self.lines.append(line.text)


def _start_ping(app: QApplication, proc: ServerProcess) -> None:
    assert proc.start(_PING, _PING_ARGS)
    _pump(app, 300)
    assert proc.last_pid is not None, "启动后必须记住 PID（善后强杀靠它）"


def _cleanup(proc: ServerProcess) -> None:
    """确保本测试留下的 ping 子进程不会变成新的残留（本文件不留尾巴）。"""
    pid = proc.last_pid or proc.pid
    if not pid:
        return
    terminate_process_hard(pid)
    deadline = time.time() + 5
    while not process_terminated(pid) and time.time() < deadline:  # 等到 OS 确认消失
        time.sleep(0.05)
    proc._proc.waitForFinished(3000)


class TestOsTruthPrimitives:
    """OS 层进程存活探测 / 强杀原语。"""

    def test_process_terminated_rejects_invalid_pid(self):
        assert process_terminated(None) is False
        assert process_terminated(0) is False
        # 无效/不存在的 PID：OpenProcess 必然失败 => 该进程「已不在」=> True
        # （区别于 None/0 哨兵：那表示「没有 PID 可查」，返回 False）
        assert process_terminated(_INVALID_PID) is True

    def test_terminate_process_hard_rejects_invalid_pid(self):
        ok, message = terminate_process_hard(0)
        assert ok is False
        assert "PID" in message
        ok, message = terminate_process_hard(_INVALID_PID)
        assert ok is False
        assert "句柄" in message

    def test_hard_kill_really_terminates_live_process(self):
        """真进程闭环：OpenProcess 探测存活 -> 强杀 -> OS 确认消失。"""
        app = _qapp()
        proc = _make_proc()
        _start_ping(app, proc)
        pid = proc.last_pid
        assert not process_terminated(pid), "真活着：OpenProcess 能探测到 => 未终止"
        ok, message = terminate_process_hard(pid)
        assert ok, message
        deadline = time.time() + 5
        while not process_terminated(pid) and time.time() < deadline:
            app.processEvents()
            time.sleep(0.05)
        assert process_terminated(pid), "强杀后 OS 必须确认进程消失"
        _cleanup(proc)


class TestStopEscalation:
    """停止流程的强杀升级：terminate / taskkill 都失效时的兜底行为。"""

    def test_stubborn_process_escalates_and_warns_at_deadline(self):
        """进程始终拒绝终止（模拟 CUDA 驱动态）：必须升级强杀，时限用尽后显著告警。"""
        app = _qapp()
        killer_calls: list[int] = []
        hammer_calls: list[int] = []
        proc = _make_proc(
            killer=lambda pid: (killer_calls.append(pid), (True, f"假 taskkill {pid}"))[1],
            hard_terminator=lambda pid: (hammer_calls.append(pid), (True, f"假强杀 {pid}"))[1],
            alive_check=lambda pid: True,  # 永远「杀不死」
        )
        _start_ping(app, proc)
        spy = _LogSpy(proc)

        result = proc.stop_and_wait()

        assert proc.state is ServerState.STOPPED
        assert result is None, "进程仍「存活」：不得做显存回落等待"
        assert killer_calls, "terminate 失效后必须回落 taskkill"
        assert hammer_calls, "taskkill 失效后必须升级强杀"
        assert any("强杀" in line and "仍未退出" in line for line in spy.lines), (
            "升级时限用尽必须打显著警告，不能静默残留"
        )
        _cleanup(proc)

    def test_eventually_dying_process_completes_normally(self):
        """前几轮拒绝终止（模拟 CUDA 驱动态）、随后进程真死：升级流程正常收尾、无警告。"""
        app = _qapp()
        killer_calls: list[int] = []
        hammer_calls: list[int] = []
        # 硬杀用假替身（不真杀，让 alive_check 成为唯一真值来源），真实 ping 由 _cleanup 收尾
        t0: list[float | None] = [None]

        def fake_alive(pid):
            """前 0.2 秒「驱动态拒绝终止」，之后「驱动平息、进程真死」。"""
            if t0[0] is None:
                t0[0] = time.monotonic()
            return time.monotonic() - t0[0] < 0.2

        proc = _make_proc(
            killer=lambda pid: (killer_calls.append(pid), (True, f"假 taskkill {pid}"))[1],
            hard_terminator=lambda pid: (hammer_calls.append(pid), (True, f"假强杀 {pid}"))[1],
            alive_check=fake_alive,
        )
        _start_ping(app, proc)
        spy = _LogSpy(proc)

        result = proc.stop_and_wait()

        assert proc.state is ServerState.STOPPED
        assert killer_calls, "terminate 失效后必须回落 taskkill"
        assert hammer_calls, "taskkill 失效后必须升级强杀（多轮直到进程真死）"
        assert result is not None, "进程真死后必须完成显存回落等待"
        assert not any("强杀后服务器进程" in line and "仍未退出" in line for line in spy.lines), (
            "进程已真死：不应出现「强杀后仍未退出」警告"
        )
        _cleanup(proc)

    def test_desync_stopped_state_still_triggers_kill(self):
        """状态失同步（状态机说 stopped、进程实际活着）：stop_and_wait 不得提前返回。"""
        app = _qapp()
        killer_calls: list[int] = []
        hammer_calls: list[int] = []
        proc = _make_proc(
            killer=lambda pid: (killer_calls.append(pid), (True, "假 taskkill"))[1],
            hard_terminator=lambda pid: (hammer_calls.append(pid), (True, "假强杀"))[1],
            alive_check=lambda pid: True,
        )
        _start_ping(app, proc)
        proc._state = ServerState.STOPPED  # 人为制造失同步
        spy = _LogSpy(proc)

        result = proc.stop_and_wait()

        assert killer_calls, "失同步场景不得被「状态=stopped」短路（旧实现会在这里直接漏进程）"
        assert hammer_calls
        assert result is None
        assert any("失同步" in line for line in spy.lines)
        assert any("仍未退出" in line for line in spy.lines)
        _cleanup(proc)


@pytest.fixture
def _stub_confirm_yes(monkeypatch):
    """offscreen 下不真跑模态框：关闭确认框一律点「是」。"""
    calls = []

    def fake_question(*args, **kwargs):
        calls.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    return calls


@pytest.fixture
def window(monkeypatch, tmp_path):
    """配置根指到临时目录，绝不碰真实 settings.json / presets（同 test_show_command）。"""
    from ninfer_launcher.core import config as config_mod

    monkeypatch.setattr(config_mod, "resolve_config_root", lambda probe=None: tmp_path)
    from ninfer_launcher.ui.main_window import MainWindow

    win = MainWindow()
    yield win
    win.close()


class TestCloseGate:
    """closeEvent 门控：状态失同步（stopped 但进程活着）也必须触发停止流程。"""

    def test_close_stops_desynced_alive_process(self, window, _stub_confirm_yes, monkeypatch):
        proc = window._process
        proc._state = ServerState.STOPPED
        proc._last_pid = 12345
        monkeypatch.setattr(proc, "is_alive", lambda: True)
        killer_calls: list[int] = []
        proc._killer = lambda pid: (killer_calls.append(pid), (True, "假 taskkill"))[1]
        proc._hard_terminator = lambda pid: (True, "假强杀")
        proc._kill_escalation = 0.1
        proc._kill_check_interval_ms = 50

        window.close()

        assert killer_calls, "失同步场景下关闭仍必须走杀进程流程（旧实现会在这里直接漏进程）"
        assert _stub_confirm_yes, "进程仍活着时关闭必须询问确认"

    def test_close_without_live_process_is_silent(self, window, _stub_confirm_yes, monkeypatch):
        proc = window._process
        proc._state = ServerState.STOPPED
        monkeypatch.setattr(proc, "is_alive", lambda: False)
        killer_calls: list[int] = []
        proc._killer = lambda pid: (killer_calls.append(pid), (True, "x"))[1]

        window.close()

        assert not _stub_confirm_yes, "服务器未在运行：关闭不应弹确认框"
        assert not killer_calls
