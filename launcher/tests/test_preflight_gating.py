"""显存预检与「启动」按钮门控的 MainWindow 集成测试（docs/02-vram-preflight-design.md §9.4）。

验收标准落地（预检改为知情提示、不再硬门控启动）：
- 显存不足 → 「启动」仍可用 + 面板红色「显存不足」+ 进程清单（排除 ninfer 自身与系统关键进程）；
- 显存充足 → 「启动」可用 + 面板绿色「显存充足」；
- 读数变化（用户退出占显存程序）→ 下一轮预检面板从红转绿；
- 无法预检（读不到显存）→ 不阻断启动，降级为日志告警；
- 启动路径（_on_start）里再算一次预检刷新面板，显存不足不弹框、照常启动；
- 日志权重 100% 行 → weight_bytes_cache 回填并落盘 settings.json。

纪律：假 MonitorService（固定快照 + 假 NVML 进程模块）整段替换真实读数来源，
配置根 monkeypatch 到 tmp_path，端口探测 stub 成 FREE——测试全程不碰真实 GPU /
真实端口 / 真实配置目录。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ninfer_launcher.core.monitor import GpuSnapshot, MonitorSource, SystemSnapshot
from ninfer_launcher.core.process import ServerState
from ninfer_launcher.ui import main_window as main_window_module
from ninfer_launcher.ui.main_window import MainWindow

GIB = 1024 ** 3
MIB = 1024 ** 2


# --- 假件 ------------------------------------------------------------------

class FakeProcessNvml:
    """假 NVML 进程接口：进程清单固定为 chrome(10G) / Code(4G) / dwm(1G,受保护)。"""

    def __init__(self) -> None:
        self._compute = [
            SimpleNamespace(pid=101, usedGpuMemory=10 * GIB),
            SimpleNamespace(pid=102, usedGpuMemory=4 * GIB),
        ]
        self._graphics = [
            SimpleNamespace(pid=103, usedGpuMemory=1 * GIB),  # dwm.exe（graphics）
        ]

    def nvmlInit(self) -> None:
        pass

    def nvmlDeviceGetHandleByIndex(self, index: int) -> int:
        return index

    def nvmlDeviceGetComputeRunningProcesses(self, handle):
        return self._compute

    def nvmlDeviceGetGraphicsRunningProcesses(self, handle):
        return self._graphics


class FakePsutil:
    """假 psutil：chrome / Code / dwm 三个名字，其余进程查不到。"""

    def Process(self, pid: int):
        names = {101: "chrome.exe", 102: "Code.exe", 103: "dwm.exe"}

        class _P:
            def __init__(self, pid) -> None:
                self.pid = pid

            def name(self) -> str:
                if self.pid not in names:
                    raise RuntimeError("no such process")
                return names[self.pid]

            def exe(self) -> str:
                raise FileNotFoundError("no exe")

        return _P(pid)


class FakePreflightMonitor:
    """固定快照监视器：快照可被测试改写（模拟「用户退程序后显存释放」）。

    psutil_module 走 main_window 的注入位（getattr 回退自动 import）：不注入假件时
    进程名会走真实 psutil 查到「PID <pid>」，受保护进程名匹配就失效了。
    """

    def __init__(self, snapshot: SystemSnapshot, nvml=FakeProcessNvml()) -> None:
        self.snapshot = snapshot
        self.nvml_module = nvml
        self.psutil_module = FakePsutil()
        self.poll_calls = 0

    def poll(self) -> SystemSnapshot:
        self.poll_calls += 1
        return self.snapshot

    def shutdown(self) -> None:
        pass


def _snapshot(free_gib: float, total_gib: float = 24.0) -> SystemSnapshot:
    total = int(total_gib * GIB)
    used = int(total - free_gib * GIB)
    gpu = GpuSnapshot(
        index=0,
        name="NVIDIA GeForce RTX 4090",
        mem_used_bytes=used,
        mem_total_bytes=total,
        sm_clock_mhz=2520,
        temperature_c=45,
        power_draw_w=120.0,
        power_limit_w=450.0,
    )
    return SystemSnapshot(
        source=MonitorSource.NVML,
        gpus=(gpu,),
        mem_used_bytes=used,
        mem_total_bytes=total,
        cpu_percent=10.0,
    )


def _make_window(monkeypatch, tmp_path, free_gib: float) -> MainWindow:
    """构造一个监视来源全假件、配置根落 tmp_path 的 MainWindow。"""
    from ninfer_launcher.core import config as config_mod

    monkeypatch.setattr(config_mod, "resolve_config_root", lambda probe=None: tmp_path)
    fake = FakePreflightMonitor(_snapshot(free_gib))
    monkeypatch.setattr(main_window_module, "MonitorService", lambda: fake)
    # 端口探测 stub：测试环境端口 8080 可能真被占，必须确定性
    monkeypatch.setattr(
        main_window_module,
        "check_port",
        lambda port, host="127.0.0.1": SimpleNamespace(status=SimpleNamespace(name="FREE"), in_use=False),
    )
    return MainWindow()


def _select_model(win: MainWindow, path: str) -> None:
    win._control._model_combo.clear()
    win._control._model_combo.addItem(path, path)
    win._control._model_combo.setCurrentIndex(0)


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(["test"])


# ---------------------------------------------------------------------------
# 门控：显存不足 → 「启动」禁用
# ---------------------------------------------------------------------------

class TestInsufficientGating:
    def test_start_disabled_and_panel_red(self, monkeypatch, tmp_path, qapp):
        win = _make_window(monkeypatch, tmp_path, free_gib=2)
        try:
            # 默认参数（163840 + rk4v4-e8 + mtp）需求 ≈ 22 GiB ≫ 空余 2 GiB
            verdict = win._run_preflight_and_apply()
            from ninfer_launcher.core.vram_preflight import PreflightStatus

            assert verdict.status is PreflightStatus.INSUFFICIENT
            # 预检不再硬门控：显存不足时「启动」仍可用，仅面板红色警告告知
            assert win._control._btn_start.isEnabled() is True
            assert "显存不足" in win._control.get_preflight_panel().status_label.text()
            # 缺口与候选进程都落到了面板（offscreen 下 isVisible 恒 False，用 isHidden 判显隐态）
            panel = win._control.get_preflight_panel()
            assert "缺口" in panel.summary_label.text()
            assert panel.process_list.isHidden() is False
        finally:
            win.close()

    def test_self_and_protected_excluded_from_list(self, monkeypatch, tmp_path, qapp):
        win = _make_window(monkeypatch, tmp_path, free_gib=2)
        try:
            win._run_preflight_and_apply()
            panel = win._control.get_preflight_panel()
            items = [panel.process_list.item(i).text() for i in range(panel.process_list.count())]
            # 候选只有 chrome / Code（dwm 受保护被排除；ninfer 自身不在假清单里）
            assert len(items) == 2
            assert items[0].startswith("chrome.exe")
            assert items[1].startswith("Code.exe")
        finally:
            win.close()

    def test_shortfall_reason_shown_in_panel(self, monkeypatch, tmp_path, qapp):
        """预检不再设按钮 tooltip 阻断原因；缺口/需求文案改由面板 summary 呈现。"""
        win = _make_window(monkeypatch, tmp_path, free_gib=2)
        try:
            win._run_preflight_and_apply()
            # 按钮不再承载阻断原因（不阻断）
            assert win._control._btn_start.toolTip() == ""
            summary = win._control.get_preflight_panel().summary_label.text()
            assert "需求" in summary and "缺口" in summary
        finally:
            win.close()


# ---------------------------------------------------------------------------
# 门控：显存充足 → 「启动」可用 + 自动解禁
# ---------------------------------------------------------------------------

class TestOkGating:
    def test_start_enabled_when_free(self, monkeypatch, tmp_path, qapp):
        win = _make_window(monkeypatch, tmp_path, free_gib=23)
        try:
            win._run_preflight_and_apply()
            assert win._control._btn_start.isEnabled() is True
            assert "显存充足" in win._control.get_preflight_panel().status_label.text()
        finally:
            win.close()

    def test_panel_turns_green_after_user_closes_programs(self, monkeypatch, tmp_path, qapp):
        """核心链路：不足（面板红）→ 用户退出程序 → 下一轮预检面板转绿。按钮始终可用。"""
        win = _make_window(monkeypatch, tmp_path, free_gib=2)
        try:
            win._run_preflight_and_apply()
            panel = win._control.get_preflight_panel()
            assert win._control._btn_start.isEnabled() is True  # 始终可用（不再门控）
            assert "显存不足" in panel.status_label.text()
            # 用户退掉了占显存的程序：监控读数变成空余 23 GiB
            fake = win._monitor
            fake.snapshot = _snapshot(23)
            win._run_preflight_and_apply()  # 相当于下一次 2s 定时器 tick
            assert win._control._btn_start.isEnabled() is True
            assert "显存充足" in panel.status_label.text()
        finally:
            win.close()


# ---------------------------------------------------------------------------
# 降级：读不到显存 → 不阻断启动
# ---------------------------------------------------------------------------

class TestUnavailableDegradation:
    def test_unreadable_memory_does_not_block(self, monkeypatch, tmp_path, qapp):
        win = _make_window(monkeypatch, tmp_path, free_gib=2)
        try:
            # 快照的显存字段全部读不到（监控降级场景）
            gpu = GpuSnapshot(index=0, mem_used_bytes=None, mem_total_bytes=None)
            win._monitor.snapshot = SystemSnapshot(
                source=MonitorSource.UNAVAILABLE,
                gpus=(gpu,),
                mem_used_bytes=None,
                mem_total_bytes=None,
                cpu_percent=10.0,
            )
            verdict = win._run_preflight_and_apply()
            from ninfer_launcher.core.vram_preflight import PreflightStatus

            assert verdict.status is PreflightStatus.UNAVAILABLE
            # 关键验收：不阻断
            assert win._control._btn_start.isEnabled() is True
            assert "无法预检" in win._control.get_preflight_panel().status_label.text()
        finally:
            win.close()


# ---------------------------------------------------------------------------
# 服务运行中：预检返回 RUNNING 中性态，不误报「显存不足」
# ---------------------------------------------------------------------------

class TestRunningState:
    def test_running_service_not_reported_insufficient(self, monkeypatch, tmp_path, qapp):
        """复刻 bug：服务已运行、空余仅 1.3 GiB（本服务占满）→ 面板「服务运行中」而非「显存不足」。"""
        from ninfer_launcher.core.vram_preflight import PreflightStatus

        win = _make_window(monkeypatch, tmp_path, free_gib=1.3)
        try:
            # 模拟观测到外部服务在运行（等价于自身进程非 STOPPED）
            win._external_state = ServerState.RUNNING
            verdict = win._run_preflight_and_apply()
            assert verdict.status is PreflightStatus.RUNNING
            panel = win._control.get_preflight_panel()
            assert panel.status_label.text() == "服务运行中"
            assert "缺口" not in panel.summary_label.text()
            assert panel.process_list.isHidden() is True
        finally:
            win._external_state = None  # 复位，避免 close() 弹「服务运行中，确认退出？」模态框
            win.close()

    def test_stopped_service_evaluates_normally(self, monkeypatch, tmp_path, qapp):
        """服务未运行时（默认）恢复正常评估：空余不足 → 面板「显存不足」。"""
        from ninfer_launcher.core.vram_preflight import PreflightStatus

        win = _make_window(monkeypatch, tmp_path, free_gib=1.3)
        try:
            assert win._external_state is None
            assert win._process.state is ServerState.STOPPED
            verdict = win._run_preflight_and_apply()
            assert verdict.status is PreflightStatus.INSUFFICIENT
            assert win._control.get_preflight_panel().status_label.text() == "显存不足"
        finally:
            win.close()


# ---------------------------------------------------------------------------
# _on_start 兜底：点「启动」瞬间再算一次预检
# ---------------------------------------------------------------------------

class TestStartSlotPreflightGate:
    def _armed_window(self, monkeypatch, tmp_path, free_gib, model_file: str):
        win = _make_window(monkeypatch, tmp_path, free_gib)
        _select_model(win, model_file)
        return win

    def test_insufficient_does_not_block_start(self, monkeypatch, tmp_path, qapp):
        """显存不足不再阻断启动：_on_start 不弹「显存不足」框，照常走到 process.start。"""
        model_file = tmp_path / "test.ninfer"
        model_file.write_bytes(b"stub")
        win = self._armed_window(monkeypatch, tmp_path, free_gib=2, model_file=str(model_file))
        warnings = []
        monkeypatch.setattr(
            "PySide6.QtWidgets.QMessageBox.warning",
            staticmethod(lambda *a, **k: warnings.append(a) or 0),
        )
        monkeypatch.setattr(
            "PySide6.QtWidgets.QMessageBox.critical",
            staticmethod(lambda *a, **k: 0),
        )
        started = []
        win._process.start = lambda exe, args: started.append((exe, args))
        try:
            win._on_start()
            # 不足 → 不弹「显存不足」框，且启动流程照常走到子进程启动
            assert not any("显存不足" in str(w) for w in warnings), f"warnings={warnings}"
            assert started, "显存不足时 _on_start 仍应走到 process.start（不再门控）"
            # 面板仍红色警告告知
            assert "显存不足" in win._control.get_preflight_panel().status_label.text()
        finally:
            win.close()

    def test_ok_start_proceeds_when_enough_vram(self, monkeypatch, tmp_path, qapp):
        model_file = tmp_path / "test.ninfer"
        model_file.write_bytes(b"stub")
        win = self._armed_window(monkeypatch, tmp_path, free_gib=23, model_file=str(model_file))
        warnings = []
        monkeypatch.setattr(
            "PySide6.QtWidgets.QMessageBox.warning",
            staticmethod(lambda *a, **k: warnings.append(a) or 0),
        )
        monkeypatch.setattr(
            "PySide6.QtWidgets.QMessageBox.critical",
            staticmethod(lambda *a, **k: 0),
        )
        # stub 掉真实子进程启动：只验证「预检放行后 _on_start 走到了 start()」，不真起服务
        started = []
        win._process.start = lambda exe, args: started.append((exe, args))
        try:
            win._on_start()
            # 门控语义：无「显存不足」框，且启动流程走到了子进程启动
            assert not any("显存不足" in str(w) for w in warnings)
            assert started, "显存充足时 _on_start 应走到 process.start"
            assert win._process.state is ServerState.STOPPED  # start 被 stub，状态机未动
        finally:
            win.close()


# ---------------------------------------------------------------------------
# 参数变化去抖：连续 valueChanged 只在稳定后重算一次
# ---------------------------------------------------------------------------

class TestParamChangeDebounce:
    def test_rapid_changes_coalesce_to_single_recompute(self, monkeypatch, tmp_path, qapp):
        """spinbox 连续拖动发一串 valueChanged → 去抖定时器只挂起一次，触发后只重算一次。

        钉住去抖语义（用可重启的 setSingleShot QTimer，而非各起独立 singleShot）：
        - 连发 N 次 valueChanged 期间不产生 N 次重算（定时器未到点）；
        - 去抖定时器是单次触发（isSingleShot）；
        - 手动触发一次 timeout 后，_run_preflight_and_apply 恰好被调用一次。
        """
        win = _make_window(monkeypatch, tmp_path, free_gib=23)
        try:
            from ninfer_launcher.core.vram_preflight import PREFLIGHT_PARAM_DEBOUNCE_MS

            # 计数器：直接连到去抖定时器的 timeout（与生产接线并存，不替换绑定方法——
            # QTimer.timeout 连的是构造期就绑好的 self._run_preflight_and_apply，
            # monkeypatch 实例属性改不到已连接的绑定方法）。
            calls = []
            win._preflight_debounce.timeout.connect(lambda: calls.append(1))

            assert win._preflight_debounce.isSingleShot() is True
            assert win._preflight_debounce.interval() == PREFLIGHT_PARAM_DEBOUNCE_MS
            # 连发一串参数变化（模拟拖动 spinbox）
            for _ in range(20):
                win._on_param_value_changed("max_context", 8192)
            # 定时器尚未到点：一次都还没触发，且只挂起了这一个去抖定时器（可重启，未累积）
            assert calls == []
            assert win._preflight_debounce.isActive() is True

            # 定时器到点：合并成单次触发
            win._preflight_debounce.timeout.emit()
            assert calls == [1]
        finally:
            win.close()


# ---------------------------------------------------------------------------
# 权重日志 → weight_bytes_cache 回填
# ---------------------------------------------------------------------------

class TestWeightCacheFromLog:
    def test_log_line_records_and_persists(self, monkeypatch, tmp_path, qapp):
        model_file = tmp_path / "test.ninfer"
        model_file.write_bytes(b"stub")
        win = _make_window(monkeypatch, tmp_path, free_gib=2)
        try:
            _select_model(win, str(model_file))
            from ninfer_launcher.core.process import LogLine, LogSource
            from ninfer_launcher.core.vram_estimate import GIB as _GIB

            weight = int(16.95 * _GIB)
            line = LogLine(
                LogSource.STDERR,
                f"load weights                     100.00%   {weight / (1024 ** 3):.2f} GiB /   {weight / (1024 ** 3):.2f} GiB 12.345 s",
            )
            win._on_log_line(line)
            # 内存里的缓存已回填
            assert win._settings.weight_bytes_cache.get(str(model_file)) == weight
            # 且已落盘 settings.json（下次启动首跑即用实测值）
            from ninfer_launcher.core import config as config_mod

            reloaded = config_mod.load_settings(tmp_path)
            assert reloaded.weight_bytes_cache.get(str(model_file)) == weight
        finally:
            win.close()

    def test_non_weight_log_line_is_ignored(self, monkeypatch, tmp_path, qapp):
        win = _make_window(monkeypatch, tmp_path, free_gib=2)
        try:
            _select_model(win, str(tmp_path / "test.ninfer"))
            from ninfer_launcher.core.process import LogLine, LogSource

            win._on_log_line(LogLine(LogSource.STDERR, "serve ready on :8080"))
            assert win._settings.weight_bytes_cache == {}
        finally:
            win.close()


# ---------------------------------------------------------------------------
# 安全垫接线：面板 SpinBox → 落 settings + 预检重算
# ---------------------------------------------------------------------------

class TestSafetyControlIntegration:
    def test_panel_initialized_from_settings(self, monkeypatch, tmp_path, qapp):
        """构造期把 settings.safety_bytes 灌进面板 SpinBox。"""
        from ninfer_launcher.core import config as config_mod

        # 预置一个非默认安全垫到 settings.json
        config_mod.save_settings(tmp_path, config_mod.Settings(safety_bytes=1 * GIB))
        win = _make_window(monkeypatch, tmp_path, free_gib=23)
        try:
            assert win._settings.safety_bytes == 1 * GIB
            assert win._control.get_preflight_panel().get_safety_bytes() == 1 * GIB
        finally:
            win.close()

    def test_change_persists_and_affects_requirement(self, monkeypatch, tmp_path, qapp):
        """改安全垫 → 写盘 settings.json + 下一轮预检需求随之变大。"""
        from ninfer_launcher.core import config as config_mod

        win = _make_window(monkeypatch, tmp_path, free_gib=23)
        try:
            before = win._run_preflight_and_apply().requirement.total_bytes
            # 用户把安全垫从默认 200 MiB 调到 3 GiB
            win._on_safety_changed(3 * GIB)
            # 已落盘
            assert win._settings.safety_bytes == 3 * GIB
            assert config_mod.load_settings(tmp_path).safety_bytes == 3 * GIB
            # 需求随之增大（差值 = 3 GiB - 200 MiB）
            after = win._run_preflight_and_apply().requirement.total_bytes
            assert after - before == 3 * GIB - 200 * MIB
        finally:
            win.close()

    def test_noop_change_does_not_write(self, monkeypatch, tmp_path, qapp):
        """值未变时不写盘（幂等）。"""
        win = _make_window(monkeypatch, tmp_path, free_gib=23)
        try:
            writes = []
            win._store.save_settings = lambda s: writes.append(s)
            win._on_safety_changed(win._settings.safety_bytes)  # 传当前值
            assert writes == []
        finally:
            win.close()
