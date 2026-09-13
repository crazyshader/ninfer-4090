"""回归：「服务器控制」四按钮的可用性必须严格跟随状态机（用户反馈，2026-09）。

需求（截图红框）：
- 「打开 WebUI」只在 RUNNING 可用——服务没就绪时 WebUI 必然不可用；
- 「显示命令」在 STOPPED / STARTING / RUNNING 都可用，仅 STOPPING 禁用；
- 「启动」仅 STOPPED 可用；「停止」仅 STARTING / RUNNING 可用。

此前 apply_state 从未碰 _btn_cmd，且构造期默认 enabled=True，导致 STOPPING 期间
「显示命令」仍可点击、STOPPED 初始态与状态矩阵脱节。本文件把整张矩阵钉死。
"""

import pytest

from ninfer_launcher.core.monitor import GpuSnapshot, MonitorSource, SystemSnapshot
from ninfer_launcher.core.process import ServerState
from ninfer_launcher.ui.control_panel import ControlPanel

GIB = 1024 ** 3


class _FakeMonitor:
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


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(["test"])


@pytest.fixture
def panel(qapp):
    p = ControlPanel(
        model_dir="no-such-models-dir",
        monitor_service=_FakeMonitor(_snapshot()),
    )
    yield p
    p.shutdown()


# state -> (start, stop, webui, cmd)
EXPECTED = {
    ServerState.STOPPED:   (True,  False, False, True),
    ServerState.STARTING:  (False, True,  False, True),
    ServerState.RUNNING:   (False, True,  True,  True),
    ServerState.STOPPING:  (False, False, False, False),
}


class TestButtonEnableMatrix:
    @pytest.mark.parametrize("state", list(ServerState))
    def test_matrix(self, panel, state):
        panel.apply_state(state, port=8080)
        expected_start, expected_stop, expected_webui, expected_cmd = EXPECTED[state]
        assert panel._btn_start.isEnabled() is expected_start, f"{state}: 启动"
        assert panel._btn_stop.isEnabled() is expected_stop, f"{state}: 停止"
        assert panel._btn_webui.isEnabled() is expected_webui, f"{state}: WebUI"
        assert panel._btn_cmd.isEnabled() is expected_cmd, f"{state}: 显示命令"

    def test_initial_state_matches_stopped(self, panel):
        """构造后未跑过事件循环：按钮初值必须与 apply_state(STOPPED) 一致，
        否则首帧界面与真实状态脱节。"""
        # 不触发任何信号/槽，只读初值
        assert panel._btn_start.isEnabled() is True
        assert panel._btn_stop.isEnabled() is False
        assert panel._btn_webui.isEnabled() is False
        assert panel._btn_cmd.isEnabled() is True

    def test_running_enables_webui_and_cmd(self, panel):
        panel.apply_state(ServerState.RUNNING, port=8080)
        assert panel._btn_webui.isEnabled() is True
        assert panel._btn_cmd.isEnabled() is True
        assert panel.webui_url() == "http://127.0.0.1:8080"
