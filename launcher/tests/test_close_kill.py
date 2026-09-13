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


class TestIsAliveSemantics:
    """回归钉死 is_alive() 的语义：必须表示「子进程是否存活」（True=活）。

    2026-09 修复 process_terminated 返回值（True=已死）后，ServerProcess.is_alive()
    的 pid 非 None 分支一度漏改取反，直接 return self._terminated_check(pid)，
    导致「进程活着时 is_alive() 返回 False」。_begin_stop 的 `if not self.is_alive()`
    据此走「子进程已不在运行」分支，跳过全部杀进程步骤（terminate→taskkill→强杀），
    GUI 点「停止」后 ninfer-serve.exe 永远不会被结束（用户报告，2026-09）。
    本测试用注入的 terminated_check 验证 is_alive 在两态下的真实返回值，防止再次回归。
    """

    def test_alive_process_returns_true(self):
        _qapp()
        # terminated_check 语义 True=已死；返回 False 即「未终止=存活」
        proc = _make_proc(terminated_check=lambda pid: False)
        proc._last_pid = 0x1234
        assert proc.is_alive() is True, "进程存活（terminated_check=False）时 is_alive 必须为 True"

    def test_terminated_process_returns_false(self):
        _qapp()
        # terminated_check 返回 True 即「已终止=不在运行」
        proc = _make_proc(terminated_check=lambda pid: True)
        proc._last_pid = 0x1234
        assert proc.is_alive() is False, "进程已死（terminated_check=True）时 is_alive 必须为 False"

    def test_no_pid_falls_back_to_qt_state(self):
        _qapp()
        # 无 PID 可查时退回 Qt 缓存：未启动的 QProcess 处于 NotRunning → is_alive=False
        proc = _make_proc()
        proc._last_pid = None
        assert proc.is_alive() is False


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
            terminated_check=lambda pid: False,  # 永远「不终止」（terminated 语义 False=未终止=一直活着/杀不死）
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
        # 硬杀用假替身（不真杀，让 terminated_check 成为唯一真值来源），真实 ping 由 _cleanup 收尾
        t0: list[float | None] = [None]

        def fake_terminated(pid):
            """terminated 语义（True=已死）：前 0.2 秒「驱动态拒绝终止」（未终止=False），
            之后「驱动平息、进程真死」（已终止=True）。"""
            if t0[0] is None:
                t0[0] = time.monotonic()
            return time.monotonic() - t0[0] >= 0.2

        proc = _make_proc(
            killer=lambda pid: (killer_calls.append(pid), (True, f"假 taskkill {pid}"))[1],
            hard_terminator=lambda pid: (hammer_calls.append(pid), (True, f"假强杀 {pid}"))[1],
            terminated_check=fake_terminated,
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
            terminated_check=lambda pid: False,
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
    # 收尾清理：清掉外部服务观测与残留 stopper，让这里的 close() 不再弹确认框
    # （测试进程无人点模态框，conftest 护栏会把它变成明确失败）。
    stopper = win._external_stopper
    if stopper is not None:
        for _attr in ("_kill_timer", "_settle_timer"):
            _timer = getattr(stopper, _attr, None)
            if _timer is not None:
                _timer.stop()
    win._external_stopper = None
    win._external_stop_in_progress = False
    win._external_state = None
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


@pytest.fixture
def _stub_confirm_no(monkeypatch):
    """offscreen 下不真跑模态框：关闭确认框一律点「否」。"""
    calls = []

    def fake_question(*args, **kwargs):
        calls.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    return calls


class TestCloseGateExternal:
    """closeEvent 门控的外部服务分支（docs/03-close-external-service-leak-fix.md）。

    回归目标：面板显示外部服务在运行（自身进程 STOPPED）时直接关窗，旧实现会跳过
    全部停止逻辑、把外部 ninfer-serve 遗弃（残留 ~23 GB 显存）。现在必须弹确认、
    确认后同步杀掉外部实例再放行。
    """

    def test_close_stops_external_service_with_pid(self, window, _stub_confirm_yes, monkeypatch):
        proc = window._process
        proc._state = ServerState.STOPPED
        monkeypatch.setattr(proc, "is_alive", lambda: False)
        # 模拟对账识别出的外部 RUNNING 实例（有 PID 登记表）
        window._external_state = ServerState.RUNNING
        window._external_pid = 424242
        window._external_port = 8080
        window._external_owner = "cli"
        killer_calls: list[int] = []
        hammer_calls: list[int] = []
        checks: list[int | None] = []
        # OS 真值替身：taskkill 后仍存活（驱动态），第一轮强杀后确认死亡
        monkeypatch.setattr(
            "ninfer_launcher.ui.main_window.stop_external_sync",
            lambda pid, on_message=None: (
                killer_calls.append(pid),
                on_message(f"假 taskkill {pid}"),
                checks.append(pid),
                hammer_calls.append(pid),
                on_message("假强杀"),
                checks.append(pid),
                True,
            )[-1],
        )

        window.close()

        assert killer_calls == [424242], "外部实例必须在关闭时被同步停止"
        assert hammer_calls == [424242], "taskkill 失效时必须升级强杀"
        assert len(checks) >= 2, "必须以 OS 真值校验到进程消失"
        assert _stub_confirm_yes, "外部服务在运行时关闭必须询问确认"
        assert not window.isVisible(), "确认并停掉外部服务后窗口必须真正关闭"

    def test_close_external_without_pid_only_logs_hint(self, window, _stub_confirm_yes, monkeypatch):
        proc = window._process
        proc._state = ServerState.STOPPED
        monkeypatch.setattr(proc, "is_alive", lambda: False)
        # /health 通但无 PID 登记表：无法定位进程
        window._external_state = ServerState.RUNNING
        window._external_pid = None
        window._external_port = 8080
        window._external_owner = "external"
        killer_calls: list[int] = []
        hammer_calls: list[int] = []
        monkeypatch.setattr(
            "ninfer_launcher.ui.main_window.stop_external_sync",
            lambda pid, on_message=None: (killer_calls.append(pid), True)[-1],
        )

        window.close()

        assert not killer_calls, "无 PID 不得代杀（与 _stop_external 行为一致）"
        assert not hammer_calls
        log_text = window._log.text()
        assert "无法定位其进程" in log_text, "无 PID 时必须落提示日志指引用户手动处置"
        assert not window.isVisible(), "无 PID 的外部服务不得阻断关闭"

    def test_close_external_running_reply_no_ignores(self, window, _stub_confirm_no, monkeypatch):
        proc = window._process
        proc._state = ServerState.STOPPED
        monkeypatch.setattr(proc, "is_alive", lambda: False)
        window._external_state = ServerState.RUNNING
        window._external_pid = 424242
        window._external_owner = "cli"
        killer_calls: list[int] = []
        monkeypatch.setattr(
            "ninfer_launcher.ui.main_window.stop_external_sync",
            lambda pid, on_message=None: (killer_calls.append(pid), True)[-1],
        )

        window.show()  # 先让窗口可见：offscreen 下从未 show 过的窗口 isVisible() 恒为 False
        window.close()

        assert not killer_calls, "用户拒绝退出时不得触发任何停止流程"
        assert window.isVisible(), "回复「否」必须取消关闭（event.ignore）"

