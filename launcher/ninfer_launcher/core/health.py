"""ninfer-serve /health 周期探测（Qt 版）。

纯探测逻辑（HealthState / HealthResult / classify_health / probe_health / 常量）已拆到
core/health_probe.py——零 Qt 依赖，CLI（cli/）直接复用那里；本模块只保留 QTimer 驱动的
周期探测器 HealthPoller，并原样重导出全部纯逻辑符号，GUI 与既有测试的既有导入路径保持不变。"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .health_probe import (
    HEALTH_INTERVAL_MS,
    HEALTH_TIMEOUT_SECONDS,
    HealthResult,
    HealthState,
    classify_health,
    probe_health,
)

__all__ = [
    "HEALTH_INTERVAL_MS",
    "HEALTH_TIMEOUT_SECONDS",
    "HealthState",
    "HealthResult",
    "classify_health",
    "probe_health",
    "HealthPoller",
]


class HealthPoller(QObject):
    """周期探测 /health，就绪时发信号。

    信号：
    - ready: 探通
    - not_ready (str): 未就绪的详情
    - aborted (str): 中止原因
    """

    ready = Signal()
    not_ready = Signal(str)
    aborted = Signal(str)

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        *,
        interval_ms: int = HEALTH_INTERVAL_MS,
        abort_check: Callable[[], str | None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._port = port
        self._abort_check = abort_check
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    @property
    def host(self) -> str:
        return self._host

    @host.setter
    def host(self, value: str) -> None:
        self._host = value

    @property
    def port(self) -> int:
        return self._port

    @port.setter
    def port(self, value: int) -> None:
        self._port = value

    def start(self) -> None:
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        if self._abort_check:
            reason = self._abort_check()
            if reason:
                self._timer.stop()
                self.aborted.emit(reason)
                return
        result = probe_health(self._host, self._port)
        if result.state is HealthState.READY:
            self._timer.stop()
            self.ready.emit()
        else:
            self.not_ready.emit(result.detail)
