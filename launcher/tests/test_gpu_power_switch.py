"""``ui/gpu_power_switch.py``：性能模式开关的勾选同步、失败回弹、不可用提示（需求 7）。

全程用注入的假 reader/writer/probe（``ui/gpu_power_switch.py`` 契约 5），**不碰真实
显卡驱动**。

本文件盯的是三处「做错了验证不会变红」的地方：

1. **写失败后勾选必须回弹**。不回弹的话界面上留着一个「已开启」的勾，而驱动里什么都
   没变——这比不显示更糟，因为使用者会据此以为性能模式生效了。
2. **回弹不能再触发一次写入**（契约 1）。少了那道 ``_applying`` 闸就是来回写驱动的
   循环；循环本身不报错，只是每点一次开关就往驱动写好几遍。
3. **不可用时既禁用又给出原因**（契约 3）。只灰掉不说明原因，使用者无法区分
   「没有 NVIDIA 卡」「驱动太旧」「权限不足」这三种完全不同的处置。
"""

import pytest

from ninfer_launcher.core.gpu_power import (
    DISABLE_MODE,
    ENABLE_MODE,
    PowerMode,
    PowerModeResult,
)
from ninfer_launcher.ui.gpu_power_switch import SWITCH_LABEL, SWITCH_TOOLTIP, GpuPowerSwitch


class FakeDriver:
    """有状态的假驱动：记住写进去的档位，让读取跟着变。

    刻意不做成「恒定返回值」的桩：恒定返回值会让「点击后状态是否真的跟着变」这类
    断言即使实现坏了也照样通过（工程纪律「强断言」）。
    """

    def __init__(self, *, mode: PowerMode = DISABLE_MODE, available: bool = True) -> None:
        self.mode = mode
        self.available = available
        self.fail_write = False
        self.write_calls: list[bool] = []
        self.read_calls = 0

    def probe(self) -> PowerModeResult:
        if not self.available:
            return PowerModeResult(False, None, "（假）没有 NVIDIA 显卡")
        return PowerModeResult(True, None, "（假）可用")

    def read(self) -> PowerModeResult:
        self.read_calls += 1
        if not self.available:
            return PowerModeResult(False, None, "（假）没有 NVIDIA 显卡")
        return PowerModeResult(True, self.mode, f"（假）当前 {self.mode.label}")

    def write(self, enable: bool) -> PowerModeResult:
        self.write_calls.append(enable)
        if self.fail_write:
            return PowerModeResult(False, None, "（假）权限不足")
        self.mode = ENABLE_MODE if enable else DISABLE_MODE
        return PowerModeResult(True, self.mode, f"（假）已设为 {self.mode.label}")


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance()


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver()


def make_switch(driver: FakeDriver) -> GpuPowerSwitch:
    return GpuPowerSwitch(
        reader=driver.read, writer=driver.write, availability_probe=driver.probe
    )


@pytest.fixture
def switch(qt_app, driver):
    return make_switch(driver)


class TestConstruction:
    def test_available_when_probe_ok(self, switch) -> None:
        assert switch.available is True
        assert switch.isEnabled() is True

    def test_does_not_read_mode_during_construction(self, qt_app, driver) -> None:
        """契约 4：构造期只探可用性，不建 DRS session 去读档位。"""
        make_switch(driver)
        assert driver.read_calls == 0

    def test_does_not_write_during_construction(self, qt_app, driver) -> None:
        make_switch(driver)
        assert driver.write_calls == []

    def test_tooltip_mentions_it_is_not_a_ninfer_serve_flag(self, switch) -> None:
        # 这一行是使用者唯一的线索：这个开关跟 ninfer-serve 的参数不是一回事。
        assert "不是 ninfer-serve" in SWITCH_TOOLTIP
        assert switch.toolTip() == SWITCH_TOOLTIP

    def test_tooltip_states_disable_semantics(self) -> None:
        # core/gpu_power.py 契约 8 的语义必须传达到界面上。
        assert "出厂默认" in SWITCH_TOOLTIP

    def test_tooltip_states_the_cost(self) -> None:
        assert "降频" in SWITCH_TOOLTIP

    def test_label_constant_is_chinese(self) -> None:
        assert SWITCH_LABEL == "性能模式"


class TestRefreshFollowsDriver:
    """契约 2：勾选状态的事实源是驱动，每次 refresh 重读。"""

    def test_refresh_reads_driver(self, switch, driver) -> None:
        switch.refresh()
        assert driver.read_calls == 1

    def test_refresh_checks_when_driver_reports_prefer_max(self, switch, driver) -> None:
        driver.mode = ENABLE_MODE
        switch.refresh()
        assert switch.isChecked() is True

    def test_refresh_unchecks_when_driver_reports_default(self, switch, driver) -> None:
        driver.mode = ENABLE_MODE
        switch.refresh()
        driver.mode = DISABLE_MODE
        switch.refresh()
        assert switch.isChecked() is False

    def test_refresh_does_not_write(self, switch, driver) -> None:
        """契约 1：同步勾选状态不得反过来触发一次写入。"""
        driver.mode = ENABLE_MODE
        switch.refresh()
        assert driver.write_calls == []


class TestToggleWritesDriver:
    def test_checking_writes_enable(self, switch, driver) -> None:
        switch.setChecked(True)
        assert driver.write_calls == [True]
        assert driver.mode is ENABLE_MODE

    def test_unchecking_writes_disable(self, switch, driver) -> None:
        switch.setChecked(True)
        switch.setChecked(False)
        assert driver.write_calls == [True, False]
        assert driver.mode is DISABLE_MODE

    def test_each_toggle_writes_exactly_once(self, switch, driver) -> None:
        """契约 1 的核心：一次点击只写一遍，不因为内部同步而重复写。"""
        switch.setChecked(True)
        assert len(driver.write_calls) == 1

    def test_emits_message_on_success(self, switch, driver) -> None:
        received: list[str] = []
        switch.messageReady.connect(lambda lines: received.extend(lines))
        switch.setChecked(True)
        assert len(received) == 1
        assert "已设为" in received[0]


class TestWriteFailureRollsBack:
    def test_checkbox_rolls_back_when_write_fails(self, switch, driver) -> None:
        driver.fail_write = True
        switch.setChecked(True)
        assert switch.isChecked() is False

    def test_driver_state_unchanged_when_write_fails(self, switch, driver) -> None:
        driver.fail_write = True
        switch.setChecked(True)
        assert driver.mode is DISABLE_MODE

    @pytest.mark.smoke
    def test_rollback_does_not_trigger_second_write(self, switch, driver) -> None:
        """契约 1：回弹的 setChecked 不得再触发一次写入（否则是来回写的循环）。"""
        driver.fail_write = True
        switch.setChecked(True)
        assert driver.write_calls == [True]

    def test_emits_reason_on_failure(self, switch, driver) -> None:
        driver.fail_write = True
        received: list[str] = []
        switch.messageReady.connect(lambda lines: received.extend(lines))
        switch.setChecked(True)
        assert len(received) == 1
        assert "权限不足" in received[0]
        assert "最高性能优先" in received[0]


class TestUnavailable:
    @pytest.fixture
    def unavailable_switch(self, qt_app):
        driver = FakeDriver(available=False)
        return make_switch(driver), driver

    def test_disabled_when_probe_fails(self, unavailable_switch) -> None:
        switch, _driver = unavailable_switch
        assert switch.available is False
        assert switch.isEnabled() is False

    def test_tooltip_states_the_reason(self, unavailable_switch) -> None:
        """契约 3：只灰掉不说明原因，使用者无法判断该怎么处置。"""
        switch, _driver = unavailable_switch
        assert "没有 NVIDIA 显卡" in switch.toolTip()

    def test_tooltip_still_contains_full_explanation(self, unavailable_switch) -> None:
        switch, _driver = unavailable_switch
        assert SWITCH_TOOLTIP in switch.toolTip()

    def test_refresh_keeps_it_disabled(self, unavailable_switch) -> None:
        switch, _driver = unavailable_switch
        result = switch.refresh()
        assert result.ok is False
        assert switch.isEnabled() is False
        assert switch.isChecked() is False
