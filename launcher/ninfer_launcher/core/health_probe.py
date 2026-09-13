"""ninfer-serve /health 单次探测：纯逻辑，零 Qt 依赖。

/health 返回 200 表示就绪，503 表示未就绪（模型加载中）。本模块只包含 GUI 与 CLI
（cli/）共享的探测原语；QTimer 驱动的周期探测器 HealthPoller 依赖 Qt，单独放在
core/health.py。CLI 禁止 import core/health.py（模块顶层会 import PySide6），
统一从本模块取 probe_health / classify_health / HealthResult / 常量。"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "HEALTH_INTERVAL_MS",
    "HEALTH_TIMEOUT_SECONDS",
    "HealthState",
    "HealthResult",
    "classify_health",
    "probe_health",
]

#: 探测周期（毫秒）
HEALTH_INTERVAL_MS = 500

#: 单次请求超时（秒）
HEALTH_TIMEOUT_SECONDS = 3.0


class HealthState(Enum):
    UNKNOWN = "unknown"
    NOT_READY = "not_ready"
    READY = "ready"
    ABORTED = "aborted"


@dataclass(frozen=True)
class HealthResult:
    state: HealthState
    detail: str = ""


def classify_health(status_code: int, body: str = "") -> HealthResult:
    """纯逻辑：根据 HTTP 状态码判定健康状态。

    - 200 -> READY
    - 503 -> NOT_READY
    - 其他 -> NOT_READY（带详情）
    """
    if status_code == 200:
        return HealthResult(HealthState.READY, "ready")
    if status_code == 503:
        return HealthResult(HealthState.NOT_READY, "loading")
    return HealthResult(HealthState.NOT_READY, f"HTTP {status_code}")


def probe_health(host: str, port: int, timeout: float = HEALTH_TIMEOUT_SECONDS) -> HealthResult:
    """向 /health 发一次 GET 请求。不抛异常。"""
    url = f"http://{host}:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=timeout)
        status = resp.status
        body = resp.read(256).decode("utf-8", errors="replace")
        return classify_health(status, body)
    except urllib.error.HTTPError as exc:
        return classify_health(exc.code)
    except Exception as exc:
        return HealthResult(HealthState.NOT_READY, f"连接失败: {exc}")


