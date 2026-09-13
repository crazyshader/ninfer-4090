"""测试 MonitorPanel._apply 在 GPU 可用时的界面更新逻辑。

回归测试：pct 变量曾在 f-string 中被引用后才赋值，导致 UnboundLocalError
中断 _apply，使后续所有标签停留在「不可用」。本测试确保 GPU 可用且显存
数据存在时 _apply 完整执行、不抛异常、标签被正确更新。
"""

import pytest

from ninfer_launcher.core.monitor import (
    GpuSnapshot,
    MonitorService,
    MonitorSource,
    SystemSnapshot,
)
from ninfer_launcher.ui.monitor_panel import MonitorPanel

GIB = 1024 ** 3


@pytest.fixture(scope="module")
def qapp():
    """模块级 QApplication，整个测试模块共享一个 Qt 实例。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(["test"])
    yield app


class _FakeService:
    """最小假 MonitorService：poll 返回预设快照。"""

    def __init__(self, snapshot: SystemSnapshot) -> None:
        self._snapshot = snapshot

    def poll(self) -> SystemSnapshot:
        return self._snapshot


def _make_gpu_snapshot(
    *,
    mem_used: int | None = 20 * GIB,
    mem_total: int | None = 24 * GIB,
    clock: int | None = 2520,
    temp: int | None = 55,
    power_draw: float | None = 180.0,
    power_limit: float | None = 450.0,
    sys_mem_used: int | None = 24 * GIB,
    sys_mem_total: int | None = 64 * GIB,
    cpu: float | None = 12.5,
) -> SystemSnapshot:
    gpu = GpuSnapshot(
        index=0,
        name="NVIDIA GeForce RTX 4090",
        mem_used_bytes=mem_used,
        mem_total_bytes=mem_total,
        sm_clock_mhz=clock,
        temperature_c=temp,
        power_draw_w=power_draw,
        power_limit_w=power_limit,
    )
    return SystemSnapshot(
        source=MonitorSource.NVML,
        gpus=(gpu,),
        mem_used_bytes=sys_mem_used,
        mem_total_bytes=sys_mem_total,
        cpu_percent=cpu,
    )


class TestApplyGpuAvailable:
    """GPU 可用时 _apply 应完整执行，所有标签被更新，不抛异常。"""

    def test_apply_does_not_raise(self, qapp):
        """回归：pct 曾在 f-string 中被引用后才赋值，触发 UnboundLocalError。"""
        svc = _FakeService(_make_gpu_snapshot())
        panel = MonitorPanel(service=svc, auto_start=False)
        # 不应抛出 UnboundLocalError 或任何其他异常
        panel._apply(panel._last_snapshot or _make_gpu_snapshot())
        panel.stop()

    def test_vram_text_contains_percentage(self, qapp):
        """显存文字应包含 GB 值和百分比。"""
        snap = _make_gpu_snapshot(mem_used=22 * GIB, mem_total=24 * GIB)
        svc = _FakeService(snap)
        panel = MonitorPanel(service=svc, auto_start=False)
        panel._apply(snap)
        text = panel._vram_text.text()
        assert "22.0/24 GB" in text
        assert "91%" in text
        assert panel._vram_bar.value() == 91
        panel.stop()

    def test_all_labels_updated(self, qapp):
        """GPU 可用时所有读数标签都应从「不可用」更新为实际值。"""
        snap = _make_gpu_snapshot()
        svc = _FakeService(snap)
        panel = MonitorPanel(service=svc, auto_start=False)
        panel._apply(snap)
        assert panel.gpu_label.text() == "GPU 0：NVIDIA GeForce RTX 4090"
        assert panel.clock_label.text() == "2520 MHz"
        assert panel.temp_label.text() == "55°C"
        assert "180.0 W" in panel.power_label.text()
        assert "450.0 W" in panel.power_label.text()
        assert "12.5%" in panel.cpu_label.text()
        panel.stop()

    def test_vram_row_shown_when_gpu_available(self, qapp):
        snap = _make_gpu_snapshot()
        svc = _FakeService(snap)
        panel = MonitorPanel(service=svc, auto_start=False)
        assert panel._vram_row.isHidden()  # 构造时默认隐藏
        panel._apply(snap)
        assert not panel._vram_row.isHidden()  # GPU 可用后应显示
        panel.stop()


class TestApplyGpuUnavailable:
    """GPU 不可用时 _apply 应设所有标签为「不可用」并隐藏进度条。"""

    def test_all_labels_unavailable(self, qapp):
        snap = SystemSnapshot(source=MonitorSource.UNAVAILABLE)
        svc = _FakeService(snap)
        panel = MonitorPanel(service=svc, auto_start=False)
        panel._apply(snap)
        assert panel.gpu_label.text() == "不可用"
        assert panel.clock_label.text() == "不可用"
        assert panel.temp_label.text() == "不可用"
        assert panel.power_label.text() == "不可用"
        assert panel.cpu_label.text() == "不可用"
        panel.stop()

    def test_vram_row_hidden_when_unavailable(self, qapp):
        snap = SystemSnapshot(source=MonitorSource.UNAVAILABLE)
        svc = _FakeService(snap)
        panel = MonitorPanel(service=svc, auto_start=False)
        # 先让它可见
        panel._vram_row.setVisible(True)
        assert not panel._vram_row.isHidden()
        panel._apply(snap)
        assert panel._vram_row.isHidden()
        panel.stop()


class TestApplyVramBarColor:
    """显存进度条颜色随使用率变化：绿 < 60% ≤ 黄 < 85% ≤ 红。"""

    @pytest.mark.parametrize(
        "used, total, expected_pct, expected_color",
        [
            (10 * GIB, 24 * GIB, 41, "#6BCB77"),   # 绿
            (16 * GIB, 24 * GIB, 66, "#FFD93D"),   # 黄
            (22 * GIB, 24 * GIB, 91, "#FF6B6B"),   # 红
        ],
    )
    def test_color_thresholds(self, qapp, used, total, expected_pct, expected_color):
        snap = _make_gpu_snapshot(mem_used=used, mem_total=total)
        svc = _FakeService(snap)
        panel = MonitorPanel(service=svc, auto_start=False)
        panel._apply(snap)
        assert panel._vram_bar.value() == expected_pct
        qss = panel._vram_bar.styleSheet()
        assert expected_color in qss
        panel.stop()
