"""端口占用检测与顺延建议。"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from enum import Enum

__all__ = ["PortStatus", "PortCheck", "check_port", "suggest_port"]

#: 顺延建议的最大尝试次数
SUGGEST_LIMIT = 100


class PortStatus(Enum):
    FREE = "free"
    IN_USE = "in_use"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PortCheck:
    port: int
    status: PortStatus
    message: str = ""


def check_port(port: int, host: str = "127.0.0.1") -> PortCheck:
    """检查端口是否可用。使用 SO_EXCLUSIVEADDRUSE 探测。"""
    if port < 1 or port > 65535:
        return PortCheck(port, PortStatus.UNKNOWN, f"端口 {port} 不在合法范围 1~65535")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
        sock.close()
        return PortCheck(port, PortStatus.FREE)
    except OSError:
        return PortCheck(port, PortStatus.IN_USE, f"端口 {port} 已被占用")
    except Exception as exc:
        return PortCheck(port, PortStatus.UNKNOWN, f"端口检查失败：{exc}")


def suggest_port(preferred: int, host: str = "127.0.0.1") -> int | None:
    """从 preferred+1 开始扫描，找到第一个可用端口。"""
    for offset in range(1, SUGGEST_LIMIT + 1):
        candidate = preferred + offset
        if candidate > 65535:
            return None
        check = check_port(candidate, host)
        if check.status is PortStatus.FREE:
            return candidate
    return None
