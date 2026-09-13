"""占用显存的进程枚举：NVML 进程接口 + psutil 进程名。

core/monitor.py 三段分法的第三段（与真实系统打交道）：本模块是唯一在启动器里
直接碰 NVML 进程接口的地方。NVML / psutil 模块都可注入（测试用假件替换），
默认自动 import；两者皆缺失时返回空元组。

与 core/monitor.py 的关系：进程级显存读数 NVML 只给得出（nvidia-smi 的
--query-compute-apps 亦可，但每条查询都要新起进程，周期任务里太贵）；因此
调用方（ui/main_window.py）优先传入 MonitorService 已解析的 NVML 模块
（monitor.nvml_module 访问器），复用同一初始化句柄，绝不二次 nvmlInit。

隐性契约：

1. **永不抛异常**。本模块的结果会被界面周期定时器消费，抛异常等于点一下就崩；
   NVML / psutil 的每次调用各自 try/except，单项失败只影响那一项（进程名回落
   \"PID <pid>\"），不牵连整体。
2. **used_bytes 是 值 | None**：NVML 在部分驱动 / 权限下报不出单进程显存
   （usedGpuMemory=None），此时如实记 None（界面显示「未知」），绝不记 0。
3. **同一个 PID 同时出现在 compute / graphics 两个列表时合并**，占用取两者较大值
   （None 只让位给有读数的一方，两个都是 None 才保持 None）。
4. **is_self / is_protected 只标记、不过滤**：自身（ninfer-serve / ninfer）与
   系统关键进程仍出现在返回结果里（它们的占用计入总量），只是上游「建议退出」
   列表会排除它们——退掉自己没意义，退掉 dwm.exe 会毁桌面。
5. **排序：used_bytes 降序，None 排最后**，同值按 pid 升序（结果确定性）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "GpuProcess",
    "SELF_NAMES",
    "PROTECTED_NAMES",
    "list_gpu_processes",
]

#: 注入点哨兵：代表「自动检测」（真的尝试 import）。
_NVML_AUTO = object()
_PSUTIL_AUTO = object()

#: ninfer 自身进程名（大小写不敏感匹配）——建议退出列表里排除。
SELF_NAMES = frozenset({"ninfer-serve.exe", "ninfer.exe"})

#: 系统关键进程（大小写不敏感匹配）——不建议用户退出。
PROTECTED_NAMES = frozenset(
    {"dwm.exe", "csrss.exe", "winlogon.exe", "explorer.exe", "system", "registry"}
)


class GpuProcess:
    """占着显存的一个进程。

    :param pid: 进程号
    :param name: 进程名（exe 名）；psutil 拿不到时 \"PID <pid>\"
    :param used_bytes: 该进程占用的显存字节数；NVML 报不出时 None
    :param is_self: 是否 ninfer 自身（不参与「建议退出」）
    :param is_protected: 是否系统关键进程（不建议退出）
    """

    __slots__ = ("pid", "name", "used_bytes", "is_self", "is_protected")

    def __init__(
        self,
        pid: int,
        name: str,
        used_bytes: int | None = None,
        is_self: bool = False,
        is_protected: bool = False,
    ) -> None:
        self.pid = pid
        self.name = name
        self.used_bytes = used_bytes
        self.is_self = is_self
        self.is_protected = is_protected

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GpuProcess):
            return NotImplemented
        return (
            self.pid == other.pid
            and self.name == other.name
            and self.used_bytes == other.used_bytes
            and self.is_self == other.is_self
            and self.is_protected == other.is_protected
        )

    def __repr__(self) -> str:
        return (
            f"GpuProcess(pid={self.pid}, name={self.name!r}, "
            f"used_bytes={self.used_bytes!r}, is_self={self.is_self}, "
            f"is_protected={self.is_protected})"
        )


def _resolve_nvml(nvml_module: Any) -> Any:
    """取要用的 NVML 模块。

    传入非 None（真实 / 假模块）时直接用它；None 或自动哨兵时尝试 import pynvml，
    未安装则返回 None（调用方据此返回空元组，预检降级为 UNAVAILABLE）。
    """
    if nvml_module is not None:
        return nvml_module
    try:
        import pynvml
    except ImportError:
        return None
    return pynvml


def _resolve_psutil(psutil_module: Any) -> Any:
    """取要用的 psutil 模块（只用于进程名）；缺失返回 None，名字回落 \"PID <pid>\"。"""
    if psutil_module is not None:
        return psutil_module
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def _process_name(psutil_mod: Any, pid: int) -> str:
    """查进程名 / exe 名；psutil 缺失或查询失败（进程已退出 / 权限不足）回落 \"PID <pid>\"。"""
    if psutil_mod is not None:
        try:
            process = psutil_mod.Process(pid)
            name = process.name()
            if name:
                return str(name)
            exe = process.exe()
            if exe:
                return Path(exe).name
        except Exception:  # noqa: BLE001 - 进程名是展示信息，失败不牵连整体
            pass
    return f"PID {pid}"


def _merge_usage(existing: int | None, incoming: int | None) -> int | None:
    """同一 PID 的两次读数合并：取较大值；None 只表示「读不出」，让位给有读数的一方。"""
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    return max(existing, incoming)


def _nvml_process_usage(nvml: Any, handle: Any, getter_name: str) -> dict[int, int | None]:
    """跑一个 NVML 进程接口（compute / graphics），返回 {pid: used_bytes | None}。

    接口本身调用失败（驱动 / 权限）时返回空 dict；单条记录的字段异常只丢弃该条。
    """
    try:
        processes = getattr(nvml, getter_name)(handle)
    except Exception:  # noqa: BLE001
        return {}
    result: dict[int, int | None] = {}
    for entry in processes or ():
        try:
            pid = int(getattr(entry, "pid"))
        except (AttributeError, TypeError, ValueError):
            continue
        used = getattr(entry, "usedGpuMemory", None)
        try:
            used = None if used is None else int(used)
        except (TypeError, ValueError):
            used = None
        result[pid] = _merge_usage(result.get(pid), used)
    return result


def list_gpu_processes(
    *,
    nvml_module: Any = _NVML_AUTO,
    psutil_module: Any = _PSUTIL_AUTO,
    device_index: int = 0,
    self_names: frozenset[str] = SELF_NAMES,
) -> tuple[GpuProcess, ...]:
    """枚举指定 GPU 上占用显存的进程。

    返回按 used_bytes 降序（None 排最后、同值按 pid 升序）的元组。**永不抛异常**：
    NVML 不可用（未安装 / 初始化失败 / 设备句柄拿不到）或没有任何进程时返回空
    元组；psutil 缺失只让进程名退化为 \"PID <pid>\"，不影响枚举本身。

    :param nvml_module: 注入的 NVML 模块（如 MonitorService.nvml_module 复用同一句柄）；
        默认自动 import pynvml，显式传 None 也走自动检测
    :param psutil_module: 注入的 psutil 模块（仅用于进程名），默认自动 import
    :param device_index: GPU 索引，单卡场景恒为 0
    :param self_names: 「自身进程」名单（覆盖默认 SELF_NAMES，测试用）
    """
    nvml = _resolve_nvml(nvml_module)
    if nvml is None:
        return ()
    try:
        nvml.nvmlInit()
    except Exception:  # noqa: BLE001 - NVML 初始化失败 = 进程枚举整体不可用
        return ()
    try:
        handle = nvml.nvmlDeviceGetHandleByIndex(device_index)
    except Exception:  # noqa: BLE001
        return ()

    usage: dict[int, int | None] = {}
    for getter in ("nvmlDeviceGetComputeRunningProcesses", "nvmlDeviceGetGraphicsRunningProcesses"):
        for pid, used in _nvml_process_usage(nvml, handle, getter).items():
            usage[pid] = _merge_usage(usage.get(pid), used)
    if not usage:
        return ()

    psutil_mod = _resolve_psutil(psutil_module)
    self_lut = {name.lower() for name in self_names}
    protected_lut = {name.lower() for name in PROTECTED_NAMES}
    processes = []
    for pid in usage:
        name = _process_name(psutil_mod, pid)
        lowered = name.lower()
        processes.append(
            GpuProcess(
                pid=pid,
                name=name,
                used_bytes=usage[pid],
                is_self=lowered in self_lut,
                is_protected=lowered in protected_lut,
            )
        )
    processes.sort(key=lambda p: (p.used_bytes is None, -(p.used_bytes or 0), p.pid))
    return tuple(processes)
