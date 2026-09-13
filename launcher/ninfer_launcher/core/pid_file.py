"""CLI 运行时文件层：进程登记表（PID 文件）、启动互斥锁、服务日志。

布局（docs/01-ninfer-launcher-cli.md 第 5 节）：

    %LOCALAPPDATA%/ninfer-launcher/
    └── runtime/
        ├── serve.pid    进程登记表（JSON，原子写入；只是「索引」，绝不是状态真值）
        ├── serve.lock   启动互斥锁（O_CREAT|O_EXCL；过期锁按 OS 层进程存活判定回收）
        └── logs/        服务 stdout/stderr 日志（serve-<yyyyMMdd-HHmmss>.log，保留最新 10 份）

本层自 2026-09 起从 cli/runtime.py 迁入 core/：GUI（主窗口每秒外部服务对账与
「停止外部实例」）和 CLI（status / start / stop / ensure）都要读写这份登记表，
放进 core 才能保证两端共享同一实现、行为一致；cli/runtime.py 现在只剩再导出
（兼容既有导入路径）。本模块零 Qt 依赖，「import 它不会加载 PySide6」。

关键纪律：

- PID 文件：先写 .tmp 再 os.replace（与 config.save_settings 同一原子模式）；读取后
  必须用 process_terminated 校验进程存活，陈旧文件立即删除——PID 文件只用来定位
  PID 和归属，绝不作为「服务在跑」的真值（约束 4）。GUI 侧的对账是纯只读观察
  （read_pid_entry_raw，不删文件），陈旧登记表的清理由 CLI 的 status/stop 负责。
- 锁：os.open(O_CREAT | O_EXCL) 创建；拿到 FileExistsError 时读持锁者 PID，
  持锁者已死视为过期锁（断电 / 崩溃残留），删除后重试一次；持锁者活着则返回
  HELD_BY_OTHER，由调用方转入「等另一个实例的结果」分支。
- 日志尾部读取：一律走 core/process_control.decode_output（永不抛异常，兼容
  OEM 代码页）；不要 open(..., encoding='utf-8') 直读（docs 陷阱 9）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .process_control import decode_output

__all__ = [
    "RUNTIME_DIR",
    "PID_FILE",
    "LOCK_FILE",
    "LOGS_DIR",
    "LOG_KEEP",
    "PID_SCHEMA",
    "PidEntry",
    "LockState",
    "LockResult",
    "runtime_dir",
    "pid_path",
    "lock_path",
    "logs_dir",
    "read_pid_entry_raw",
    "load_pid_entry",
    "write_pid_entry",
    "delete_pid_file",
    "acquire_lock",
    "release_lock",
    "lock_holder_pid",
    "new_log_path",
    "rotate_logs",
    "read_log_tail",
    "TAIL_BLOCK_SIZE",
]

RUNTIME_DIR = "runtime"
PID_FILE = "serve.pid"
LOCK_FILE = "serve.lock"
LOGS_DIR = "logs"
#: 日志保留份数（按文件名排序，文件名即时间戳）
LOG_KEEP = 10
#: read_log_tail 每次从文件尾部读取的块大小：64 KiB 足够覆盖 20 条常规日志行（docs 12.4）
TAIL_BLOCK_SIZE = 64 * 1024
PID_SCHEMA = 1


@dataclass(frozen=True)
class PidEntry:
    """进程登记表内容：定位服务进程 + 记录归属（约束 4 中「只用来定位」的那部分）。"""

    schema: int
    pid: int
    port: int | None
    exe: str
    args: tuple[str, ...]
    model: str | None
    preset: str | None
    owner: str
    started_at: float
    log_path: str | None


def runtime_dir(root: Path) -> Path:
    return Path(root) / RUNTIME_DIR


def pid_path(root: Path) -> Path:
    return runtime_dir(root) / PID_FILE


def lock_path(root: Path) -> Path:
    return runtime_dir(root) / LOCK_FILE


def logs_dir(root: Path) -> Path:
    return runtime_dir(root) / LOGS_DIR


def _ensure_runtime_dir(root: Path) -> None:
    runtime_dir(root).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 进程登记表（PID 文件）
# ---------------------------------------------------------------------------

def read_pid_entry_raw(root: Path) -> PidEntry | None:
    """读进程登记表。文件缺失或损坏 → None（损坏视同陈旧，由调用方删除）。

    owner 缺失/非法时按 external 处理：不是我们写的登记表就不能归到本工具名下，
    stop 因此默认拒绝动手（除非 --force），这是安全方向上的保守选择。
    """
    path = pid_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    port = data.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not (0 < port < 65536):
        port = None
    args = data.get("args")
    args = tuple(str(x) for x in args) if isinstance(args, list) else ()
    owner = data.get("owner")
    if not isinstance(owner, str) or not owner:
        owner = "external"
    started = data.get("startedAt")
    started = float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else None
    exe = data.get("exe")
    model = data.get("model")
    preset = data.get("preset")
    log_path = data.get("logPath")
    return PidEntry(
        schema=PID_SCHEMA,
        pid=pid,
        port=port,
        exe=exe if isinstance(exe, str) else "",
        args=args,
        model=model if isinstance(model, str) else None,
        preset=preset if isinstance(preset, str) else None,
        owner=owner,
        started_at=started if started is not None else 0.0,
        log_path=log_path if isinstance(log_path, str) else None,
    )


def load_pid_entry(
    root: Path,
    terminated_check: Callable[[int | None], bool] | None = None,
) -> PidEntry | None:
    """读登记表并做 OS 层存活校验（约束 4：PID 文件绝不是状态真值）。

    terminated_check(pid) 语义同 core.process_control.process_terminated：
    返回 True 表示进程**已不在**。校验不通过（进程已死 / 文件损坏）时删除
    文件并返回 None——调用方按「服务未运行」分支处理。
    """
    if terminated_check is None:
        from .process_control import process_terminated

        terminated_check = process_terminated
    entry = read_pid_entry_raw(root)
    if entry is None:
        delete_pid_file(root)
        return None
    if terminated_check(entry.pid):
        delete_pid_file(root)
        return None
    return entry


def write_pid_entry(root: Path, entry: PidEntry) -> None:
    """原子写入进程登记表：先写 .tmp 再 os.replace（docs 陷阱 12）。"""
    _ensure_runtime_dir(root)
    path = pid_path(root)
    data = {
        "schema": entry.schema,
        "pid": entry.pid,
        "port": entry.port,
        "exe": entry.exe,
        "args": list(entry.args),
        "model": entry.model,
        "preset": entry.preset,
        "owner": entry.owner,
        "startedAt": entry.started_at,
        "logPath": entry.log_path,
    }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def delete_pid_file(root: Path) -> None:
    path = pid_path(root)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 启动互斥锁
# ---------------------------------------------------------------------------

class LockState(Enum):
    ACQUIRED = "acquired"
    HELD_BY_OTHER = "held_by_other"
    FAILED = "failed"


@dataclass(frozen=True)
class LockResult:
    state: LockState
    path: Path
    #: HELD_BY_OTHER 时：持锁的另一个 CLI 的 PID
    holder_pid: int | None = None
    #: ACQUIRED 时：写进锁文件的本进程 PID
    owner_pid: int | None = None


def lock_holder_pid(path: Path) -> int | None:
    """读锁文件里的持锁者 PID；文件缺失/损坏 → None。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def acquire_lock(
    root: Path,
    *,
    owner_pid: int | None = None,
    terminated_check: Callable[[int | None], bool] | None = None,
) -> LockResult:
    """抢启动互斥锁（协议见 docs/01-ninfer-launcher-cli.md 第 5 节）。

    - 成功：O_CREAT|O_EXCL 创建锁文件并写入本进程 PID + 时间戳；
    - 失败且持锁者**活着**：返回 HELD_BY_OTHER（调用方转「等另一个实例的结果」，
      这不是错误）；
    - 失败且持锁者已死（断电 / 崩溃残留的过期锁）：删除后重试**一次**——
      只重试一次，避免两个 CLI 反复互删。
    """
    if terminated_check is None:
        from .process_control import process_terminated

        terminated_check = process_terminated
    path = lock_path(root)
    _ensure_runtime_dir(root)
    owner = owner_pid if owner_pid is not None else os.getpid()
    for _attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = lock_holder_pid(path)
            if holder is not None and not terminated_check(holder):
                return LockResult(LockState.HELD_BY_OTHER, path, holder_pid=holder)
            # 持锁者已死（或锁文件读不出 PID）→ 过期锁：删除并重试
            try:
                path.unlink()
            except OSError:
                pass
            continue
        except OSError:
            return LockResult(LockState.FAILED, path)
        try:
            payload = json.dumps({"pid": owner, "acquiredAt": time.time()}, ensure_ascii=False)
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return LockResult(LockState.ACQUIRED, path, owner_pid=owner)
    # 重试过一次后仍然拿不到：锁被活着的进程持有，或处于无法判定的状态
    holder = lock_holder_pid(path)
    if holder is not None and not terminated_check(holder):
        return LockResult(LockState.HELD_BY_OTHER, path, holder_pid=holder)
    return LockResult(LockState.FAILED, path, holder_pid=holder)


def release_lock(lock: LockResult) -> None:
    """释放锁（调用方必须放在 try/finally 里；仅当我们持有锁时生效）。"""
    if lock.state is not LockState.ACQUIRED:
        return
    try:
        lock.path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def new_log_path(root: Path, *, now: float | None = None) -> Path:
    """创建 logs/ 目录并返回新日志文件路径（serve-<yyyyMMdd-HHmmss>.log）。"""
    d = logs_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    return d / ("serve-%s.log" % stamp)


def rotate_logs(root: Path, keep: int = LOG_KEEP) -> None:
    """保留最新 keep 份日志，删掉更早的（文件名即时间戳，按名排序即按时间排序）。"""
    d = logs_dir(root)
    if not d.is_dir():
        return
    files = sorted(
        f for f in d.iterdir()
        if f.is_file() and f.name.startswith("serve-") and f.name.endswith(".log")
    )
    overflow = len(files) - max(0, keep)
    for old in files[: max(0, overflow)]:
        try:
            old.unlink()
        except OSError:
            pass


def read_log_tail(path: str | Path, lines: int, block_size: int = TAIL_BLOCK_SIZE) -> list[str]:
    """读日志尾部 lines 行；读不到 / 解码失败一律返回 []，绝不抛异常。

    不整读文件：默认从文件**尾部**读 64 KiB 一块，行数不够就把块加倍向前扩，直到
    凑够或读到文件头（docs 12.4）——几十 MB 的长时运行日志上 --tail 20 只读最后一块。
    两个必须守住的细节：

    - 解码一律走 process_control.decode_output（自动探测编码链：UTF-8 → OEM
      代码页 → locale → utf-8+replace，永不抛异常，兼容 ninfer-serve 的 OEM 输出，
      docs 陷阱 9）——不要 open(encoding='utf-8') 直读；
    - 块起点落在行中间时，先前进到第一个换行、把不完整的**首行**整体丢掉再解码。
      不能按字节硬切后直接解码：块起点切断一个多字节字符会让整块 UTF-8 解码失败、
      decode_output 落到 OEM 回退，导致**整段尾部**全乱码而不是首行。换行符不可能
      是多字节字符的组成部分（UTF-8 / GBK 的后续字节都不含 0x0A），按行边界对齐
      不会切字符，剩余块仍可用单一编码整体解码。
    """
    if lines <= 0:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    try:
        size = p.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    span = min(max(block_size, 1), size)
    offset = size - span
    for _ in range(64):  # 块每轮加倍，够不到的文件早就读到头了；上限只是保险
        try:
            with open(p, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            return []
        if offset == 0:
            text, _ = decode_output(data)
            if not text:
                return []
            all_lines = text.splitlines()
            return all_lines[-lines:] if len(all_lines) > lines else all_lines
        cut = data.find(b"\n")
        if cut < 0:
            # 整块是一条（不完整的）长行：块加倍向前扩
            span = min(size, span * 2)
            offset = size - span
            continue
        data = data[cut + 1:]  # 丢弃被块边界切断的首行
        text, _ = decode_output(data)
        all_lines = text.splitlines()
        if len(all_lines) >= lines:
            return all_lines[-lines:]
        span = min(size, span * 2)
        offset = size - span
    # 64 轮仍未凑够（病态日志）：退回整读一次
    try:
        with open(p, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    text, _ = decode_output(data)
    if not text:
        return []
    all_lines = text.splitlines()
    return all_lines[-lines:] if len(all_lines) > lines else all_lines
