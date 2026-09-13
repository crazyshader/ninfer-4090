"""外部服务实时对账（GUI 与 CLI 共享真值）测试。

背景（2026-09）：用户用 ninfer-launcher-cli 启动服务后，GUI 主窗口不知情，
四个控制按钮停留在停止态（启动可点、停止 / WebUI 点不了），无法管理已在运行的服务。
修复：GUI 在自身进程处于停止态时每秒对账一次（真值顺序与 CLI status 完全一致：
/health 探测 > OS 层进程存活 > PID 登记表），把面板同步到外部服务真实状态；
点「停止」时按 PID 走 OS 层升级链（ExternalProcessStopper）停掉外部实例。

本文件钉死：
1. 共享判定表 judge_service_state（GUI / CLI 唯一事实源）四象限；
2. observe_service_state 纯只读观测（端口回退、owner 归因、陈旧登记→pid None）；
3. GUI 对账：识别外部 RUNNING→面板切 RUNNING 矩阵；外部消失→切回 STOPPED；无服务→不动；
   自身进程状态机一动→清掉陈旧外部观测；非停止态→不清面板也不发起观测；
4. GUI「停止」路由：外部实例走 ExternalProcessStopper，绝不动 self._process 状态机；
5. ExternalProcessStopper 升级链：taskkill 后死亡→收尾成功；杀不死→时限到显著告警；
   发起前已死→直接收尾且不发杀命令；重复 start() 被忽略。

纪律：与 test_cli / test_close_kill 一致——进程 / 探测 / 显存 / 时钟全部注入假件，
配置根 monkeypatch 到 tmp_path，绝不碰真实进程 / 端口 / GPU / 配置。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal

from ninfer_launcher.core.health_probe import HealthResult, HealthState
from ninfer_launcher.core.pid_file import PidEntry
from ninfer_launcher.core.process import (
    ExternalProcessStopper,
    ServerState,
)
from ninfer_launcher.core.service_status import (
    ServiceObservation,
    judge_service_state,
    observe_service_state,
)

# --- 共享常量 / 假件 ---------------------------------------------------------

READY = HealthResult(HealthState.READY, "ready")
LOADING = HealthResult(HealthState.NOT_READY, "loading")
UNREACHABLE = HealthResult(HealthState.NOT_READY, "连接失败: 测试")

LIVE_PID = 99123
EXT_PID = 987654


def mk_entry(pid: int, port: int, owner: str) -> PidEntry:
    """构造一条完整的 PID 登记表（11 字段，schema=1）。"""
    return PidEntry(
        schema=1, pid=pid, port=port, exe="ninfer-serve.exe", args=(),
        model=None, preset=None, owner=owner, started_at=0.0, log_path=None,
    )


class FakeClock:
    """假时钟：测试手动推进 t，升级链 / 显存回落的时间判定都走它，不耗真实时间。"""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _drive(stopper: ExternalProcessStopper, clock: FakeClock, step: float, ticks: int = 300):
    """手动驱动 stopper 的定时器槽（不依赖真实 Qt 计时），推进假时钟直到 finished。

    start() 后把真实定时器停掉，按当前阶段（kill / settle）逐 tick 调对应槽，
    每 tick 先推进假时钟——等价于真实定时器周期性触发，但完全确定、不耗真实时间。
    返回 finished 信号收到的 (ok, message) 列表。
    """
    finished: list[tuple[bool, str]] = []
    stopper.finished.connect(lambda ok, m: finished.append((ok, m)))
    stopper.start()
    stopper._kill_timer.stop()
    stopper._settle_timer.stop()
    phase = "kill" if stopper._kill_deadline > 0 else "settle"
    for _ in range(ticks):
        if stopper._finished_flag:
            break
        clock.t += step
        if phase == "kill":
            stopper._on_kill_tick()
            if stopper._settle_timer.isActive() or stopper._finished_flag:
                phase = "settle"
        else:
            stopper._on_settle_tick()
    return finished


def _mk_stopper(*, alive_check, killer, hard_terminator, kill_escalation, clock):
    """构造一个 I/O 全假件、时间尺度缩小的 ExternalProcessStopper（测试专用）。

    vram_reader=None → 显存回落走降级路径（fallback_wait 即到即收尾），
    避免依赖真实显存读数来源。
    """
    return ExternalProcessStopper(
        LIVE_PID,
        vram_reader=None,
        killer=killer,
        hard_terminator=hard_terminator,
        alive_check=alive_check,
        kill_escalation=kill_escalation,
        kill_check_interval_ms=10,
        settle_timeout=1.0,
        settle_interval=0.1,
        settle_fallback_wait=0.2,
        settle_drop_bytes=128 * 1024 * 1024,
        clock=clock,
    )

# ---------------------------------------------------------------------------
# 1. 共享判定表 judge_service_state
# ---------------------------------------------------------------------------

class TestJudgeServiceState:
    def test_ready_live_cli(self):
        state, owner = judge_service_state(READY, "cli", True)
        assert state is ServerState.RUNNING
        assert owner == "cli"

    def test_ready_no_entry_owner_external(self):
        state, owner = judge_service_state(READY, None, False)
        assert state is ServerState.RUNNING
        assert owner == "external"

    def test_ready_live_external(self):
        state, owner = judge_service_state(READY, "external", True)
        assert state is ServerState.RUNNING
        assert owner == "external"

    def test_loading_live_starting(self):
        state, owner = judge_service_state(LOADING, "cli", True)
        assert state is ServerState.STARTING
        assert owner == "cli"

    def test_unreachable_but_alive_starting(self):
        # 503/连接失败但实例存活（还在加载）→ STARTING，不是 STOPPED
        state, owner = judge_service_state(UNREACHABLE, "external", True)
        assert state is ServerState.STARTING
        assert owner == "external"

    def test_dead_no_entry_stopped(self):
        state, owner = judge_service_state(UNREACHABLE, None, False)
        assert state is ServerState.STOPPED
        assert owner is None

    def test_stale_entry_stopped(self):
        # 登记表在但进程已死（陈旧）→ STOPPED，owner 归 None
        state, owner = judge_service_state(UNREACHABLE, "cli", False)
        assert state is ServerState.STOPPED
        assert owner is None


# ---------------------------------------------------------------------------
# 2. observe_service_state（纯只读观测）
# ---------------------------------------------------------------------------

class TestObserveServiceState:
    def test_ready_live_cli_uses_entry_port_and_pid(self, tmp_path):
        obs = observe_service_state(
            host="127.0.0.1", port=8080, root=tmp_path,
            probe=lambda h, p: READY,
            terminated_check=lambda pid: pid != LIVE_PID,  # LIVE_PID 存活
            entry_reader=lambda r: mk_entry(LIVE_PID, 18080, "cli"),
        )
        assert obs.state is ServerState.RUNNING
        assert obs.port == 18080  # 优先登记表端口
        assert obs.pid == LIVE_PID
        assert obs.owner == "cli"
        assert obs.health is READY

    def test_ready_no_entry_port_fallback_owner_external(self, tmp_path):
        obs = observe_service_state(
            host="127.0.0.1", port=8080, root=tmp_path,
            probe=lambda h, p: READY,
            terminated_check=lambda pid: True,  # 无进程
            entry_reader=lambda r: None,
        )
        assert obs.state is ServerState.RUNNING
        assert obs.port == 8080  # 回退到调用方端口
        assert obs.pid is None
        assert obs.owner == "external"

    def test_loading_live_external_starting(self, tmp_path):
        obs = observe_service_state(
            host="127.0.0.1", port=8080, root=tmp_path,
            probe=lambda h, p: LOADING,
            terminated_check=lambda pid: pid != LIVE_PID,
            entry_reader=lambda r: mk_entry(LIVE_PID, 8080, "external"),
        )
        assert obs.state is ServerState.STARTING
        assert obs.owner == "external"
        assert obs.pid == LIVE_PID

    def test_dead_stale_entry_stopped_pid_none(self, tmp_path):
        obs = observe_service_state(
            host="127.0.0.1", port=8080, root=tmp_path,
            probe=lambda h, p: UNREACHABLE,
            terminated_check=lambda pid: True,  # 登记表指向的进程已死
            entry_reader=lambda r: mk_entry(LIVE_PID, 8080, "cli"),
        )
        assert obs.state is ServerState.STOPPED
        assert obs.pid is None  # 陈旧：不给出可停止的 PID
        assert obs.owner is None

    def test_observe_is_pure_reader_calls_entry_reader_once(self, tmp_path):
        # 观测是纯只读观察者：对登记表只读一次，不校验删除 / 不改文件
        calls = {"n": 0}

        def reader(root):
            calls["n"] += 1
            return mk_entry(LIVE_PID, 18080, "cli")

        observe_service_state(
            host="127.0.0.1", port=8080, root=tmp_path,
            probe=lambda h, p: READY,
            terminated_check=lambda pid: pid != LIVE_PID,
            entry_reader=reader,
        )
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 3. GUI 对账（MainWindow._reconcile_external_service）
# ---------------------------------------------------------------------------

@pytest.fixture
def window(monkeypatch, tmp_path):
    """配置根指到临时目录，绝不碰真实 settings.json / presets（同 test_close_kill）。"""
    from ninfer_launcher.core import config as config_mod

    monkeypatch.setattr(config_mod, "resolve_config_root", lambda probe=None: tmp_path)
    from ninfer_launcher.ui.main_window import MainWindow

    win = MainWindow()
    yield win
    win.close()


def _obs(state, port=8080, pid=None, owner=None):
    return ServiceObservation(
        state=state, port=port, pid=pid, owner=owner,
        health=READY if state is ServerState.RUNNING else None,
    )


class TestGuiReconciliation:
    def _reconcile(self, win, monkeypatch, obs):
        import ninfer_launcher.ui.main_window as mw

        win._ext_timer.stop()  # 测试里手动对账，避免真实 1s 定时器干扰
        monkeypatch.setattr(mw, "observe_service_state", lambda **kw: obs)
        win._process._state = ServerState.STOPPED  # GUI 自身进程处于停止态
        win._reconcile_external_service()

    def test_detects_external_running_panel_switches(self, window, monkeypatch):
        self._reconcile(window, monkeypatch, _obs(ServerState.RUNNING, 18080, EXT_PID, "cli"))
        panel = window._control
        assert window._external_state is ServerState.RUNNING
        assert window._external_pid == EXT_PID
        assert window._external_port == 18080
        # 面板切到 RUNNING 矩阵：启动禁用、停止 / WebUI / 显示命令 可用
        assert not panel._btn_start.isEnabled()
        assert panel._btn_stop.isEnabled()
        assert panel._btn_webui.isEnabled()
        assert panel._btn_cmd.isEnabled()

    def test_external_starting_panel_starting_matrix(self, window, monkeypatch):
        self._reconcile(window, monkeypatch, _obs(ServerState.STARTING, 18080, EXT_PID, "external"))
        panel = window._control
        assert window._external_state is ServerState.STARTING
        assert not panel._btn_start.isEnabled()
        assert panel._btn_stop.isEnabled()
        assert not panel._btn_webui.isEnabled()

    def test_no_external_service_leaves_panel_stopped(self, window, monkeypatch):
        # 初始面板即停止态矩阵；对账无服务 → 保持不变
        panel = window._control
        assert panel._btn_start.isEnabled()
        assert not panel._btn_stop.isEnabled()
        self._reconcile(window, monkeypatch, _obs(ServerState.STOPPED))
        assert window._external_state is None
        assert panel._btn_start.isEnabled()
        assert not panel._btn_stop.isEnabled()
        assert not panel._btn_webui.isEnabled()

    def test_reverts_to_stopped_when_service_goes_away(self, window, monkeypatch):
        # 先识别到外部 RUNNING，再对账发现它没了 → 面板切回停止态矩阵
        self._reconcile(window, monkeypatch, _obs(ServerState.RUNNING, 18080, EXT_PID, "cli"))
        assert window._control._btn_stop.isEnabled()
        self._reconcile(window, monkeypatch, _obs(ServerState.STOPPED))
        panel = window._control
        assert window._external_state is None
        assert panel._btn_start.isEnabled()
        assert not panel._btn_stop.isEnabled()
        assert not panel._btn_webui.isEnabled()

    def test_process_state_change_clears_stale_external(self, window, monkeypatch):
        # 面板显示外部 RUNNING 后，GUI 自身进程状态机一动（启动）→ 清掉外部观测，
        # 面板真值交还给进程层（本行 apply_state 已把面板切到进程状态）。
        self._reconcile(window, monkeypatch, _obs(ServerState.RUNNING, 18080, EXT_PID, "cli"))
        assert window._external_state is ServerState.RUNNING
        # 隔离健康轮询：本用例只验证「进程状态变化→清外部观测」逻辑，不真实启动周期
        # 探测（那会发真实 /health 请求、碰网络），把 start/stop 中性化即可。
        monkeypatch.setattr(window._health, "start", lambda: None)
        monkeypatch.setattr(window._health, "stop", lambda: None)
        window._process._state = ServerState.STARTING
        window._on_state_changed(ServerState.STARTING)
        assert window._external_state is None
        # 面板已按进程 STARTING 渲染（停止可用、WebUI 不可用）
        assert window._control._btn_stop.isEnabled()
        assert not window._control._btn_webui.isEnabled()
        # 复位：本用例人为把进程状态机推离停止态并启动了健康轮询，断言完成后推回停止态，
        # 避免收尾 closeEvent 误判「有进程在跑」而弹停止确认 / 触发停止链。
        window._health.stop()
        window._process._state = ServerState.STOPPED

    def test_reconcile_skipped_when_process_not_stopped(self, window, monkeypatch):
        # 自身进程非停止态：面板由进程状态机主导，清掉陈旧外部观测、不发起观测。
        import ninfer_launcher.ui.main_window as mw

        win = window
        win._ext_timer.stop()
        seen = {}

        def fake_obs(**kw):
            seen["called"] = True
            return _obs(ServerState.RUNNING, 18080, EXT_PID, "cli")

        monkeypatch.setattr(mw, "observe_service_state", fake_obs)
        win._process._state = ServerState.RUNNING  # GUI 自己在跑
        win._external_state = ServerState.RUNNING
        win._external_pid = EXT_PID
        win._reconcile_external_service()
        assert "called" not in seen  # 非停止态不发起观测
        assert win._external_state is None  # 但陈旧外部观测被清掉
        # 复位：为测试把进程状态机推高到非停止态，断言后推回停止态，保持 closeEvent 干净。
        win._process._state = ServerState.STOPPED


# ---------------------------------------------------------------------------
# 4. GUI「停止」路由：外部实例走 ExternalProcessStopper
# ---------------------------------------------------------------------------

class FakeStopper(QObject):
    """ExternalProcessStopper 替身：记录构造参数与 start()，可手动 emit finished。"""

    message = Signal(str)
    finished = Signal(bool, str)
    created: list["FakeStopper"] = []

    def __init__(self, pid, vram_reader=None, parent=None, **kw):
        super().__init__(parent)
        self.pid = pid
        self.vram_reader = vram_reader
        self.started = False
        FakeStopper.created.append(self)

    def start(self):
        self.started = True


class TestGuiStopRouting:
    def test_stop_routes_to_external_stopper(self, window, monkeypatch):
        import ninfer_launcher.ui.main_window as mw

        FakeStopper.created.clear()
        # 让窗口「看到」一个 CLI 拉起的 RUNNING 外部服务
        window._external_state = ServerState.RUNNING
        window._external_pid = EXT_PID
        window._external_port = 18080
        window._external_owner = "cli"

        monkeypatch.setattr(mw, "ExternalProcessStopper", FakeStopper)
        window._on_stop()

        assert len(FakeStopper.created) == 1
        stopper = FakeStopper.created[0]
        assert stopper.pid == EXT_PID
        assert stopper.started is True
        assert window._external_stop_in_progress is True
        # GUI 自身进程状态机不受影响（仍停止）
        assert window._process.state is ServerState.STOPPED

    def test_stop_on_own_process_uses_process_stop(self, window, monkeypatch):
        # 自身进程在运行：停止走常规 ServerProcess.stop()，不碰外部 stopper
        FakeStopper.created.clear()
        stopped = []
        window._process.stop = lambda: stopped.append(True)
        window._process._state = ServerState.RUNNING
        window._on_stop()
        assert stopped == [True]
        assert FakeStopper.created == []
        # 复位：本用例人为把进程状态机推到 RUNNING，且 stop 被替换成只记录的替身
        # （状态机不会真的退出运行态）。不推回停止态，收尾 closeEvent 会判定「有进程
        # 在跑」而弹出模态确认框——本文件的 window 夹具没有替换 QMessageBox，模态框
        # 在无人点击的测试进程里永久阻塞（现由 conftest 契约 5 的护栏兜底为明确失败）。
        window._process._state = ServerState.STOPPED

    def test_stop_with_nothing_running_is_silent(self, window, monkeypatch):
        FakeStopper.created.clear()
        window._process.stop = lambda: pytest.fail("不该走自身进程停止")
        window._process._state = ServerState.STOPPED
        window._external_state = None
        window._on_stop()  # 两者皆停 → 静默返回
        assert FakeStopper.created == []


# ---------------------------------------------------------------------------
# 5. ExternalProcessStopper 升级链
# ---------------------------------------------------------------------------

class TestExternalProcessStopper:
    def test_taskkill_then_dead_settles_ok(self):
        # taskkill 一锤定音（进程随之死亡）→ 直接显存回落收尾，finished(ok=True)
        state = {"alive": True}

        def killer(pid):
            state["alive"] = False
            return (True, "假 taskkill")

        def alive_check(pid):
            return not state["alive"]  # True = 已死

        clock = FakeClock()
        stopper = _mk_stopper(
            alive_check=alive_check, killer=killer,
            hard_terminator=lambda pid: (True, "假强杀"),
            kill_escalation=10.0, clock=clock,
        )
        results = _drive(stopper, clock, step=0.25)
        stopper.deleteLater()
        assert results and results[0][0] is True

    def test_stubborn_process_escalates_then_times_out(self):
        # taskkill / 强杀都杀不死：每轮锤强杀，升级时限到 → finished(ok=False) 显著告警
        hard_calls: list[int] = []

        def killer(pid):
            return (True, "假 taskkill（无效）")

        def hard(pid):
            hard_calls.append(pid)
            return (True, "假强杀（无效）")

        clock = FakeClock()
        stopper = _mk_stopper(
            alive_check=lambda pid: False,  # 永远存活（True=已死 的取反）
            killer=killer, hard_terminator=hard,
            kill_escalation=0.5, clock=clock,
        )
        results = _drive(stopper, clock, step=0.25)
        stopper.deleteLater()
        assert results and results[0][0] is False
        assert "任务管理器" in results[0][1]
        assert hard_calls, "升级阶段必须真的发起强杀"

    def test_already_dead_before_start_settles_without_kill(self):
        # 发起前进程已自行退出：不发任何杀命令，直接收尾
        state = {"alive": False}
        kill_calls: list[int] = []

        def killer(pid):
            kill_calls.append(pid)
            return (True, "不该被调用")

        clock = FakeClock()
        stopper = _mk_stopper(
            alive_check=lambda pid: not state["alive"],  # 已死
            killer=killer,
            hard_terminator=lambda pid: (True, "x"),
            kill_escalation=10.0, clock=clock,
        )
        results = _drive(stopper, clock, step=0.25)
        stopper.deleteLater()
        assert results and results[0][0] is True
        assert kill_calls == [], "进程已死时不得再发杀命令"

    def test_reentrant_start_is_ignored(self):
        # 重复 start() 被忽略（不会二次杀 / 二次收尾）
        stopper = _mk_stopper(
            alive_check=lambda pid: True,  # 已死
            killer=lambda pid: (True, "x"),
            hard_terminator=lambda pid: (True, "x"),
            kill_escalation=10.0, clock=FakeClock(),
        )
        assert stopper.start() is True
        assert stopper.start() is False
        stopper._kill_timer.stop()
        stopper._settle_timer.stop()
        stopper.deleteLater()

