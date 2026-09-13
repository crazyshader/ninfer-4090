"""ninfer-serve 子进程管理：QProcess 封装、四态状态机、停止流程（含强杀升级）、显存回落等待。

本模块自 2026-09 起分为两半（为支持零 Qt 依赖的 CLI 入口，见 docs/01-ninfer-launcher-cli.md）：

1. 纯逻辑与纯编排（decode_output / LineAssembler / ExitInfo / 状态机 /
   run_taskkill / terminate_process_hard / process_terminated / VramSettleWatcher /
   settle_vram / 各常量）位于 core/process_control.py——零 Qt 依赖，GUI 与 CLI
   共享同一份实现；
2. 本模块只保留 QProcess 封装 ServerProcess（依赖事件循环），并从 process_control
   原样重导出全部纯函数与常量：所有既有的 core.process 导入路径（含测试）继续有效，
   GUI 停止链行为不变——CLI 只调用这些纯函数，从不修改它们。"""

from __future__ import annotations

import os
import time
from collections import deque
from typing import Callable, Sequence

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .process_control import (
    TERMINATE_GRACE_SECONDS,
    VRAM_SETTLE_TIMEOUT_SECONDS,
    VRAM_POLL_INTERVAL_SECONDS,
    VRAM_FALLBACK_WAIT_SECONDS,
    VRAM_SETTLE_DROP_BYTES,
    TAIL_LINE_COUNT,
    LINE_BUFFER_LIMIT,
    ServerState,
    LogSource,
    LogLine,
    ExitInfo,
    SettleOutcome,
    VramSettle,
    ALLOWED_TRANSITIONS,
    can_transition,
    decode_output,
    LineAssembler,
    format_log_line,
    KILL_ESCALATION_SECONDS,
    KILL_CHECK_INTERVAL_MS,
    taskkill_command,
    run_taskkill,
    terminate_process_hard,
    process_terminated,
    VramSettleWatcher,
    settle_vram,
    stop_external_sync,
    VramReader,
)

__all__ = [
    "TERMINATE_GRACE_SECONDS",
    "VRAM_SETTLE_TIMEOUT_SECONDS",
    "VRAM_POLL_INTERVAL_SECONDS",
    "VRAM_FALLBACK_WAIT_SECONDS",
    "VRAM_SETTLE_DROP_BYTES",
    "TAIL_LINE_COUNT",
    "LINE_BUFFER_LIMIT",
    "ServerState",
    "LogSource",
    "LogLine",
    "ExitInfo",
    "SettleOutcome",
    "VramSettle",
    "ALLOWED_TRANSITIONS",
    "can_transition",
    "decode_output",
    "LineAssembler",
    "format_log_line",
    "KILL_ESCALATION_SECONDS",
    "KILL_CHECK_INTERVAL_MS",
    "taskkill_command",
    "run_taskkill",
    "terminate_process_hard",
    "process_terminated",
    "VramSettleWatcher",
    "settle_vram",
    "stop_external_sync",
    "ServerProcess",
    "ExternalProcessStopper",
]


# ---------------------------------------------------------------------------
# QProcess 封装
# ---------------------------------------------------------------------------

Killer = Callable[[int], "tuple[bool, str]"]


class ServerProcess(QObject):
    """ninfer-serve 子进程封装：四态状态机 + 分通道读取 + 停止流程。"""

    state_changed = Signal(object)
    log_line = Signal(object)
    exited = Signal(object)
    stop_finished = Signal(object)

    def __init__(
        self,
        *,
        vram_reader: VramReader | None = None,
        killer: Killer | None = None,
        hard_terminator: Callable[[int], tuple[bool, str]] | None = None,
        terminated_check: Callable[[int | None], bool] | None = None,
        terminate_grace: float = TERMINATE_GRACE_SECONDS,
        kill_escalation: float = KILL_ESCALATION_SECONDS,
        kill_check_interval_ms: int = KILL_CHECK_INTERVAL_MS,
        settle_timeout: float = VRAM_SETTLE_TIMEOUT_SECONDS,
        settle_interval: float = VRAM_POLL_INTERVAL_SECONDS,
        settle_fallback_wait: float = VRAM_FALLBACK_WAIT_SECONDS,
        settle_drop_bytes: int = VRAM_SETTLE_DROP_BYTES,
        tail_lines: int = TAIL_LINE_COUNT,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._vram_reader = vram_reader
        self._killer = killer or run_taskkill
        self._hard_terminator = hard_terminator or terminate_process_hard
        self._terminated_check = terminated_check or process_terminated
        self._terminate_grace = terminate_grace
        self._kill_escalation = kill_escalation
        self._kill_check_interval_ms = kill_check_interval_ms
        self._settle_timeout = settle_timeout
        self._settle_interval = settle_interval
        self._settle_fallback_wait = settle_fallback_wait
        self._settle_drop_bytes = settle_drop_bytes
        self._clock = clock

        self._state = ServerState.STOPPED
        self._last_exit: ExitInfo | None = None
        self._command: tuple[str, tuple[str, ...]] | None = None
        self._last_pid: int | None = None
        self._stop_requested = False
        self._killed = False
        self._sync_stop = False
        self._exit_seen = False
        self._settle_in_flight = False
        self._kill_deadline = 0.0
        self._settle: VramSettle | None = None
        self._watcher: VramSettleWatcher | None = None

        self._out_assembler = LineAssembler()
        self._err_assembler = LineAssembler()
        self._tail: deque[LogLine] = deque(maxlen=max(1, int(tail_lines)))

        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._proc.readyReadStandardOutput.connect(self._on_stdout)
        self._proc.readyReadStandardError.connect(self._on_stderr)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)

        self._grace_timer = QTimer(self)
        self._grace_timer.setSingleShot(True)
        self._grace_timer.timeout.connect(self._on_grace_timeout)

        self._settle_timer = QTimer(self)
        self._settle_timer.setInterval(max(1, int(self._settle_interval * 1000)))
        self._settle_timer.timeout.connect(self._on_settle_tick)

        # 强杀升级阶段的周期校验器：异步停止时，taskkill 后进程仍可能因 GPU 驱动
        # 状态迟迟不退出，每轮重发强杀并以 OS 真值验证，直到确认死亡或超时。
        self._kill_verify_timer = QTimer(self)
        self._kill_verify_timer.setInterval(max(1, int(kill_check_interval_ms)))
        self._kill_verify_timer.timeout.connect(self._on_kill_verify_tick)

    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def last_exit(self) -> ExitInfo | None:
        return self._last_exit

    @property
    def last_settle(self) -> VramSettle | None:
        return self._settle

    @property
    def command(self) -> tuple[str, tuple[str, ...]] | None:
        return self._command

    @property
    def pid(self) -> int | None:
        pid = int(self._proc.processId())
        return pid or None

    @property
    def last_pid(self) -> int | None:
        """最近一次已确认启动的子进程 PID（进程退出后依然有效，供善后强杀/核对）。"""
        return self._last_pid

    def is_process_alive(self) -> bool:
        return self._proc.state() is not QProcess.ProcessState.NotRunning

    def is_alive(self) -> bool:
        """OS 层真值：子进程是否仍存活（OpenProcess 探测，见 :func:`process_terminated`）。

        Windows 上 CUDA 进程（ninfer-serve）在驱动临界区会拒绝/推迟终止，Qt 的
        状态缓存也可能与 OS 实况脱节；一切「进程已消失」的判定（关闭流程、显存
        回落、状态复位）都必须以本方法为准，而不是 Qt 缓存或单次 kill 的返回值。
        """
        pid = self.pid or self._last_pid
        if pid is None:
            return self.is_process_alive()
        # _terminated_check 语义 True = 进程已不在（与 process_terminated 一致），
        # 必须取反才是「是否存活」。此前直接 return 未取反：进程活着时 is_alive() 误判为
        # False，_begin_stop 的 `if not self.is_alive()` 走「子进程已不在运行」分支，
        # 跳过全部杀进程步骤（terminate→taskkill→强杀），进程永远不会被停止。
        # 该回归源于 2026-09 process_terminated 返回值修复（见 process_control.py 注释）
        # 漏改了此处依赖方。pid is None 分支（is_process_alive）语义为 True=活，与之保持一致。
        return not self._terminated_check(pid)

    def tail_error_lines(self) -> tuple[str, ...]:
        server_lines = [line for line in self._tail if line.source.from_server]
        stderr_lines = [line for line in server_lines if line.source is LogSource.STDERR]
        chosen = stderr_lines or server_lines
        return tuple(line.formatted for line in chosen)

    def abort_reason(self) -> str | None:
        if self.is_alive():
            return None
        if self._last_exit is not None:
            return self._last_exit.abort_message()
        if self._state is ServerState.STOPPING:
            return "正在停止服务器"
        return "子进程已不在运行"

    def start(
        self,
        program: str,
        arguments: Sequence[str] = (),
        *,
        working_directory: str | None = None,
    ) -> bool:
        if self._state is not ServerState.STOPPED:
            self._emit_launcher(f"当前状态为「{self._state.label}」，忽略启动请求")
            return False
        if not program or not os.path.isfile(program):
            self._emit_launcher(f"启动失败：找不到可执行文件 {program!r}")
            self._last_exit = ExitInfo(failed_to_start=True, state_before=ServerState.STOPPED)
            self.exited.emit(self._last_exit)
            return False

        args = tuple(str(item) for item in arguments)
        self._command = (program, args)
        self._last_exit = None
        self._settle = None
        self._watcher = None
        self._last_pid = None
        self._stop_requested = False
        self._killed = False
        self._sync_stop = False
        self._exit_seen = False
        self._settle_in_flight = False
        self._kill_deadline = 0.0
        self._out_assembler = LineAssembler()
        self._err_assembler = LineAssembler()
        self._tail.clear()

        workdir = working_directory or os.path.dirname(os.path.abspath(program))
        if workdir and os.path.isdir(workdir):
            self._proc.setWorkingDirectory(workdir)

        self._set_state(ServerState.STARTING)
        self._emit_launcher(f"启动服务器：{program} {' '.join(args)}".rstrip())
        self._proc.start(program, list(args))
        # Windows 上 CreateProcess 同步返回：start() 返回后即可取到 PID。记住它——
        # 进程退出后 Qt 侧 PID 可能失效，善后强杀与 OS 核对仍要认这个号。
        pid = int(self._proc.processId())
        self._last_pid = pid or None
        return True

    def mark_ready(self) -> bool:
        if self._state is not ServerState.STARTING:
            return False
        return self._set_state(ServerState.RUNNING)

    def stop(self) -> bool:
        return self._begin_stop(sync=False)

    def stop_and_wait(self) -> VramSettle | None:
        """同步停止并等待退出（启动器关闭流程使用），逐级升级直到 OS 确认进程消失。

        阶段 1：terminate + 宽限期（进程通常在宽限期内自行退出）；
        阶段 2：仍未退出 → taskkill /T /F 杀进程树；
        阶段 3：仍存活（CUDA 进程在 GPU 驱动态会拒绝终止，实测确认）→ 强杀升级：
        每隔 :data:`KILL_CHECK_INTERVAL_MS` 毫秒重发 Win32 TerminateProcess 并以
        :meth:`is_alive`（OpenProcess 探测）验证，直到进程真死或升级时限
        （:data:`KILL_ESCALATION_SECONDS`）用尽；时限到达仍存活时打显著警告——
        宁可带着警告退出，也不让启动器静默留下占着 23 GB 显存的残留进程。

        全程以 OS 层进程存活为「进程已消失」的唯一依据：既不信任 Qt 的状态缓存，
        也不信任任何单次 kill 调用的返回值。
        """
        if self._state is ServerState.STOPPED and not self.is_alive():
            return None
        if self._state is not ServerState.STOPPING:
            self._begin_stop(sync=True)
        else:
            self._sync_stop = True
            self._grace_timer.stop()
            self._kill_verify_timer.stop()

        if self.is_alive():
            grace_ms = max(0, int(self._terminate_grace * 1000))
            self._proc.waitForFinished(grace_ms)
        if self.is_alive():
            self._emit_launcher(f"terminate 后 {self._terminate_grace:.0f} 秒仍未退出，回落 taskkill（进程树）")
            self._kill_tree()
        if self.is_alive():
            deadline = self._clock() + self._kill_escalation
            self._emit_launcher(
                f"taskkill 之后进程仍未退出（GPU 驱动态常见），升级强杀并等待 OS 确认"
                f"（最多 {self._kill_escalation:.0f} 秒）…"
            )
            check_ms = max(1, int(self._kill_check_interval_ms))
            while self.is_alive() and self._clock() < deadline:
                self._hammer_kill()
                self._proc.waitForFinished(check_ms)
        if self.is_alive():
            pid = self.pid or self._last_pid
            self._emit_launcher(
                f"⚠ {self._kill_escalation:.0f} 秒强杀后服务器进程（PID {pid}）仍未退出："
                "启动器将带着这个残留进程退出，它仍会占用 GPU 显存——请在任务管理器中手动结束它"
            )

        if not self._exit_seen and not self.is_process_alive():
            self._record_exit(
                int(self._proc.exitCode()),
                self._proc.exitStatus() is QProcess.ExitStatus.CrashExit,
            )
        settle = None if self.is_alive() else self._run_settle_sync()
        self._finish_stop(settle)
        return settle

    def _begin_stop(self, *, sync: bool) -> bool:
        if self._state is ServerState.STOPPING:
            self._sync_stop = sync
            self._emit_launcher("已经在停止中，忽略重复的停止请求")
            return False
        if self._state is ServerState.STOPPED and not self.is_alive():
            self._emit_launcher("服务器本来就没有在运行")
            return False

        self._stop_requested = True
        self._sync_stop = sync
        self._settle_in_flight = False
        if self._state is ServerState.STOPPED:
            # 状态失同步：状态机说 stopped，但进程实际还活着（CUDA 进程被系统层面
            # 判定为「已结束」而 OS 里还活着时会发生）。强制收回停止流程，否则关闭
            # 启动器会把进程漏成孤儿。
            self._emit_launcher("检测到状态失同步（状态为已停止但进程仍存活），收回停止流程")
            self._state = ServerState.STOPPING
            self.state_changed.emit(ServerState.STOPPING)
        else:
            self._set_state(ServerState.STOPPING)

        self._watcher = VramSettleWatcher(
            self._vram_reader,
            timeout=self._settle_timeout,
            interval=self._settle_interval,
            fallback_wait=self._settle_fallback_wait,
            drop_bytes=self._settle_drop_bytes,
            clock=self._clock,
        )
        self._watcher.begin()

        if not self.is_alive():
            self._emit_launcher("子进程已不在运行，直接走停止收尾")
            if not sync:
                self._start_settle_async()
            return True

        pid = self.pid or self._last_pid
        self._last_pid = pid
        self._emit_launcher(f"正在停止服务器（PID {pid}）")
        self._proc.terminate()
        if not sync:
            self._grace_timer.start(max(0, int(self._terminate_grace * 1000)))
        return True

    def _on_grace_timeout(self) -> None:
        if self._state is not ServerState.STOPPING or self._sync_stop:
            return
        if not self.is_alive():
            return
        self._emit_launcher(f"terminate 后 {self._terminate_grace:.0f} 秒仍未退出，回落 taskkill（进程树）")
        self._kill_tree()
        if self.is_alive():
            self._kill_deadline = self._clock() + self._kill_escalation
            self._emit_launcher(
                f"taskkill 之后进程仍存活（GPU 驱动态常见），进入强杀校验"
                f"（最多 {self._kill_escalation:.0f} 秒）…"
            )
            self._kill_verify_timer.start()

    def _kill_tree(self) -> None:
        """taskkill /T /F 杀整个进程树；若 OS 探测发现仍存活，立即补一轮强杀。"""
        pid = self.pid or self._last_pid
        if pid is None:
            return
        self._killed = True
        ok, message = self._killer(pid)
        self._emit_launcher(message)
        if self.is_alive():
            self._emit_launcher("taskkill 之后进程仍存活，升级强杀（Win32 TerminateProcess）")
            self._hammer_kill()

    def _hammer_kill(self) -> None:
        """强杀一轮：绕过 Qt，对记录的 PID 直接发 Win32 TerminateProcess。

        CUDA 进程可能在 GPU 驱动态拒绝第一次终止请求；驱动平息后重复同一动作
        通常会成功，因此停止流程以「强杀 + OS 验证」成对出现，循环直到进程真死
        （同步路径见 :meth:`stop_and_wait`，异步路径见 :meth:`_on_kill_verify_tick`）。
        """
        pid = self.pid or self._last_pid
        if pid is None:
            return
        ok, message = self._hard_terminator(pid)
        if ok:
            self._killed = True
        self._emit_launcher(message)

    def _on_kill_verify_tick(self) -> None:
        """异步停止的强杀升级阶段：每轮以 OS 真值校验，直到确认死亡或升级时限用尽。

        只有在 OS 确认进程已死（或时限用尽并给出明确警告）后才进入显存回落等待，
        保证「停止完成」永远不会发生在进程仍占着 GPU 的时候。
        """
        if self._state is not ServerState.STOPPING or self._sync_stop:
            self._kill_verify_timer.stop()
            return
        if not self.is_alive():
            self._kill_verify_timer.stop()
            if not self._exit_seen:
                if self.is_process_alive():
                    # OS 已确认死亡但 Qt 缓存尚未跟上：补记退出信息
                    self._record_exit(0, False)
                else:
                    self._record_exit(
                        int(self._proc.exitCode()),
                        self._proc.exitStatus() is QProcess.ExitStatus.CrashExit,
                    )
            if not self._settle_in_flight:
                self._start_settle_async()
            return
        if self._clock() >= self._kill_deadline:
            self._kill_verify_timer.stop()
            pid = self.pid or self._last_pid
            self._emit_launcher(
                f"⚠ {self._kill_escalation:.0f} 秒强杀后服务器进程（PID {pid}）仍未退出："
                "继续走停止收尾，但该进程可能残留并占用 GPU 显存——请在任务管理器中确认"
            )
            if not self._settle_in_flight:
                self._start_settle_async()
            return
        self._hammer_kill()

    def _on_stdout(self) -> None:
        data = bytes(self._proc.readAllStandardOutput())
        for text in self._out_assembler.feed(data):
            self._emit_line(LogLine(LogSource.STDOUT, text))

    def _on_stderr(self) -> None:
        data = bytes(self._proc.readAllStandardError())
        for text in self._err_assembler.feed(data):
            self._emit_line(LogLine(LogSource.STDERR, text))

    def _drain(self) -> None:
        self._on_stdout()
        self._on_stderr()
        for text in self._out_assembler.flush():
            self._emit_line(LogLine(LogSource.STDOUT, text))
        for text in self._err_assembler.flush():
            self._emit_line(LogLine(LogSource.STDERR, text))

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self._record_exit(int(exit_code), exit_status is QProcess.ExitStatus.CrashExit)

    def _record_exit(self, exit_code: int, crashed: bool) -> None:
        if self._exit_seen:
            return
        self._exit_seen = True
        self._grace_timer.stop()
        self._drain()

        info = ExitInfo(
            exit_code=exit_code,
            crashed=crashed,
            expected=self._stop_requested,
            killed=self._killed,
            state_before=self._state,
            tail=self.tail_error_lines() if not self._stop_requested else (),
        )
        self._last_exit = info
        for line in info.format_lines():
            self._emit_launcher(line)
        self.exited.emit(info)

        if self._stop_requested:
            if not self._sync_stop:
                self._start_settle_async()
            return
        self._set_state(ServerState.STOPPED)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error is QProcess.ProcessError.FailedToStart:
            if self._exit_seen:
                return
            self._exit_seen = True
            program = self._command[0] if self._command else "（未知程序）"
            self._emit_launcher(f"启动失败：{program} 无法执行（{self._proc.errorString()}）")
            info = ExitInfo(failed_to_start=True, state_before=self._state)
            self._last_exit = info
            self.exited.emit(info)
            self._set_state(ServerState.STOPPED)
            return
        if error is QProcess.ProcessError.Crashed and self._stop_requested:
            return
        self._emit_launcher(f"子进程报告错误：{self._proc.errorString()}")

    def _start_settle_async(self) -> None:
        if self._settle_in_flight:
            return
        if self._watcher is None:
            self._finish_stop(None)
            return
        self._settle_in_flight = True
        self._watcher.restart_timer()
        result = self._watcher.poll()
        if result.done:
            self._finish_stop(result)
            return
        self._settle_timer.start()

    def _on_settle_tick(self) -> None:
        if self._watcher is None:
            self._settle_timer.stop()
            self._finish_stop(None)
            return
        result = self._watcher.poll()
        if result.done:
            self._settle_timer.stop()
            self._finish_stop(result)

    def _run_settle_sync(self) -> VramSettle | None:
        if self._watcher is None:
            return None
        self._watcher.restart_timer()
        return settle_vram(watcher=self._watcher)

    def _finish_stop(self, settle: VramSettle | None) -> None:
        self._settle_timer.stop()
        self._grace_timer.stop()
        self._kill_verify_timer.stop()
        self._settle_in_flight = False
        self._settle = settle
        if settle is not None:
            self._emit_launcher(settle.message())
        self._watcher = None
        self._stop_requested = False
        self._sync_stop = False
        self._set_state(ServerState.STOPPED)
        self.stop_finished.emit(settle)

    def _set_state(self, target: ServerState) -> bool:
        if target is self._state:
            return False
        if not can_transition(self._state, target):
            self._emit_launcher(
                f"内部状态异常：不允许从「{self._state.label}」转到「{target.label}」"
            )
            return False
        self._state = target
        self.state_changed.emit(target)
        return True

    def _emit_line(self, line: LogLine) -> None:
        if line.source.from_server:
            self._tail.append(line)
        self.log_line.emit(line)

    def _emit_launcher(self, text: str) -> None:
        self.log_line.emit(LogLine(LogSource.LAUNCHER, text))


class ExternalProcessStopper(QObject):
    """停止非本进程组拉起的服务实例（按 PID 定位，如 CLI 启动的服务）：与 ServerProcess 同一杀进程升级链。

    ServerProcess 的停止链从 stage 0 terminate 起步（它持有 QProcess 句柄）；外部实例没有
    QProcess 句柄，因此直接从 taskkill 杀进程树起步（与 CLI action_stop 行为一致），仍存活则
    升级强杀：每 :data:`KILL_CHECK_INTERVAL_MS` 毫秒重发 Win32 TerminateProcess，并以
    :func:`process_terminated`（OS 层真值）验证，直到进程真死或升级时限（kill_escalation）
    用尽；进程确认死亡后走与 ServerProcess 同源的显存回落等待（VramSettleWatcher）。

    terminated_check 语义与 process_terminated 相同：True = 进程已不在。全部外部 I/O 可注入
    （killer / hard_terminator / terminated_check / vram_reader / 各时间参数 / clock），
    测试用假件替换后不碰真实进程。

    信号：
    - message(str)：杀进程 / 显存回落各阶段的进度文本（调用方落日志区）；
    - finished(ok, message)：ok=True 表示进程已确认死亡且回落收尾完成；
      ok=False 表示升级时限用尽进程仍存活（message 内含手动处置指引）。
    """

    message = Signal(str)
    finished = Signal(bool, str)

    def __init__(
        self,
        pid: int,
        *,
        vram_reader: VramReader | None = None,
        killer: Killer | None = None,
        hard_terminator: Callable[[int], tuple[bool, str]] | None = None,
        terminated_check: Callable[[int | None], bool] | None = None,
        kill_escalation: float = KILL_ESCALATION_SECONDS,
        kill_check_interval_ms: int = KILL_CHECK_INTERVAL_MS,
        settle_timeout: float = VRAM_SETTLE_TIMEOUT_SECONDS,
        settle_interval: float = VRAM_POLL_INTERVAL_SECONDS,
        settle_fallback_wait: float = VRAM_FALLBACK_WAIT_SECONDS,
        settle_drop_bytes: int = VRAM_SETTLE_DROP_BYTES,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._pid = int(pid)
        self._killer = killer or run_taskkill
        self._hard_terminator = hard_terminator or terminate_process_hard
        self._terminated_check = terminated_check or process_terminated
        self._kill_escalation = kill_escalation
        self._clock = clock
        self._watcher = VramSettleWatcher(
            vram_reader,
            timeout=settle_timeout,
            interval=settle_interval,
            fallback_wait=settle_fallback_wait,
            drop_bytes=settle_drop_bytes,
            clock=clock,
        )
        self._killing = False
        self._finished_flag = False
        self._kill_deadline = 0.0

        # 强杀升级阶段的周期校验器：与 ServerProcess 的 _kill_verify_timer 同构——
        # 每轮以 OS 真值校验，直到确认死亡或升级时限用尽。
        self._kill_timer = QTimer(self)
        self._kill_timer.setInterval(max(1, int(kill_check_interval_ms)))
        self._kill_timer.timeout.connect(self._on_kill_tick)

        self._settle_timer = QTimer(self)
        self._settle_timer.setInterval(max(1, int(settle_interval * 1000)))
        self._settle_timer.timeout.connect(self._on_settle_tick)

    @property
    def pid(self) -> int:
        return self._pid

    def start(self) -> bool:
        """发起停止流程：先记显存基线（服务仍活着时），再 taskkill 杀进程树。

        进程若在发起前已自行退出（例如停止确认框弹出期间它退出了），直接走回落收尾，
        不再发杀命令。重复调用 start() 会被忽略。
        """
        if self._killing or self._finished_flag:
            return False
        self._killing = True
        self._watcher.begin()
        if self._terminated_check(self._pid):
            self._note("进程（PID %d）已不在运行，直接走停止收尾" % self._pid)
            self._begin_settle_async()
            return True
        self._note("正在停止外部服务（PID %d）…" % self._pid)
        ok, detail = self._killer(self._pid)
        self._note(detail)
        if self._terminated_check(self._pid):
            self._begin_settle_async()
        else:
            self._kill_deadline = self._clock() + self._kill_escalation
            self._note(
                "taskkill 之后进程仍存活（GPU 驱动态常见），升级强杀并等待 OS 确认"
                "（最多 %.0f 秒）…" % self._kill_escalation
            )
            self._kill_timer.start()
        return True

    def _on_kill_tick(self) -> None:
        """强杀升级阶段：每轮以 OS 真值校验，直到确认死亡或升级时限用尽。"""
        if not self._killing:
            self._kill_timer.stop()
            return
        if self._terminated_check(self._pid):
            self._kill_timer.stop()
            self._begin_settle_async()
            return
        if self._clock() >= self._kill_deadline:
            self._kill_timer.stop()
            message = (
                "⚠ %.0f 秒强杀后服务进程（PID %d）仍未退出：请在任务管理器中手动结束它，"
                "该进程仍会占用 GPU 显存" % (self._kill_escalation, self._pid)
            )
            self._note(message)
            self._finish(False, message)
            return
        ok, detail = self._hard_terminator(self._pid)
        self._note(detail)

    def _begin_settle_async(self) -> None:
        """进程已确认死亡：重启显存回落计时并周期 poll（无读数来源时按降级路径固定等待）。"""
        self._kill_timer.stop()
        self._watcher.restart_timer()
        result = self._watcher.poll()
        if result.done:
            self._finish(True, result.message())
            return
        self._settle_timer.start()

    def _on_settle_tick(self) -> None:
        result = self._watcher.poll()
        if result.done:
            self._settle_timer.stop()
            self._finish(True, result.message())

    def _note(self, text: str) -> None:
        self.message.emit(text)

    def _finish(self, ok: bool, text: str) -> None:
        if self._finished_flag:
            return
        self._finished_flag = True
        self._killing = False
        self._kill_timer.stop()
        self._settle_timer.stop()
        self.finished.emit(ok, text)

