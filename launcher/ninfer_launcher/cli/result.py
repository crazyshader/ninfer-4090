"""CLI 输出契约：结果对象、状态/错误码常量、单行 JSON 序列化。

本模块是 JSON 输出契约的实现（docs/01-ninfer-launcher-cli.md 第 6 节为唯一事实源）：

- 所有子命令输出同一形状：**键永远齐全**，缺省值为 null（调用方不必写
  「key in result」之类的防御代码，result.pid 拿到 None 就明确是「没有」）；
- error 是稳定标识符（调用方按它分支），中文措辞放 message；
- 序列化用 ensure_ascii=False：调用方按 UTF-8 解码。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = [
    "STATE_RUNNING",
    "STATE_STARTING",
    "STATE_STOPPED",
    "STATE_UNKNOWN",
    "HEALTH_READY",
    "HEALTH_LOADING",
    "HEALTH_UNREACHABLE",
    "OWNER_CLI",
    "OWNER_GUI",
    "OWNER_EXTERNAL",
    "SETTLE_SETTLED",
    "SETTLE_TIMEOUT",
    "SETTLE_DEGRADED",
    "ERR_NO_PRESET",
    "ERR_INVALID_PARAMS",
    "ERR_MODEL_NOT_FOUND",
    "ERR_EXE_NOT_FOUND",
    "ERR_PORT_IN_USE",
    "ERR_PORT_MISMATCH",
    "ERR_SPAWN_FAILED",
    "ERR_HEALTH_TIMEOUT",
    "ERR_CRASHED",
    "ERR_NOT_RUNNING",
    "ERR_NO_PID",
    "ERR_KILL_TIMEOUT",
    "ERR_NOT_OWNED",
    "ERR_LOCK_TIMEOUT",
    "ERR_INTERNAL",
    "CONTRACT_KEYS",
    "CliResult",
]

# -- 服务状态（与 docs 第 4 节状态表的取值一一对应） -----------------------------
STATE_RUNNING = "running"
STATE_STARTING = "starting"
STATE_STOPPED = "stopped"
#: 无法按三信息源判定（如强杀升级超时后进程仍卡在驱动里）
STATE_UNKNOWN = "unknown"

# -- /health 探测结果（core/health_probe.py 的状态映射为这三档） -----------------
HEALTH_READY = "ready"
HEALTH_LOADING = "loading"
HEALTH_UNREACHABLE = "unreachable"

# -- 实例归属（PID 文件 owner 字段的取值） --------------------------------------
OWNER_CLI = "cli"
OWNER_GUI = "gui"
OWNER_EXTERNAL = "external"

# -- 显存回落结果（对应 core/process_control.py 的 SettleOutcome） ---------------
SETTLE_SETTLED = "settled"
SETTLE_TIMEOUT = "timeout"
SETTLE_DEGRADED = "degraded"

# -- 错误码（稳定标识符；调用方按它分支，不要解析中文） ---------------------------
ERR_NO_PRESET = "no-preset"
ERR_INVALID_PARAMS = "invalid-params"
ERR_MODEL_NOT_FOUND = "model-not-found"
ERR_EXE_NOT_FOUND = "exe-not-found"
ERR_PORT_IN_USE = "port-in-use"
#: start/ensure：登记表里有活着、但端口与本次请求不同的实例（docs 12.3）
ERR_PORT_MISMATCH = "port-mismatch"
ERR_SPAWN_FAILED = "spawn-failed"
ERR_HEALTH_TIMEOUT = "health-timeout"
ERR_CRASHED = "crashed"
ERR_NOT_RUNNING = "not-running"
#: stop --force：外部实例活着但没有可定位的 PID，停不掉（docs 12.5）
ERR_NO_PID = "no-pid-to-stop"
ERR_KILL_TIMEOUT = "kill-timeout"
ERR_NOT_OWNED = "not-owned"
ERR_LOCK_TIMEOUT = "lock-timeout"
#: 逃生通道：入口层捕获到未预期异常时使用（正常业务路径不应出现）。
ERR_INTERNAL = "internal-error"

#: JSON 输出的完整键集（顺序即契约表格顺序）。
#: 测试用 set(data.keys()) == set(CONTRACT_KEYS) 校验契约：键缺失 = 契约破坏。
CONTRACT_KEYS: tuple[str, ...] = (
    "ok",
    "action",
    "state",
    "health",
    "pid",
    "port",
    "model",
    "preset",
    "owner",
    "startedAt",
    "elapsedMs",
    "waitedMs",
    "vramFreedBytes",
    "settle",
    "logPath",
    "logTail",
    "message",
    "error",
)


@dataclass
class CliResult:
    """一次 CLI 动作的结果。to_json() 输出单行 JSON，键齐全（缺省为 null）。"""

    action: str
    ok: bool = False
    state: str | None = STATE_UNKNOWN
    health: str | None = None
    pid: int | None = None
    port: int | None = None
    model: str | None = None
    preset: str | None = None
    owner: str | None = None
    started_at: float | None = None
    elapsed_ms: float = 0.0
    waited_ms: float | None = None
    vram_freed_bytes: int | None = None
    settle: str | None = None
    log_path: str | None = None
    log_tail: list[str] | None = None
    message: str = ""
    error: str | None = None

    def to_json(self) -> str:
        payload = {
            "ok": self.ok,
            "action": self.action,
            "state": self.state,
            "health": self.health,
            "pid": self.pid,
            "port": self.port,
            "model": self.model,
            "preset": self.preset,
            "owner": self.owner,
            "startedAt": self.started_at,
            "elapsedMs": self.elapsed_ms,
            "waitedMs": self.waited_ms,
            "vramFreedBytes": self.vram_freed_bytes,
            "settle": self.settle,
            "logPath": self.log_path,
            "logTail": self.log_tail,
            "message": self.message,
            "error": self.error,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
