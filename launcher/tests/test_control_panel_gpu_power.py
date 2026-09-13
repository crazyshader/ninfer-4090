"""ControlPanel 对「性能模式」开关的集成测试。

验证 main_window 把 GpuPowerSwitch 注入给 ControlPanel 后：开关被真正摆进「通用设置」
组、可通过 getter 取回同一个实例、且开关不可用时禁用但仍挂在界面上。全程用假
reader/writer/probe（不碰真实显卡驱动）与假 MonitorService（不碰真实 NVML/GPU 读数）。
"""

import pytest

from ninfer_launcher.core.gpu_power import (
    DISABLE_MODE,
    ENABLE_MODE,
    PowerModeResult,
)
from ninfer_launcher.core.monitor import GpuSnapshot, MonitorSource, SystemSnapshot
from ninfer_launcher.ui.control_panel import ControlPanel
from ninfer_launcher.ui.gpu_power_switch import GpuPowerSwitch


GIB = 1024 ** 3


class _FakeMonitor:
    """最小假 MonitorService：poll 返回固定快照，shutdown 为空操作。"""

    def __init__(self, snapshot: SystemSnapshot) -> None:
        self._snapshot = snapshot

    def poll(self) -> SystemSnapshot:
        return self._snapshot

    def shutdown(self) -> None:
        pass


def _snapshot() -> SystemSnapshot:
    gpu = GpuSnapshot(
        index=0,
        name="NVIDIA GeForce RTX 4090",
        mem_used_bytes=8 * GIB,
        mem_total_bytes=24 * GIB,
        sm_clock_mhz=2520,
        temperature_c=45,
        power_draw_w=60.0,
        power_limit_w=450.0,
    )
    return SystemSnapshot(
        source=MonitorSource.NVML,
        gpus=(gpu,),
        mem_used_bytes=20 * GIB,
        mem_total_bytes=64 * GIB,
        cpu_percent=10.0,
    )


class _FakeDriver:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.mode = DISABLE_MODE
        self.writes: list[bool] = []

    def probe(self) -> PowerModeResult:
        if not self.available:
            return PowerModeResult(False, None, "（假）没有 NVIDIA 显卡")
        return PowerModeResult(True, None, "（假）可用")

    def read(self) -> PowerModeResult:
        return PowerModeResult(True, self.mode, f"（假）{self.mode.label}")

    def write(self, enable: bool) -> PowerModeResult:
        self.writes.append(enable)
        self.mode = ENABLE_MODE if enable else DISABLE_MODE
        return PowerModeResult(True, self.mode, f"（假）{self.mode.label}")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(["test"])


def _switch_for(driver: _FakeDriver) -> GpuPowerSwitch:
    return GpuPowerSwitch(
        reader=driver.read, writer=driver.write, availability_probe=driver.probe
    )


class TestSwitchWiring:
    def test_injected_switch_is_hosted_by_panel(self, qapp) -> None:
        driver = _FakeDriver()
        switch = _switch_for(driver)
        panel = ControlPanel(
            model_dir="no-such-models-dir",
            monitor_service=_FakeMonitor(_snapshot()),
            gpu_power_switch=switch,
        )
        try:
            # getter 取回的是同一实例（main_window 注入的开关被原样摆进布局）。
            assert panel.get_gpu_power_switch() is switch
            # 开关确实挂进了控制面板的控件树。
            assert switch in panel.findChildren(GpuPowerSwitch)
        finally:
            panel.shutdown()


class TestUnavailableSwitchStillHosted:
    def test_unavailable_probe_yields_disabled_but_present_switch(self, qapp) -> None:
        driver = _FakeDriver(available=False)
        switch = _switch_for(driver)
        panel = ControlPanel(
            model_dir="no-such-models-dir",
            monitor_service=_FakeMonitor(_snapshot()),
            gpu_power_switch=switch,
        )
        try:
            assert panel.get_gpu_power_switch() is switch
            # 构造期探测失败 → 控件禁用（契约 3：灰掉且给出原因）。
            assert switch.isEnabled() is False
            assert "没有 NVIDIA 显卡" in switch.toolTip()
        finally:
            panel.shutdown()