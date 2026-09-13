"""服务状态观测（GUI 与 CLI 共享）：纯逻辑，零 Qt 依赖。

真值顺序（docs/01-ninfer-launcher-cli.md 约束 4）：

    /health 探测 > OS 层进程存活（process_terminated）> PID 登记表（core/pid_file）

GUI 主窗口在「自身进程处于停止态」时每秒调用 :func:`observe_service_state` 对账
一次：识别 CLI / 外部启动、本 GUI 并不知情的服务，把四个控制按钮同步到对应状态
（不再出现「后台服务在跑、GUI 按钮却停在停止态点不动」的失同步）。CLI 的 status
动作与 GUI 对账使用同一张判定表（:func:`judge_service_state`），保证同一场景下
两端判出同一状态。

纪律：本模块是**纯只读**观察者——只用 read_pid_entry_raw（不校验删除、不动文件），
陈旧登记表的清理由 CLI 的 status / stop 负责（load_pid_entry 的语义）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .health_probe import HealthResult, HealthState, probe_health
from .pid_file import PidEntry, read_pid_entry_raw
from .process_control import ServerState, process_terminated

__all__ = [
    "EXTERNAL_OWNER",
    "ServiceObservation",
    "judge_service_state",
    "observe_service_state",
]

#: 登记表缺失 / 归属不明时的兜底 owner。必须与 cli/result.OWNER_EXTERNAL 保持一致
#: （core 层不得反向 import cli 包，故此处定义同一字符串常量，两侧注释互相引用）。
EXTERNAL_OWNER = "external"


def judge_service_state(
    result: HealthResult,
    entry_owner: str | None,
    entry_alive: bool,
) -> tuple[ServerState, str | None]:
    """约束 4 状态判定表（GUI 与 CLI 共用的唯一事实源）。

    参数：
    - result：/health 探测结果（READY / NOT_READY(loading) / NOT_READY(其他)）；
    - entry_owner：PID 登记表记录的 owner（无表则为 None）；
    - entry_alive：登记表存在**且**进程在 OS 层确认存活。

    判定：
    - READY → RUNNING（有存活实例时取登记表 owner，否则 external）；
    - 实例存活，或 503 加载中 → STARTING（owner 同上）；
    - 其余 → STOPPED（owner 为 None）。
    """
    if result.state is HealthState.READY:
        owner = entry_owner if (entry_alive and entry_owner) else EXTERNAL_OWNER
        return ServerState.RUNNING, owner
    loading = result.state is HealthState.NOT_READY and result.detail == "loading"
    if entry_alive or loading:
        owner = entry_owner if (entry_alive and entry_owner) else EXTERNAL_OWNER
        return ServerState.STARTING, owner
    return ServerState.STOPPED, None


@dataclass(frozen=True)
class ServiceObservation:
    """一次观测结果：状态 + 服务实际端口 + 归属（GUI 对账消费本结构）。"""

    state: ServerState
    port: int
    #: 仅当登记表存在且进程存活时给出（GUI 据此定位可停止的 PID）
    pid: int | None = None
    owner: str | None = None
    health: HealthResult | None = None


def observe_service_state(
    *,
    host: str,
    port: int,
    root: Path,
    probe: Callable[[str, int], HealthResult] = probe_health,
    terminated_check: Callable[[int | None], bool] = process_terminated,
    entry_reader: Callable[[Path], PidEntry | None] = read_pid_entry_raw,
) -> ServiceObservation:
    """观测一次服务实况（只读，无副作用：不删文件、不杀进程、不抢锁）。

    端口解析与 CLI status 相同：登记表自带的 port 优先，其次调用方传入的端口
    （GUI 传当前设置端口；CLI 层再往下还有预设 / 8080 的回退链）。

    存活判定：登记表存在时按 terminated_check（OS 层真值）本地判定，
    **不**像 load_pid_entry 那样顺带删除陈旧文件——GUI 是纯观察者。
    """
    entry = entry_reader(root)
    probe_port = int(entry.port) if (entry is not None and entry.port) else int(port)
    result = probe(host, probe_port)
    entry_alive = entry is not None and not terminated_check(entry.pid)
    state, owner = judge_service_state(
        result, entry.owner if entry is not None else None, entry_alive
    )
    return ServiceObservation(
        state=state,
        port=probe_port,
        pid=entry.pid if entry_alive else None,
        owner=owner,
        health=result,
    )
