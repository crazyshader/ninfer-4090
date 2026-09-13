"""ui/preflight_panel.py 单测：三态渲染、进程清单高亮（最小补齐集合）、
「刷新」按钮信号、初始态。

面板零系统接触面（不碰 NVML / psutil），测试只需构造假 PreflightVerdict 灌入。
"""

import pytest

from PySide6.QtWidgets import QApplication

from ninfer_launcher.core.gpu_processes import GpuProcess
from ninfer_launcher.core.monitor import GpuSnapshot
from ninfer_launcher.core.vram_estimate import (
    GIB,
    MIB,
    SAFETY_MAX_BYTES,
    SAFETY_MIN_BYTES,
    VramConfig,
)
from ninfer_launcher.core.vram_preflight import (
    PreflightStatus,
    PreflightVerdict,
    evaluate,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(["test"])
    yield app


@pytest.fixture
def panel(qapp):
    from ninfer_launcher.ui.preflight_panel import PreflightPanel

    return PreflightPanel()


def _verdict(status: str, *, free_gib=None, total_gib=24, config_overrides=None) -> PreflightVerdict:
    """经真实 evaluate() 造 verdict（比手工拼 dataclass 更贴近真实渲染路径）。"""
    overrides = dict(max_context=1024, kv_dtype="rk4v4-e8", spec="mtp", vision=True, weight_bytes=8 * GIB)
    overrides.update(config_overrides or {})
    cfg = VramConfig(**overrides)
    if status is PreflightStatus.OK:
        free = 16 if free_gib is None else free_gib
        gpu = _snapshot(free)
        return evaluate(cfg, gpu, ())
    if status is PreflightStatus.UNAVAILABLE:
        return evaluate(cfg, None, ())
    if status is PreflightStatus.RUNNING:
        free = 1 if free_gib is None else free_gib
        return evaluate(cfg, _snapshot(free), (), server_running=True)
    # INSUFFICIENT
    free = 4 if free_gib is None else free_gib
    procs = (
        GpuProcess(pid=1, name="chrome.exe", used_bytes=6 * GIB),
        GpuProcess(pid=2, name="Code.exe", used_bytes=4 * GIB),
        GpuProcess(pid=3, name="dwm.exe", used_bytes=1 * GIB, is_protected=True),
        GpuProcess(pid=4, name="ninfer-serve.exe", used_bytes=8 * GIB, is_self=True),
    )
    return evaluate(cfg, _snapshot(free), procs)


def _snapshot(free_gib) -> GpuSnapshot:
    total = int(24 * GIB)
    return GpuSnapshot(
        index=0, name="RTX 4090",
        mem_used_bytes=total - int(free_gib * GIB),
        mem_total_bytes=total,
    )


class TestInitialState:
    def test_pending_before_first_verdict(self, panel):
        assert panel.status_label.text() == "等待预检…"
        # offscreen 下顶层窗口从未 show()，isVisible 恒 False；用 isHidden 判显隐状态
        assert panel.process_list.isHidden() is True


class TestOkRendering:
    def test_green_status_and_summary(self, panel):
        verdict = _verdict(PreflightStatus.OK)
        panel.apply_verdict(verdict)
        assert panel.status_label.text() == "显存充足"
        assert "需求" in panel.summary_label.text()
        assert "空余" in panel.summary_label.text()
        assert panel.process_list.isHidden() is True
        assert panel.note_label.isHidden() is True


class TestInsufficientRendering:
    def test_red_status_and_summary(self, panel):
        panel.apply_verdict(_verdict(PreflightStatus.INSUFFICIENT))
        assert panel.status_label.text() == "显存不足"
        text = panel.summary_label.text()
        assert "需求" in text and "空余" in text and "缺口" in text

    def test_process_list_shows_user_processes_excluding_self_and_protected(self, panel):
        panel.apply_verdict(_verdict(PreflightStatus.INSUFFICIENT))
        assert panel.process_list.isHidden() is False
        assert panel.process_list.count() == 2
        items = [panel.process_list.item(i).text() for i in range(panel.process_list.count())]
        assert items[0].startswith("chrome.exe")
        assert items[1].startswith("Code.exe")
        # 自身与系统关键进程不进清单
        assert not any("dwm.exe" in t for t in items)
        assert not any("ninfer-serve" in t for t in items)
        # 清单里带 PID 与占用
        assert "PID 1" in items[0]
        assert "GiB" in items[0]

    def test_highlighted_minimal_set(self, panel):
        """空余 5 GiB → 缺口 ~5.2 GiB < chrome(6G) → 仅 chrome 高亮，Code 不高亮。"""
        panel.apply_verdict(_verdict(PreflightStatus.INSUFFICIENT, free_gib=5))
        from PySide6.QtGui import QColor

        highlight = QColor("#FFD93D").name()
        first = panel.process_list.item(0)
        second = panel.process_list.item(1)
        assert first.foreground().color().name() == highlight
        assert second.foreground().color().name() != highlight

    def test_note_shows_closest_actionable_line(self, panel):
        panel.apply_verdict(_verdict(PreflightStatus.INSUFFICIENT))
        assert panel.note_label.isHidden() is False
        assert "退出" in panel.note_label.text() or "任务管理器" in panel.note_label.text()


class TestUnavailableRendering:
    def test_gray_status_and_warning(self, panel):
        panel.apply_verdict(_verdict(PreflightStatus.UNAVAILABLE))
        assert panel.status_label.text() == "无法预检"
        assert "风险自负" in panel.summary_label.text()
        assert panel.process_list.isHidden() is True


class TestRunningRendering:
    def test_shows_running_status_not_shortfall(self, panel):
        """服务运行中：显示「服务运行中」+ 当前占用，不显示缺口 / 进程清单。"""
        panel.apply_verdict(_verdict(PreflightStatus.RUNNING))
        assert panel.status_label.text() == "服务运行中"
        text = panel.summary_label.text()
        assert "服务运行中" in text
        assert "缺口" not in text  # 关键：不再误报缺口
        assert panel.process_list.isHidden() is True
        assert panel.note_label.isHidden() is True

    def test_shows_current_usage(self, panel):
        """空余 1 GiB / 总 24 GiB → 已用约 23 GiB 落到 summary。"""
        panel.apply_verdict(_verdict(PreflightStatus.RUNNING, free_gib=1))
        assert "已用" in panel.summary_label.text()
        assert "24 GiB" in panel.summary_label.text()


class TestRefreshSignal:
    def test_refresh_button_emits_signal(self, panel):
        calls = []
        panel.refresh_requested.connect(lambda: calls.append(1))
        panel._btn_refresh.click()
        assert calls == [1]


class TestSafetyControl:
    """安全垫 SpinBox：范围 / 初值 / set-get / 值变化发信号（已夹紧字节）。"""

    def test_spin_range_and_initial_value(self, panel):
        assert panel.safety_spin.minimum() == SAFETY_MIN_BYTES // MIB
        assert panel.safety_spin.maximum() == SAFETY_MAX_BYTES // MIB
        # 初值为最小值（默认安全垫）
        assert panel.safety_spin.value() == SAFETY_MIN_BYTES // MIB
        assert panel.get_safety_bytes() == SAFETY_MIN_BYTES

    def test_set_and_get_bytes(self, panel):
        panel.set_safety_bytes(1 * GIB)
        assert panel.safety_spin.value() == (1 * GIB) // MIB
        assert panel.get_safety_bytes() == 1 * GIB

    def test_set_clamps_out_of_range(self, panel):
        panel.set_safety_bytes(10 * GIB)
        assert panel.get_safety_bytes() == SAFETY_MAX_BYTES
        panel.set_safety_bytes(1 * MIB)
        assert panel.get_safety_bytes() == SAFETY_MIN_BYTES

    def test_set_does_not_emit_signal(self, panel):
        """初始化回填（set_safety_bytes）不应触发 safety_changed（屏蔽信号）。"""
        emitted = []
        panel.safety_changed.connect(emitted.append)
        panel.set_safety_bytes(1 * GIB)
        assert emitted == []

    def test_user_change_emits_clamped_bytes(self, panel):
        """用户改 SpinBox → safety_changed 携带字节数（= MiB 值 × MIB）。"""
        emitted = []
        panel.safety_changed.connect(emitted.append)
        panel.safety_spin.setValue(1024)  # 1024 MiB = 1 GiB
        assert emitted == [1024 * MIB]
