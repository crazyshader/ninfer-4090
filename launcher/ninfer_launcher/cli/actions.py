"""四个动作的实现：status / start / stop / ensure。

每个动作的步骤严格按 docs/01-ninfer-launcher-cli.md 第 7 节；状态真值顺序
（约束 4）是：/health 探测 > OS 层进程存活（process_terminated）> PID 文件。

所有外部 I/O（probe_health / check_port / process_terminated / run_taskkill /
terminate_process_hard / Popen / MonitorService / sleep / clock / progress）都通过
关键字参数注入，默认值才是真实实现——测试用假件替换后，不 spawn 真服务、不碰
真实 GPU、不碰真实配置根（root 由调用方解析后传入，测试里是 tmp 目录）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, IO, Sequence

from ..core.health_probe import (
    HEALTH_INTERVAL_MS,
    HealthResult,
    HealthState,
    probe_health,
)
from ..core.monitor import MonitorService, make_vram_reader
from ..core.ports import PortStatus, check_port
from ..core.process_control import (
    KILL_CHECK_INTERVAL_MS,
    KILL_ESCALATION_SECONDS,
    TAIL_LINE_COUNT,
    SettleOutcome,
    VramSettle,
    VramSettleWatcher,
    process_terminated,
    run_taskkill,
    settle_vram,
    terminate_process_hard,
)
from ..core.service_status import judge_service_state
from ..params import builder, registry
from . import resolve
from .result import (
    CliResult,
    ERR_CRASHED,
    ERR_EXE_NOT_FOUND,
    ERR_HEALTH_TIMEOUT,
    ERR_INTERNAL,
    ERR_INVALID_PARAMS,
    ERR_KILL_TIMEOUT,
    ERR_LOCK_TIMEOUT,
    ERR_MODEL_NOT_FOUND,
    ERR_NO_PID,
    ERR_NO_PRESET,
    ERR_NOT_OWNED,
    ERR_NOT_RUNNING,
    ERR_PORT_IN_USE,
    ERR_PORT_MISMATCH,
    ERR_SPAWN_FAILED,
    HEALTH_LOADING,
    HEALTH_READY,
    HEALTH_UNREACHABLE,
    OWNER_CLI,
    OWNER_EXTERNAL,
    SETTLE_DEGRADED,
    SETTLE_SETTLED,
    SETTLE_TIMEOUT,
    STATE_RUNNING,
    STATE_STARTING,
    STATE_STOPPED,
    STATE_UNKNOWN,
)
from ..core.pid_file import (
    PID_SCHEMA,
    LockResult,
    LockState,
    PidEntry,
    acquire_lock,
    delete_pid_file,
    load_pid_entry,
    new_log_path,
    read_log_tail,
    release_lock,
    rotate_logs,
    write_pid_entry,
)

__all__ = [
    "action_status",
    "action_start",
    "action_stop",
    "action_ensure",
    "_default_spawn",
]

ProbeFn = Callable[[str, int], HealthResult]
#: 语义同 process_terminated：True = 进程已不在
TerminatedCheck = Callable[[int | None], bool]
SpawnFn = Callable[[str, Sequence[str], str | None, IO[bytes]], int]
ProgressFn = Callable[[str], None]


def _eprint(text: str) -> None:
    """人类可读进度信息 → stderr（约束 3：stdout 只允许那一行 JSON）。"""
    try:
        sys.stderr.write(text + chr(10))
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 —— 进度打印失败不能影响命令结果
        pass


def _health_label(result: HealthResult) -> str:
    if result.state is HealthState.READY:
        return HEALTH_READY
    if result.state is HealthState.NOT_READY and result.detail == "loading":
        return HEALTH_LOADING
    return HEALTH_UNREACHABLE


@dataclass
class _WaitOutcome:
    ok: bool
    state: str
    health: str
    waited: float
    error: str | None
    log_tail: list[str] | None = None
    timeout: float = 0.0


def _default_spawn(exe: str, argv: Sequence[str], cwd: str | None, out: IO[bytes]) -> int:
    """分离式 spawn：服务进程独立于 CLI 存活（fire-and-forget，docs 约束 2）。

    - DETACHED_PROCESS：不绑定 CLI 控制台，CLI 退出后服务不受影响；
    - CREATE_NEW_PROCESS_GROUP：独立进程组，CLI 被 Ctrl+C / 杀掉时信号不会
      波及服务；
    - stdout / stderr 合流进同一日志文件——不重定向的话，启动失败的原因会
      全部丢失（docs 第 7.2 节步骤 4）；
    - stdin=DEVNULL：服务不会被控制台输入唤醒。
    """
    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        [exe, *argv],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=out,
        creationflags=creationflags,
        close_fds=True,
    )
    return int(proc.pid)


def _quiet_close(stream: IO[bytes]) -> None:
    try:
        stream.close()
    except Exception:  # noqa: BLE001
        pass


def _wait_ready(
    *,
    host: str,
    port: int,
    timeout: float,
    pid_getter: Callable[[], int | None],
    log_path_getter: Callable[[], str | None],
    probe: ProbeFn,
    terminated_check: TerminatedCheck,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    interval_s: float = HEALTH_INTERVAL_MS / 1000.0,
    progress: ProgressFn | None = None,
) -> _WaitOutcome:
    """轮询直到 ready：每轮**先查进程存活**（进程死了立即返回 crashed，
    不让超时把真实原因掩盖掉——docs 陷阱 6），再探测 /health。

    pid_getter / log_path_getter 是回调：「等另一个实例的结果」场景里 PID 文件
    可能中途才出现，固定值不够用。
    """
    deadline = clock() + max(0.0, timeout)
    start = clock()
    last_progress = start
    while True:
        pid = pid_getter()
        if pid is not None and terminated_check(pid):
            log_path = log_path_getter()
            tail = read_log_tail(log_path, TAIL_LINE_COUNT) if log_path else []
            return _WaitOutcome(False, STATE_STOPPED, HEALTH_UNREACHABLE, clock() - start, ERR_CRASHED, tail, timeout)
        result = probe(host, port)
        label = _health_label(result)
        if label == HEALTH_READY:
            return _WaitOutcome(True, STATE_RUNNING, HEALTH_READY, clock() - start, None, None, timeout)
        if clock() >= deadline:
            log_path = log_path_getter()
            tail = read_log_tail(log_path, TAIL_LINE_COUNT) if log_path else None
            return _WaitOutcome(False, STATE_STARTING, label, clock() - start, ERR_HEALTH_TIMEOUT, tail, timeout)
        now = clock()
        if progress is not None and now - last_progress >= 5.0:
            last_progress = now
            progress("仍在等待就绪，已 %.0f 秒（上限 %.0f 秒）…" % (now - start, timeout))
        sleep(interval_s)


def _fail(action: str, code: str, message: str, **fields) -> CliResult:
    result = CliResult(action=action, ok=False, state=STATE_STOPPED, health=None, message=message, error=code)
    for key, value in fields.items():
        setattr(result, key, value)
    return result


def _result_from_wait(
    action: str,
    outcome: _WaitOutcome,
    *,
    pid: int | None,
    port: int,
    model: str | None,
    preset: str | None,
    owner: str,
    started_at: float | None,
    log_path: str | None,
) -> CliResult:
    result = CliResult(
        action=action,
        ok=outcome.ok,
        state=outcome.state,
        health=outcome.health,
        pid=pid,
        port=port,
        model=model,
        preset=preset,
        owner=owner,
        started_at=started_at,
        waited_ms=round(outcome.waited * 1000.0, 1),
        log_path=log_path,
        log_tail=outcome.log_tail,
        error=outcome.error,
    )
    if outcome.ok:
        result.message = "服务已就绪（端口 %d，耗时 %.1f 秒）" % (port, outcome.waited)
    elif outcome.error == ERR_CRASHED:
        result.message = "服务进程在加载期间退出，原因见 logTail（完整日志见 logPath）"
    else:
        result.message = "服务在 %d 秒内未就绪（进程仍存活，可再次 ensure 继续等待；日志见 logPath）" % int(outcome.timeout)
    return result


def _wait_for_other(
    action: str,
    *,
    root: Path,
    host: str,
    port: int,
    timeout: float,
    probe: ProbeFn,
    terminated_check: TerminatedCheck,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    progress: ProgressFn,
) -> CliResult:
    """另一个 CLI 正持锁启动：等它的结果（start 的降级分支 / ensure 的第 2 步）。"""
    progress("检测到另一个实例正在启动服务，等待其结果…")

    def pid_getter() -> int | None:
        entry = load_pid_entry(root, terminated_check=terminated_check)
        return entry.pid if entry is not None else None

    def log_path_getter() -> str | None:
        entry = load_pid_entry(root, terminated_check=terminated_check)
        return entry.log_path if entry is not None else None

    outcome = _wait_ready(
        host=host,
        port=port,
        timeout=timeout,
        pid_getter=pid_getter,
        log_path_getter=log_path_getter,
        probe=probe,
        terminated_check=terminated_check,
        sleep=sleep,
        clock=clock,
        progress=progress,
    )
    if outcome.ok:
        entry = load_pid_entry(root, terminated_check=terminated_check)
        return CliResult(
            action=action,
            ok=True,
            state=STATE_RUNNING,
            health=HEALTH_READY,
            pid=entry.pid if entry is not None else None,
            port=port,
            model=entry.model if entry is not None else None,
            preset=entry.preset if entry is not None else None,
            owner=(entry.owner if entry is not None and entry.owner else OWNER_EXTERNAL),
            started_at=entry.started_at if entry is not None else None,
            log_path=entry.log_path if entry is not None else None,
            waited_ms=round(outcome.waited * 1000.0, 1),
            message="服务已就绪（由另一个实例启动，端口 %d）" % port,
        )
    return CliResult(
        action=action,
        ok=False,
        state=STATE_STARTING,
        health=outcome.health,
        port=port,
        waited_ms=round(outcome.waited * 1000.0, 1),
        log_tail=outcome.log_tail,
        error=ERR_LOCK_TIMEOUT,
        message="等待另一个实例的启动结果 %.0f 秒未就绪：该实例可能已失败，可重试 ensure" % timeout,
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def action_status(
    *,
    root: Path,
    preset_name: str | None = None,
    host: str = "127.0.0.1",
    tail: int = 0,
    probe: ProbeFn = probe_health,
    terminated_check: TerminatedCheck = process_terminated,
) -> CliResult:
    """查询服务状态，无副作用（唯一例外：清理陈旧 PID 文件）。

    端口来源（docs 7.1）：entry.port → --preset 的预设端口 → settings.params 端口 → 8080。
    """
    tail = max(0, min(int(tail), 200))
    entry = load_pid_entry(root, terminated_check=terminated_check)
    port = entry.port if (entry is not None and entry.port) else resolve.resolve_port(root, preset_name)
    health_result = probe(host, port)
    label = _health_label(health_result)
    alive = entry is not None
    state, owner = judge_service_state(health_result, entry.owner if entry is not None else None, alive)
    result = CliResult(
        action="status",
        ok=True,
        state=state.value,
        health=label,
        pid=entry.pid if entry is not None else None,
        port=port,
        model=entry.model if entry is not None else None,
        # preset 是「实例」的预设名（契约字段）：没有实例时必须是 null，
        # 不能回显调用方传的 --preset（那只是查询参数，不是实例的属性）
        preset=entry.preset if entry is not None else None,
        owner=owner,
        started_at=entry.started_at if entry is not None else None,
        log_path=entry.log_path if entry is not None else None,
    )
    if state == STATE_RUNNING:
        if entry is not None:
            result.message = "服务运行中（PID %d，端口 %d，owner: %s）" % (entry.pid, port, entry.owner)
        else:
            result.message = "服务运行中（端口 %d，非本工具启动的实例）" % port
    elif state == STATE_STARTING:
        detail = "正在加载权重" if label == HEALTH_LOADING else "进程存活，尚未监听"
        result.message = "服务启动中：%s（端口 %d）" % (detail, port)
    else:
        result.message = "服务未运行"
    if tail > 0 and entry is not None and entry.log_path:
        result.log_tail = read_log_tail(entry.log_path, tail)
    return result


# ---------------------------------------------------------------------------
# start / ensure 共用的启动流程（锁已持有）
# ---------------------------------------------------------------------------

def _start_flow(
    *,
    action: str,
    root: Path,
    preset_name: str | None,
    exe_path: str | None,
    host: str,
    timeout: float,
    probe: ProbeFn,
    check_port_fn: Callable[..., object],
    terminated_check: TerminatedCheck,
    spawn: SpawnFn,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    progress: ProgressFn,
) -> CliResult:
    # a. 预设与参数
    try:
        plan = resolve.resolve_launch(root, preset_name=preset_name, exe_arg=exe_path)
    except resolve.ResolveError as exc:
        return _fail(action, exc.code, exc.message)
    # b. 参数校验（registry 是唯一事实源）
    errors = registry.validate_values(plan.values)
    if errors:
        return _fail(action, ERR_INVALID_PARAMS, "参数校验未通过：" + "；".join(errors))
    # c. 模型文件（isfile，不是 exists——目录也算「没找到」）
    if not plan.model or not os.path.isfile(plan.model):
        return _fail(action, ERR_MODEL_NOT_FOUND, "模型文件不存在：%s" % (plan.model or "（预设未设置）"))
    # d. exe
    if not plan.exe or not os.path.isfile(plan.exe):
        return _fail(action, ERR_EXE_NOT_FOUND, "服务可执行文件不存在：%s" % (plan.exe or "（未能解析）"))
    port = plan.port or resolve.resolve_port(root, preset_name)

    # e. 已就绪 → 幂等直接返回（顺序必须是 health 先于端口：docs 陷阱 5）
    label = _health_label(probe(host, port))
    if label == HEALTH_READY:
        entry = load_pid_entry(root, terminated_check=terminated_check)
        if entry is not None:
            return CliResult(
                action=action, ok=True, state=STATE_RUNNING, health=HEALTH_READY,
                pid=entry.pid, port=port, model=entry.model, preset=entry.preset,
                owner=entry.owner, started_at=entry.started_at, log_path=entry.log_path,
                message="服务已就绪（端口 %d），未重复启动" % port,
            )
        return CliResult(
            action=action, ok=True, state=STATE_RUNNING, health=HEALTH_READY,
            port=port, model=plan.model, preset=plan.preset_name, owner=OWNER_EXTERNAL,
            message="服务已就绪（端口 %d，非本工具启动的实例），未重复启动" % port,
        )

    # e2. 登记表里已有存活实例（load_pid_entry 已做 OS 级存活校验）：
    # - 端口一致（或登记表没记端口）→ 不重复 spawn，等它就绪（start / ensure 共用，docs 12.7）；
    # - 端口不一致 → 旧端口的实例还活着，再拉起新的一份会把两份权重压上同一张卡
    #   （docs 12.3）→ port-mismatch，交给用户先 stop 再 start，绝不擅自重启。
    # 拦截范围是「任何归属」的存活登记实例：归属不明的实例（如登记表损坏回落
    # external）同样不该被静默地再拉一个。
    entry = load_pid_entry(root, terminated_check=terminated_check)
    if entry is not None and entry.port is not None and entry.port != port:
        return _fail(
            action,
            ERR_PORT_MISMATCH,
            "已有服务实例（PID %d，端口 %d）在运行，而本次配置要求端口 %d；再拉起一个"
            "实例会在同一张卡上多占一份权重。请先 stop 再启动"
            % (entry.pid, entry.port, port),
            state=STATE_STARTING,
            health=label,
            port=port,
            pid=entry.pid,
            model=entry.model,
            preset=entry.preset,
            owner=entry.owner,
            started_at=entry.started_at,
            log_path=entry.log_path,
        )
    if entry is not None and (entry.port is None or entry.port == port):
        progress("检测到已有实例（PID %d）正在加载，等待其就绪…" % entry.pid)
        outcome = _wait_ready(
            host=host, port=port, timeout=timeout,
            pid_getter=lambda: entry.pid,
            log_path_getter=lambda: entry.log_path,
            probe=probe, terminated_check=terminated_check, sleep=sleep, clock=clock, progress=progress,
        )
        return _result_from_wait(
            action, outcome, pid=entry.pid, port=port,
            model=entry.model, preset=entry.preset, owner=entry.owner,
            started_at=entry.started_at, log_path=entry.log_path,
        )

    # f. 端口占用（能走到这里：health 未就绪、也没有本工具注册的存活实例）
    check = check_port_fn(port, host)
    if check.status is PortStatus.IN_USE:
        return _fail(action, ERR_PORT_IN_USE, "端口 %d 被其他进程占用（且不是 ninfer 服务）：%s" % (port, check.message))

    # g. spawn + 写登记表 + 等就绪
    argv = builder.build(plan.values)
    log_path = new_log_path(root)
    progress("启动服务：%s %s（日志：%s）" % (plan.exe, " ".join(argv), log_path))
    try:
        log_file = open(log_path, "wb")
    except OSError as exc:
        return _fail(action, ERR_SPAWN_FAILED, "无法创建日志文件 %s：%s" % (log_path, exc))
    started_at = time.time()
    try:
        try:
            pid = spawn(plan.exe, argv, os.path.dirname(plan.exe) or None, log_file)
        except Exception as exc:  # noqa: BLE001 —— Popen 的 OSError 及子类
            _quiet_close(log_file)
            return _fail(action, ERR_SPAWN_FAILED, "服务进程启动失败：%s" % exc)
    finally:
        _quiet_close(log_file)
    try:
        write_pid_entry(
            root,
            PidEntry(
                schema=PID_SCHEMA, pid=pid, port=port, exe=plan.exe, args=tuple(argv),
                model=plan.model, preset=plan.preset_name, owner=OWNER_CLI,
                started_at=started_at, log_path=str(log_path),
            ),
        )
    except OSError as exc:
        progress("警告：进程登记表写入失败（将影响 stop / ensure 定位本实例）：%s" % exc)
    progress("已拉起服务（PID %d），等待就绪（上限 %.0f 秒）…" % (pid, timeout))

    outcome = _wait_ready(
        host=host, port=port, timeout=timeout,
        pid_getter=lambda: pid,
        log_path_getter=lambda: str(log_path),
        probe=probe, terminated_check=terminated_check, sleep=sleep, clock=clock, progress=progress,
    )
    result = _result_from_wait(
        action, outcome, pid=pid, port=port,
        model=plan.model, preset=plan.preset_name, owner=OWNER_CLI,
        started_at=started_at, log_path=str(log_path),
    )
    rotate_logs(root)
    return result


def action_start(
    *,
    root: Path,
    preset_name: str | None = None,
    exe_path: str | None = None,
    host: str = "127.0.0.1",
    timeout: float = 600.0,
    probe: ProbeFn = probe_health,
    check_port_fn: Callable[..., object] = check_port,
    terminated_check: TerminatedCheck = process_terminated,
    spawn: SpawnFn = _default_spawn,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    progress: ProgressFn = _eprint,
) -> CliResult:
    """拉起服务并等到就绪（幂等）；fire-and-forget：CLI 自己退出，服务留在后台。"""
    lock = acquire_lock(root, terminated_check=terminated_check)
    if lock.state is LockState.HELD_BY_OTHER:
        return _wait_for_other(
            "start", root=root, host=host,
            port=resolve.resolve_port(root, preset_name), timeout=timeout,
            probe=probe, terminated_check=terminated_check, sleep=sleep, clock=clock, progress=progress,
        )
    if lock.state is LockState.FAILED:
        return _fail("start", ERR_INTERNAL, "无法创建启动锁文件：%s" % lock.path)
    try:
        return _start_flow(
            action="start", root=root, preset_name=preset_name, exe_path=exe_path, host=host,
            timeout=timeout, probe=probe, check_port_fn=check_port_fn,
            terminated_check=terminated_check, spawn=spawn, sleep=sleep, clock=clock, progress=progress,
        )
    finally:
        release_lock(lock)


def action_ensure(
    *,
    root: Path,
    preset_name: str | None = None,
    exe_path: str | None = None,
    host: str = "127.0.0.1",
    timeout: float = 600.0,
    probe: ProbeFn = probe_health,
    check_port_fn: Callable[..., object] = check_port,
    terminated_check: TerminatedCheck = process_terminated,
    spawn: SpawnFn = _default_spawn,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    progress: ProgressFn = _eprint,
) -> CliResult:
    """幂等地「保证服务可用」——插件唯一调用的入口（docs 7.4）。

    1. 快速路径：一次 /health 探测，ready 就直接返回（不读 PID 文件、不抢锁）；
    2. 抢锁：拿到 → 完整 start 流程；拿不到且持锁者活着 → 等它的结果；
    3. 已有存活进程（正在加载）→ 不重复 spawn，直接等就绪。
    """
    port = resolve.resolve_port(root, preset_name)
    label = _health_label(probe(host, port))
    if label == HEALTH_READY:
        return CliResult(
            action="ensure", ok=True, state=STATE_RUNNING, health=HEALTH_READY, port=port,
            # 快速路径没读登记表，无法断言归属：未知就该是 null，不能谎称 external（docs 12.2）
            owner=None, message="服务运行中（端口 %d），无需操作" % port,
        )
    lock = acquire_lock(root, terminated_check=terminated_check)
    if lock.state is LockState.HELD_BY_OTHER:
        return _wait_for_other(
            "ensure", root=root, host=host, port=port, timeout=timeout,
            probe=probe, terminated_check=terminated_check, sleep=sleep, clock=clock, progress=progress,
        )
    if lock.state is LockState.FAILED:
        return _fail("ensure", ERR_INTERNAL, "无法创建启动锁文件：%s" % lock.path)
    try:
        return _start_flow(
            action="ensure", root=root, preset_name=preset_name, exe_path=exe_path, host=host,
            timeout=timeout, probe=probe, check_port_fn=check_port_fn,
            terminated_check=terminated_check, spawn=spawn, sleep=sleep, clock=clock, progress=progress,
        )
    finally:
        release_lock(lock)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def action_stop(
    *,
    root: Path,
    preset_name: str | None = None,
    host: str = "127.0.0.1",
    force: bool = False,
    probe: ProbeFn = probe_health,
    terminated_check: TerminatedCheck = process_terminated,
    killer: Callable[[int], tuple[bool, str]] = run_taskkill,
    hard_terminator: Callable[[int], tuple[bool, str]] = terminate_process_hard,
    monitor_factory: Callable[[], Any] = MonitorService,
    make_reader: Callable[[Any], Callable[[], int | None]] = make_vram_reader,
    settle_wait: Callable[..., VramSettle] = settle_vram,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    progress: ProgressFn = _eprint,
) -> CliResult:
    """停止服务并等显存回落（幂等）。

    杀进程链与 GUI 的 stop_and_wait 同源（core/process_control 的纯函数）：
    阶段 1 taskkill 杀进程树（CLI 没有 QProcess 句柄，跳过 GUI 的阶段 0 terminate）；
    阶段 2 仍活着（CUDA 进程在 GPU 驱动临界区拒绝终止，实测常见）→ 升级强杀，
    每 KILL_CHECK_INTERVAL_MS 重发 Win32 TerminateProcess 并以 process_terminated
    校验，上限 KILL_ESCALATION_SECONDS；用尽仍活着 → kill-timeout + 手动处理指引。

    显存回落基线在杀进程**之前**取（watcher.begin），之后才把计时器重启为
    「从回落等待开始计」——与 GUI 的 _run_settle_sync 行为一致。
    """
    entry = load_pid_entry(root, terminated_check=terminated_check)
    port = entry.port if (entry is not None and entry.port) else resolve.resolve_port(root, preset_name)
    label = _health_label(probe(host, port))

    if entry is None:
        if label in (HEALTH_READY, HEALTH_LOADING):
            state = STATE_RUNNING if label == HEALTH_READY else STATE_STARTING
            if not force:
                return CliResult(
                    action="stop", ok=True, state=state, health=label, port=port, owner=OWNER_EXTERNAL,
                    error=ERR_NOT_OWNED,
                    message="端口 %d 有外部服务实例在运行（非本工具启动），已跳过；"
                            "本工具不通过端口反查 PID，无法代停" % port,
                )
            # --force 也定位不到 PID：进程还活着但我们停不掉，必须报 ok:false——
            # 报成功会让调用方把「没动」误判成「已停」（docs 12.5）
            return CliResult(
                action="stop", ok=False, state=state, health=label, port=port, owner=OWNER_EXTERNAL,
                error=ERR_NO_PID,
                message="外部实例没有 PID 可定位（本工具不从端口反查 PID），进程仍在运行、未停止，请手动结束该进程",
            )
        return CliResult(
            action="stop", ok=True, state=STATE_STOPPED, health=label, port=port,
            error=ERR_NOT_RUNNING, message="服务未运行",
        )

    if entry.owner != OWNER_CLI and not force:
        return CliResult(
            action="stop", ok=True, state=STATE_RUNNING if label == HEALTH_READY else STATE_STARTING,
            health=label, pid=entry.pid, port=port, model=entry.model, preset=entry.preset,
            owner=entry.owner, started_at=entry.started_at, log_path=entry.log_path,
            error=ERR_NOT_OWNED,
            message="该实例非本工具启动（owner: %s），已跳过；如确认要停，使用 --force" % entry.owner,
        )

    pid = entry.pid
    progress("正在停止服务（PID %d）…" % pid)
    # 杀进程前的最后探测仅作记录；最终 health 以杀完之后的复探为准（见下方 finally 后）
    monitor = monitor_factory()
    try:
        reader = make_reader(monitor)
        watcher = VramSettleWatcher(reader)
        watcher.begin()  # 基线：服务仍活着时的显存读数
        killer(pid)  # 阶段 1：杀进程树
        alive = not terminated_check(pid)
        if alive:
            deadline = clock() + KILL_ESCALATION_SECONDS
            check_s = KILL_CHECK_INTERVAL_MS / 1000.0
            progress("taskkill 后进程仍存活（GPU 驱动态常见），升级强杀并等待 OS 确认（最多 %.0f 秒）…" % KILL_ESCALATION_SECONDS)
            while alive and clock() < deadline:
                hard_terminator(pid)
                sleep(check_s)
                alive = not terminated_check(pid)

        if alive:
            settle_label = None
            freed = None
            ok, state, error = False, STATE_UNKNOWN, ERR_KILL_TIMEOUT
            message = (
                "⚠ %.0f 秒强杀后服务器进程（PID %d）仍未退出：请在任务管理器中手动结束 "
                "ninfer-serve.exe，该进程仍会占用 GPU 显存" % (KILL_ESCALATION_SECONDS, pid)
            )
            log_path = entry.log_path
        else:
            watcher.restart_timer()
            settle = settle_wait(watcher=watcher, sleep=sleep)
            settle_map = {
                SettleOutcome.SETTLED: SETTLE_SETTLED,
                SettleOutcome.TIMEOUT: SETTLE_TIMEOUT,
                SettleOutcome.DEGRADED: SETTLE_DEGRADED,
            }
            settle_label = settle_map[settle.outcome]
            freed = None
            if settle.baseline_bytes is not None and settle.last_bytes is not None:
                freed = max(0, settle.baseline_bytes - settle.last_bytes)
            delete_pid_file(root)
            ok, state, error = True, STATE_STOPPED, None
            message = "服务已停止（PID %d）；显存回落：%s" % (pid, settle.message())
            log_path = entry.log_path
    finally:
        try:
            monitor.shutdown()
        except Exception:  # noqa: BLE001
            pass

    # 杀完之后的 /health 复探：正常停止后服务已死 → unreachable；若强杀超时、
    # 进程仍在服务，则如实报告 ready（state 为 unknown），不假装它已经没了。
    final_health = _health_label(probe(host, port))
    return CliResult(
        action="stop", ok=ok, state=state, health=final_health,
        pid=pid, port=port, model=entry.model, preset=entry.preset,
        owner=entry.owner, started_at=entry.started_at, log_path=log_path,
        vram_freed_bytes=freed, settle=settle_label, message=message, error=error,
    )
