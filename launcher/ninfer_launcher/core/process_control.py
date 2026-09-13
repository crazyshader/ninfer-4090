"""ninfer-serve 进程控制的纯逻辑：输出解码、杀进程树、强杀、OS 存活探测、显存回落等待。

本模块是 core/process.py 的零 Qt 依赖部分（2026-09 拆分，供无 Qt 依赖的 cli/ 入口复用；
GUI 与 CLI 共享同一份实现，行为完全一致）。core/process.py 只保留 QProcess 封装
ServerProcess 并从这里重导出全部符号，既有的 core.process 导入路径保持不变。

纪律：这里只放纯函数 / 纯编排，任何 Qt 相关代码一律进 core/process.py；
本文件必须保持「import 它不会加载 PySide6」。"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

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
    "VramReader",
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
]

TERMINATE_GRACE_SECONDS = 5.0
TASKKILL_TIMEOUT_SECONDS = 10.0
KILL_ESCALATION_SECONDS = 15.0
KILL_CHECK_INTERVAL_MS = 1000
PROCESS_TERMINATE_ACCESS = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
VRAM_SETTLE_TIMEOUT_SECONDS = 3.0
VRAM_POLL_INTERVAL_SECONDS = 0.25
VRAM_FALLBACK_WAIT_SECONDS = 0.5
VRAM_SETTLE_DROP_BYTES = 128 * 1024 * 1024
TAIL_LINE_COUNT = 20
LINE_BUFFER_LIMIT = 8192


class ServerState(Enum):
    """四态状态机。"""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"

    @property
    def label(self) -> str:
        return _STATE_LABELS[self]

    @property
    def active(self) -> bool:
        return self is not ServerState.STOPPED


_STATE_LABELS = {
    ServerState.STOPPED: "已停止",
    ServerState.STARTING: "加载中",
    ServerState.RUNNING: "运行中",
    ServerState.STOPPING: "正在停止",
}

ALLOWED_TRANSITIONS: dict[ServerState, frozenset[ServerState]] = {
    ServerState.STOPPED: frozenset({ServerState.STARTING}),
    ServerState.STARTING: frozenset({ServerState.RUNNING, ServerState.STOPPING, ServerState.STOPPED}),
    ServerState.RUNNING: frozenset({ServerState.STOPPING, ServerState.STOPPED}),
    ServerState.STOPPING: frozenset({ServerState.STOPPED}),
}


def can_transition(current: ServerState, target: ServerState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class LogSource(Enum):
    STDOUT = "stdout"
    STDERR = "stderr"
    LAUNCHER = "launcher"

    @property
    def label(self) -> str:
        return _SOURCE_LABELS[self]

    @property
    def from_server(self) -> bool:
        return self is not LogSource.LAUNCHER


_SOURCE_LABELS = {
    LogSource.STDOUT: "服务器 stdout",
    LogSource.STDERR: "服务器 stderr",
    LogSource.LAUNCHER: "启动器",
}


def format_log_line(source: LogSource, text: str) -> str:
    return f"[{source.label}] {text}"


@dataclass(frozen=True)
class LogLine:
    source: LogSource
    text: str

    @property
    def formatted(self) -> str:
        return format_log_line(self.source, self.text)


@dataclass(frozen=True)
class ExitInfo:
    exit_code: int | None = None
    crashed: bool = False
    expected: bool = False
    killed: bool = False
    failed_to_start: bool = False
    state_before: ServerState = ServerState.STOPPED
    tail: tuple[str, ...] = ()

    @property
    def code_text(self) -> str:
        if self.exit_code is None:
            return "未知"
        code = self.exit_code
        if code < 0 or code > 0xFFFF:
            return f"{code} (0x{code & 0xFFFFFFFF:08X})"
        return str(code)

    @property
    def clean(self) -> bool:
        return self.exit_code == 0 and not self.crashed and not self.failed_to_start

    @property
    def unexpected(self) -> bool:
        return not self.expected

    def headline(self) -> str:
        if self.failed_to_start:
            return "服务器进程未能启动（程序不存在或不可执行）"
        if self.expected:
            head = f"服务器进程已退出，退出码 {self.code_text}"
            if self.killed:
                head += "（已用 taskkill 终止进程树）"
            return head
        stage = self.state_before.label
        if self.state_before is ServerState.STARTING:
            return f"服务器进程在就绪前退出（{stage}），退出码 {self.code_text}"
        return f"服务器进程意外退出（{stage}），退出码 {self.code_text}"

    def abort_message(self) -> str:
        return self.headline()

    def format_lines(self) -> tuple[str, ...]:
        lines = [self.headline()]
        if self.unexpected and self.tail:
            lines.append(f"最后 {len(self.tail)} 行服务器输出：")
            lines.extend(f"  {line}" for line in self.tail)
        elif self.unexpected and not self.failed_to_start:
            lines.append("服务器没有留下任何输出")
        return tuple(lines)


class SettleOutcome(Enum):
    PENDING = "pending"
    SETTLED = "settled"
    TIMEOUT = "timeout"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class VramSettle:
    outcome: SettleOutcome
    baseline_bytes: int | None = None
    last_bytes: int | None = None
    waited: float = 0.0
    polls: int = 0
    reason: str | None = None

    @property
    def done(self) -> bool:
        return self.outcome is not SettleOutcome.PENDING

    @property
    def degraded(self) -> bool:
        return self.outcome is SettleOutcome.DEGRADED

    def message(self) -> str:
        if self.outcome is SettleOutcome.SETTLED:
            freed = (self.baseline_bytes or 0) - (self.last_bytes or 0)
            return f"显存已回落：{_mib(self.baseline_bytes)} -> {_mib(self.last_bytes)}（释放约 {freed // 1024 // 1024} MiB）"
        if self.outcome is SettleOutcome.TIMEOUT:
            return f"等待 {self.waited:.1f} 秒仍未见显存回落（降级继续）"
        if self.outcome is SettleOutcome.DEGRADED:
            return f"未能确认显存回落（{self.reason or '无读数来源'}），已改为固定等待 {self.waited:.1f} 秒"
        return f"仍在等待显存回落"


def _mib(value: int | None) -> str:
    if value is None:
        return "未知"
    return f"{value / 1024 / 1024:.0f} MiB"


# ---------------------------------------------------------------------------
# 输出解码与行装配
# ---------------------------------------------------------------------------

_ENCODING_CHAIN: tuple[str, ...] | None = None


def _encoding_chain() -> tuple[str, ...]:
    global _ENCODING_CHAIN
    if _ENCODING_CHAIN is None:
        names: list[str] = ["utf-8"]
        try:
            import ctypes
            code_page = int(ctypes.windll.kernel32.GetOEMCP())
            if code_page:
                names.append(f"cp{code_page}")
        except Exception:
            pass
        import locale
        try:
            name = locale.getpreferredencoding(False)
            if name and name not in names:
                names.append(name)
        except Exception:
            pass
        _ENCODING_CHAIN = tuple(names)
    return _ENCODING_CHAIN


def decode_output(data: bytes) -> tuple[str, str]:
    """把子进程输出解码，永不抛异常。"""
    if not data:
        return "", "ascii"
    for name in _encoding_chain():
        try:
            return data.decode(name), name
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8+replace"


class LineAssembler:
    """把一串串到达的字节拼成完整的行。"""

    def __init__(self, limit: int = LINE_BUFFER_LIMIT) -> None:
        self._buffer = bytearray()
        self._limit = max(64, int(limit))

    @property
    def pending(self) -> bytes:
        return bytes(self._buffer)

    def feed(self, data: bytes) -> list[str]:
        if data:
            self._buffer.extend(data)
        if not self._buffer:
            return []
        trailing_cr = self._buffer.endswith(b"\r")
        body = bytes(self._buffer[:-1]) if trailing_cr else bytes(self._buffer)
        parts = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
        tail = parts.pop()
        lines = [decode_output(part)[0] for part in parts]
        self._buffer = bytearray(tail)
        if trailing_cr:
            self._buffer.extend(b"\r")
        if len(self._buffer) > self._limit:
            lines.append(decode_output(bytes(self._buffer))[0])
            self._buffer.clear()
        return lines

    def flush(self) -> list[str]:
        if not self._buffer:
            return []
        text = decode_output(bytes(self._buffer).rstrip(b"\r\n"))[0]
        self._buffer.clear()
        return [text] if text else []


# ---------------------------------------------------------------------------
# 杀进程树
# ---------------------------------------------------------------------------


def taskkill_command(pid: int) -> list[str]:
    return ["taskkill", "/PID", str(int(pid)), "/T", "/F"]


def run_taskkill(pid: int, timeout: float = TASKKILL_TIMEOUT_SECONDS) -> tuple[bool, str]:
    command = taskkill_command(pid)
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return False, "系统上找不到 taskkill"
    except subprocess.TimeoutExpired:
        return False, f"taskkill 超过 {timeout:.0f} 秒未返回"
    except OSError as exc:
        return False, f"taskkill 无法执行：{exc}"
    raw = completed.stdout or b""
    if not raw.strip():
        raw = completed.stderr or b""
    detail = decode_output(raw)[0].strip().replace("\r\n", " ").replace("\n", " ")
    if completed.returncode == 0:
        return True, f"已用 taskkill 终止进程树（PID {pid}）"
    return False, f"taskkill 未成功（退出码 {completed.returncode}）"


def terminate_process_hard(pid: int) -> tuple[bool, str]:
    """绕过 Qt，直接以 Win32 OpenProcess + TerminateProcess 终止目标进程。

    实测（2026-09-10，RTX 4090 / Windows 11）：QProcess.terminate() 对 CUDA 进程
    （ninfer-serve）可能静默失败——进程处于 GPU 驱动临界区时，OS 会推迟其终止，
    宽限期结束时进程仍存活。此时需要直接以 PID 再发一次 TerminateProcess；
    驱动态平息后，重复的终止请求通常会成功。「是否真死」只能以进程句柄探测
    （:func:`process_terminated`）为准，任何单次 kill 调用的返回值都不算数。
    """
    if not pid:
        return False, "无进程 PID，无法强杀"
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_TERMINATE_ACCESS, False, int(pid)
        )
        if not handle:
            code = ctypes.windll.kernel32.GetLastError()
            return False, f"无法取得进程句柄（PID {pid}，错误码 {code}）"
        try:
            if ctypes.windll.kernel32.TerminateProcess(handle, 0):
                return True, f"已向 PID {pid} 发送强杀（等待 OS 确认终止）"
            code = ctypes.windll.kernel32.GetLastError()
            return False, f"TerminateProcess 失败（PID {pid}，错误码 {code}）"
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception as exc:  # noqa: BLE001 —— 与 run_taskkill 同契约：绝不抛异常
        return False, f"强杀不可用：{exc}"


def process_terminated(pid: int | None) -> bool:
    """OS 层真值：给定 PID 的进程是否已不在（OpenProcess 探测）。

    Qt 的状态缓存与 taskkill 的返回值对 CUDA 进程都可能「说谎」，停止流程在
    宣布「进程已消失」之前必须以本方法为准。
    """
    if not pid:
        return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        # OpenProcess 成功（拿到句柄）= 进程仍在；失败（0）= 进程已不在。
        # 2026-09 回归修复：此前两处返回值写反，把「活进程」判成「已终止」，
        # 导致 ensure 在加载窗口内误报 crashed、load_pid_entry 误删存活实例的
        # 登记表（实例被降格为 external，stop 再也找不到 PID）。
        if not handle:
            return True
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 显存回落等待
# ---------------------------------------------------------------------------

VramReader = Callable[[], int | None]


class VramSettleWatcher:
    """停止后等显存回落的状态机。"""

    def __init__(
        self,
        reader: VramReader | None = None,
        *,
        timeout: float = VRAM_SETTLE_TIMEOUT_SECONDS,
        interval: float = VRAM_POLL_INTERVAL_SECONDS,
        fallback_wait: float = VRAM_FALLBACK_WAIT_SECONDS,
        drop_bytes: int = VRAM_SETTLE_DROP_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reader = reader
        self.timeout = timeout
        self.interval = interval
        self.fallback_wait = fallback_wait
        self.drop_bytes = drop_bytes
        self._clock = clock
        self._started_at: float | None = None
        self._baseline: int | None = None
        self._last: int | None = None
        self._polls = 0
        self._degraded_reason: str | None = None

    @property
    def baseline_bytes(self) -> int | None:
        return self._baseline

    def next_delay(self) -> float:
        return self.interval

    def restart_timer(self) -> None:
        self._started_at = self._clock()

    def begin(self) -> None:
        self._started_at = self._clock()
        self._polls = 0
        self._degraded_reason = None
        if self._reader is None:
            self._baseline = None
            self._last = None
            self._degraded_reason = "未提供显存读数来源"
            return
        self._baseline = self._read()
        self._last = self._baseline
        if self._baseline is None:
            self._degraded_reason = "显存读数不可用"

    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return self._clock() - self._started_at

    def poll(self) -> VramSettle:
        if self._started_at is None:
            self.begin()
        elapsed = self.elapsed()
        if self._degraded_reason is not None:
            if elapsed >= self.fallback_wait:
                return self._result(SettleOutcome.DEGRADED, elapsed)
            return self._result(SettleOutcome.PENDING, elapsed)
        reading = self._read()
        self._polls += 1
        if reading is None:
            self._degraded_reason = "显存读数中途不可用"
            return self._result(SettleOutcome.DEGRADED, elapsed)
        self._last = reading
        baseline = self._baseline
        if baseline is not None and baseline - reading >= self.drop_bytes:
            return self._result(SettleOutcome.SETTLED, elapsed)
        if elapsed >= self.timeout:
            return self._result(SettleOutcome.TIMEOUT, elapsed)
        return self._result(SettleOutcome.PENDING, elapsed)

    def _read(self) -> int | None:
        if self._reader is None:
            return None
        try:
            value = self._reader()
        except Exception:
            return None
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    def _result(self, outcome: SettleOutcome, elapsed: float) -> VramSettle:
        return VramSettle(
            outcome=outcome,
            baseline_bytes=self._baseline,
            last_bytes=self._last,
            waited=elapsed,
            polls=self._polls,
            reason=self._degraded_reason,
        )


def settle_vram(
    reader: VramReader | None = None,
    *,
    timeout: float = VRAM_SETTLE_TIMEOUT_SECONDS,
    interval: float = VRAM_POLL_INTERVAL_SECONDS,
    fallback_wait: float = VRAM_FALLBACK_WAIT_SECONDS,
    drop_bytes: int = VRAM_SETTLE_DROP_BYTES,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    watcher: VramSettleWatcher | None = None,
) -> VramSettle:
    """同步等到显存回落/超时/降级。会阻塞调用线程。"""
    if watcher is None:
        watcher = VramSettleWatcher(
            reader, timeout=timeout, interval=interval,
            fallback_wait=fallback_wait, drop_bytes=drop_bytes, clock=clock,
        )
        watcher.begin()
    while True:
        result = watcher.poll()
        if result.done:
            return result
        sleep(watcher.next_delay())


def stop_external_sync(
    pid: int,
    *,
    killer: Callable[[int], tuple[bool, str]] = run_taskkill,
    hard_terminator: Callable[[int], tuple[bool, str]] = terminate_process_hard,
    terminated_check: Callable[[int | None], bool] = process_terminated,
    kill_escalation: float = KILL_ESCALATION_SECONDS,
    kill_check_interval_ms: int = KILL_CHECK_INTERVAL_MS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_message: Callable[[str], None] | None = None,
) -> bool:
    """按 PID 同步停止外部实例（CLI / 外部拉起、无 QProcess 句柄），返回 True=已确认死亡。

    升级链与 ExternalProcessStopper 完全一致，仅把 QTimer 轮询换成阻塞 sleep：
    1. 进程已不在运行 → 直接返回 True（不发任何杀命令）；
    2. taskkill 杀进程树；
    3. 仍存活（CUDA 驱动态常见）→ 每 kill_check_interval_ms 重发 Win32 强杀，
       以 OS 层真值（terminated_check）校验，直到进程真死或 kill_escalation 用尽。

    供 closeEvent 等「必须同步等到进程消失才能继续」的路径使用（GUI 窗口关闭后
    事件循环即销毁，异步 stopper 的 QTimer 不再有 tick 机会）。显存回落等待刻意
    省略——窗口即将销毁，无需再等显存曲线回落；若时限用尽仍未杀死，调用方应据
    False 返回值给出显著警告。全部 I/O 可注入，测试用假件替换后不碰真实进程。
    """
    if not pid:
        return False
    if on_message is None:
        on_message = lambda _text: None
    if terminated_check(pid):
        on_message(f"进程（PID {pid}）已不在运行，无需停止")
        return True
    on_message(f"正在停止外部服务（PID {pid}）…")
    ok, detail = killer(pid)
    on_message(detail)
    if terminated_check(pid):
        return True
    deadline = clock() + kill_escalation
    on_message(
        f"taskkill 之后进程仍存活（GPU 驱动态常见），升级强杀并等待 OS 确认"
        f"（最多 {kill_escalation:.0f} 秒）…"
    )
    interval = max(0.001, kill_check_interval_ms / 1000.0)
    while True:
        if terminated_check(pid):
            return True
        if clock() >= deadline:
            on_message(
                f"⚠ {kill_escalation:.0f} 秒强杀后服务进程（PID {pid}）仍未退出："
                "请在任务管理器中手动结束它，该进程仍会占用 GPU 显存"
            )
            return False
        sleep(interval)
        ok, detail = hard_terminator(pid)
        on_message(detail)


