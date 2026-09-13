"""GPU / CPU / 内存资源监视：NVML 优先、nvidia-smi 回落、psutil 采集。

单卡场景（AGENTS.md：RTX 4090 / sm_89）下的资源监视。本模块按 core/ports.py /
core/health.py 的同一套分法切成三段，边界刻意划清：

1. 纯逻辑（GpuSnapshot / SystemSnapshot / MonitorSource / parse_nvidia_smi_csv /
   nvidia_smi_command）：输入数值或文本，输出数据。不起进程、不碰 NVML，是测试主战场
2. 纯编排（MonitorService）：拿可注入的 NVML 模块 / nvidia-smi 执行器 / psutil 模块，
   按优先级决定用哪个来源，自身不假设某个来源一定能用
3. 与真实系统打交道（run_nvidia_smi、真实的 pynvml / psutil import）：通过
   MonitorService 构造参数整段可替换，测试既不需要真实 GPU，也不需要装 pynvml

本模块不做定时器：MonitorService.poll 是一次同步采集，调用方（ui/monitor_panel.py
的 QTimer）决定何时调、隔多久调；SystemSnapshot.source.refresh_interval_ms 只是建议值。

隐性契约：

1. 「不可用」与「数值恰好是 0」必须能区分。每个数值字段都是 值 | None，None 表示
   这一项读不到，真实读数（哪怕 0）落在数值类型里。界面看到 None 才显示「不可用」。
2. NVML 初始化失败 → 整体回落 nvidia-smi，且只告知一次。NVML 运行期中途失效与
   「一开始就初始化失败」是两件事：本模块只在中途失效时于当轮临时回落一次 nvidia-smi
   交差，不永久放弃 NVML——下一轮还会再试。
3. NVML 与 nvidia-smi 都不可用时，gpus 是空元组，不是「填满 None 的假 GPU」。
4. 多 GPU 枚举与「提供指定索引的显存读数」是两件独立的事。make_vram_reader 产出的
   闭包每次调用走一次完整 poll（枚举全部 GPU 再挑一个），签名与 core/process.py 的
   VramReader 严格一致，可直接传给 ServerProcess(vram_reader=...)。
5. psutil.cpu_percent 首次调用必须打底（其语义是「自上次调用以来的平均利用率」）。
6. 一切失败都不抛异常。本模块挂在界面周期定时器上，抛异常等于点一下就崩。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

__all__ = [
    "NORMAL_INTERVAL_MS",
    "FALLBACK_INTERVAL_MS",
    "NVIDIA_SMI_TIMEOUT_SECONDS",
    "NVIDIA_SMI_FIELDS",
    "UNAVAILABLE_TEXT",
    "MonitorSource",
    "GpuSnapshot",
    "SystemSnapshot",
    "VramReader",
    "nvidia_smi_command",
    "parse_nvidia_smi_csv",
    "run_nvidia_smi",
    "MonitorService",
    "make_vram_reader",
]

#: 正常刷新周期（1 秒）。
NORMAL_INTERVAL_MS = 1000

#: 回落到 nvidia-smi 时建议放宽到的刷新周期（3 秒：每次要新起进程，1 秒一次会明显跟不上）。
FALLBACK_INTERVAL_MS = 3000

#: 单次 nvidia-smi 调用的超时上限（秒）。
NVIDIA_SMI_TIMEOUT_SECONDS = 5.0

#: nvidia-smi --query-gpu 的字段顺序，与 parse_nvidia_smi_csv 的解析顺序严格一一对应。
NVIDIA_SMI_FIELDS: tuple[str, ...] = (
    "index",
    "name",
    "memory.used",
    "memory.total",
    "clocks.sm",
    "temperature.gpu",
    "power.draw",
    "power.limit",
)

#: 界面显示「不可用」用的文案。
UNAVAILABLE_TEXT = "不可用"

#: 显存读数回调签名，与 core/process.py 的 VramReader 严格一致。
VramReader = Callable[[], "int | None"]


class MonitorSource(Enum):
    """本轮 GPU 数据实际来自哪里。"""

    NVML = "nvml"
    """NVML（nvidia-ml-py）。首选来源，单次查询在毫秒量级。"""

    NVIDIA_SMI = "nvidia_smi"
    """nvidia-smi 子进程回落。每次查询要新起进程，100~300ms 量级。"""

    UNAVAILABLE = "unavailable"
    """两者都不可用：GPU 监视整体不可用，不是某一项恰好读到了 0。"""

    @property
    def label(self) -> str:
        """中文短名，供界面与日志使用。"""
        return _SOURCE_LABELS[self]

    @property
    def refresh_interval_ms(self) -> int:
        """本来源建议的刷新周期（毫秒）。UNAVAILABLE 沿用回落周期。"""
        return NORMAL_INTERVAL_MS if self is MonitorSource.NVML else FALLBACK_INTERVAL_MS

    @property
    def degraded(self) -> bool:
        """是否处于「已降级」状态（回落或彻底不可用），供界面判断是否标注「刷新已降频」。"""
        return self is not MonitorSource.NVML


_SOURCE_LABELS = {
    MonitorSource.NVML: "NVML",
    MonitorSource.NVIDIA_SMI: "nvidia-smi（已降级）",
    MonitorSource.UNAVAILABLE: "不可用",
}


@dataclass(frozen=True)
class GpuSnapshot:
    """单张 GPU 在某一轮采集里的读数。

    每个数值字段都是 值 | None；None 表示这一项读不到，与「读到的值恰好是 0」严格区分。
    """

    index: int
    name: str | None = None
    mem_used_bytes: int | None = None
    mem_total_bytes: int | None = None
    sm_clock_mhz: int | None = None
    temperature_c: int | None = None
    power_draw_w: float | None = None
    power_limit_w: float | None = None

    @property
    def mem_percent(self) -> float | None:
        """显存占用百分比；已用或总量任一读不到、或总量非正时为 None。"""
        if self.mem_used_bytes is None or self.mem_total_bytes is None:
            return None
        if self.mem_total_bytes <= 0:
            return None
        return self.mem_used_bytes / self.mem_total_bytes * 100.0

    @property
    def display_name(self) -> str:
        """供界面展示的文案：有型号名就带上，否则只用索引。"""
        return f"GPU {self.index}：{self.name}" if self.name else f"GPU {self.index}"


@dataclass(frozen=True)
class SystemSnapshot:
    """一轮完整采集的结果（GPU 列表 + CPU + 内存）。

    :param source: 本轮 GPU 数据的来源；UNAVAILABLE 时 gpus 恒为空元组
    :param gpus: 全部 GPU 的读数，按索引升序
    :param cpu_percent: CPU 利用率（百分比 0~100）；读不到时 None
    :param mem_used_bytes: 系统内存已用字节数；读不到时 None
    :param mem_total_bytes: 系统内存总字节数；读不到时 None
    :param messages: 本轮要告知使用者的消息（降级告知等，只在判定发生的那一轮出现一次）
    """

    source: MonitorSource
    gpus: tuple[GpuSnapshot, ...] = ()
    cpu_percent: float | None = None
    mem_used_bytes: int | None = None
    mem_total_bytes: int | None = None
    messages: tuple[str, ...] = ()

    @property
    def mem_percent(self) -> float | None:
        """系统内存占用百分比；已用或总量任一读不到、或总量非正时为 None。"""
        if self.mem_used_bytes is None or self.mem_total_bytes is None:
            return None
        if self.mem_total_bytes <= 0:
            return None
        return self.mem_used_bytes / self.mem_total_bytes * 100.0

    @property
    def gpu_available(self) -> bool:
        """GPU 监视整体是否可用。"""
        return self.source is not MonitorSource.UNAVAILABLE and bool(self.gpus)

    def gpu_by_index(self, index: int) -> GpuSnapshot | None:
        """按索引取某张 GPU 的读数；不存在时 None。"""
        for gpu in self.gpus:
            if gpu.index == index:
                return gpu
        return None


# ---------------------------------------------------------------------------
# 纯逻辑：nvidia-smi CSV 解析
# ---------------------------------------------------------------------------


def nvidia_smi_command() -> list[str]:
    """拼出 nvidia-smi --query-gpu=... --format=csv,noheader,nounits 命令。纯函数。"""
    return [
        "nvidia-smi",
        f"--query-gpu={','.join(NVIDIA_SMI_FIELDS)}",
        "--format=csv,noheader,nounits",
    ]


def _parse_optional_int(token: str) -> int | None:
    """宽松解析一个整数字段；[N/A] / 空串 / 解不动一律返回 None，不抛异常。"""
    text = token.strip().strip("[]").strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_optional_float(token: str) -> float | None:
    """宽松解析一个浮点字段；规则同 _parse_optional_int。"""
    text = token.strip().strip("[]").strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _mib_to_bytes(value: int | None) -> int | None:
    """MiB 转字节；None 原样传递。"""
    return None if value is None else value * 1024 * 1024


def parse_nvidia_smi_csv(text: str) -> tuple[GpuSnapshot, ...]:
    """解析 nvidia-smi --query-gpu=... --format=csv,noheader,nounits 的输出。

    纯函数，无副作用。字段顺序须与 NVIDIA_SMI_FIELDS 一致。字段值为 [N/A] 或空串时
    该字段记为 None（不是 0、不是抛异常中断整行解析）。行的字段数不足或首字段解不出
    整数时整行跳过。
    """
    snapshots: list[GpuSnapshot] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        tokens = [token.strip() for token in line.split(",")]
        if len(tokens) < len(NVIDIA_SMI_FIELDS):
            continue
        index = _parse_optional_int(tokens[0])
        if index is None:
            continue
        name = tokens[1].strip() or None
        if name is not None and name.upper() == "N/A":
            name = None
        snapshots.append(
            GpuSnapshot(
                index=index,
                name=name,
                mem_used_bytes=_mib_to_bytes(_parse_optional_int(tokens[2])),
                mem_total_bytes=_mib_to_bytes(_parse_optional_int(tokens[3])),
                sm_clock_mhz=_parse_optional_int(tokens[4]),
                temperature_c=_parse_optional_int(tokens[5]),
                power_draw_w=_parse_optional_float(tokens[6]),
                power_limit_w=_parse_optional_float(tokens[7]),
            )
        )
    return tuple(snapshots)


# ---------------------------------------------------------------------------
# 与真实系统打交道：调用真实 nvidia-smi 子进程
# ---------------------------------------------------------------------------


def run_nvidia_smi(timeout: float = NVIDIA_SMI_TIMEOUT_SECONDS) -> bytes:
    """真的跑一次 nvidia-smi 查询，返回原始字节。

    不做解码（本机实测字段全是 ASCII）。stdout 为空时取 stderr。

    :raises FileNotFoundError: 系统上没有 nvidia-smi
    :raises RuntimeError: 超时，或进程没有任何输出
    """
    try:
        completed = subprocess.run(
            nvidia_smi_command(),
            capture_output=True,
            timeout=timeout,
            check=False,
            # 不弹黑窗：打包成 GUI 程序后每次采集闪一下命令行窗口很难看
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"nvidia-smi 查询超过 {timeout} 秒未返回") from exc
    data = completed.stdout or b""
    if not data.strip():
        data = completed.stderr or b""
    if not data.strip():
        raise RuntimeError(f"nvidia-smi 没有任何输出（退出码 {completed.returncode}）")
    return data


# ---------------------------------------------------------------------------
# 纯编排：来源选择与采集
# ---------------------------------------------------------------------------

#: MonitorService 构造参数 nvml_module 的默认哨兵值，代表「自动检测」。
_NVML_AUTO = object()

#: 同上，psutil_module 的自动检测哨兵。
_PSUTIL_AUTO = object()


class MonitorService:
    """一次同步采集：按优先级从 NVML / nvidia-smi 取 GPU 数据，配合 psutil 取 CPU 与内存。

    不自带定时器：调用方（ui/monitor_panel.py）用 QTimer 周期调 poll，并按返回的
    SystemSnapshot.source 调整下一轮间隔。

    来源判定只做一次并缓存：构造后第一次 poll 会依次尝试 NVML、nvidia-smi，结果记在
    backend，后续轮次固定走这条路（除非 NVML 运行期中途失效触发一次性回落重试）。

    :param nvml_module: 注入的 NVML 模块，测试用；不传时真的尝试 import pynvml，
        显式传 None 表示强制当作未安装
    :param smi_runner: 取 nvidia-smi 原始输出字节的可调用对象，默认 run_nvidia_smi
    :param psutil_module: 注入的 psutil 模块，测试用；不传时真的尝试 import psutil
    """

    def __init__(
        self,
        *,
        nvml_module: Any = _NVML_AUTO,
        smi_runner: Callable[[], bytes] | None = None,
        psutil_module: Any = _PSUTIL_AUTO,
    ) -> None:
        self._nvml_override = nvml_module
        self._smi_runner = smi_runner or run_nvidia_smi
        self._psutil_override = psutil_module

        self._backend: MonitorSource | None = None
        self._resolved_nvml: Any = None
        self._backend_reason: str | None = None
        self._downgrade_announced = False
        self._resolved_psutil: Any = None
        self._psutil_resolved = False
        self._psutil_primed = False

    @property
    def backend(self) -> MonitorSource | None:
        """已判定的来源；构造后尚未 poll 过一次时为 None。"""
        return self._backend

    @property
    def nvml_module(self) -> Any:
        """已解析并初始化成功的 NVML 模块；未走 NVML 或尚未 poll 过一次时为 None。

        供 core/gpu_processes.py 复用同一 NVML 句柄（进程级显存枚举），避免二次
        nvmlInit。监控走了 nvidia-smi 回落时这里为 None，调用方会自行 import
        pynvml 再试一次（NVML 初始化失败与「GPU 整体读数走 smi」是两件独立的事）。
        """
        return self._resolved_nvml if self._backend is MonitorSource.NVML else None

    def poll(self) -> SystemSnapshot:
        """采集一轮完整数据。永不抛异常。"""
        gpus, messages = self._collect_gpus()
        cpu_percent, mem_used, mem_total = self._collect_psutil()
        source = self._backend or MonitorSource.UNAVAILABLE
        return SystemSnapshot(
            source=source,
            gpus=gpus,
            cpu_percent=cpu_percent,
            mem_used_bytes=mem_used,
            mem_total_bytes=mem_total,
            messages=tuple(messages),
        )

    # -- GPU 来源判定与采集 -------------------------------------------------

    def _collect_gpus(self) -> tuple[tuple[GpuSnapshot, ...], list[str]]:
        messages: list[str] = []
        if self._backend is None:
            detected = self._detect_backend(messages)
            if detected is not None:
                return detected, messages

        if self._backend is MonitorSource.NVML:
            try:
                return self._collect_nvml(), messages
            except Exception as exc:  # noqa: BLE001 - NVML 运行期失效不该崩掉整轮采集
                messages.append(
                    f"NVML 读取失败（{exc}），本轮改用 nvidia-smi 补救，下一轮仍会先试 NVML"
                )
                return self._collect_smi_once(messages), messages

        if self._backend is MonitorSource.NVIDIA_SMI:
            return self._collect_smi_once(messages), messages

        return (), messages

    def _detect_backend(self, messages: list[str]) -> tuple[GpuSnapshot, ...] | None:
        """首次判定该用哪个来源，结果缓存进 _backend。

        判定 nvidia-smi 能不能用只能真跑一次，所以这一次的读数就是本轮的读数，直接
        交回调用方复用——否则首轮会白起两次 nvidia-smi 进程，正好落在最贵的那条路上。

        :return: NVML 胜出时 None（调用方接着走 NVML 采集），否则本轮 nvidia-smi 读数
        """
        nvml = self._resolve_nvml()
        if nvml is not None:
            try:
                nvml.nvmlInit()
                count = int(nvml.nvmlDeviceGetCount())
            except Exception as exc:  # noqa: BLE001
                self._backend_reason = f"NVML 初始化失败（{exc}）"
            else:
                if count > 0:
                    self._resolved_nvml = nvml
                    self._backend = MonitorSource.NVML
                    return None
                self._backend_reason = "NVML 已初始化但未检测到任何 GPU"
        else:
            self._backend_reason = "未安装 nvidia-ml-py（pynvml）"

        # NVML 这条路不通，回落 nvidia-smi
        self._announce_downgrade(messages)
        gpus = self._collect_smi_once(messages)
        self._backend = MonitorSource.NVIDIA_SMI if gpus else MonitorSource.UNAVAILABLE
        if self._backend is MonitorSource.UNAVAILABLE:
            messages.append("nvidia-smi 也不可用，GPU 监视数据不可用")
        return gpus

    def _announce_downgrade(self, messages: list[str]) -> None:
        """把降级原因写进消息列表，且只写一次。"""
        if self._downgrade_announced:
            return
        self._downgrade_announced = True
        messages.append(f"{self._backend_reason}，已回落到 nvidia-smi（刷新周期放宽到 3 秒）")

    def _collect_nvml(self) -> tuple[GpuSnapshot, ...]:
        """用已判定好的 NVML 模块采集全部 GPU。"""
        nvml = self._resolved_nvml
        count = int(nvml.nvmlDeviceGetCount())
        snapshots: list[GpuSnapshot] = []
        for index in range(count):
            handle = nvml.nvmlDeviceGetHandleByIndex(index)
            snapshots.append(
                GpuSnapshot(
                    index=index,
                    name=_nvml_name(nvml, handle),
                    mem_used_bytes=_nvml_memory_used(nvml, handle),
                    mem_total_bytes=_nvml_memory_total(nvml, handle),
                    sm_clock_mhz=_nvml_sm_clock(nvml, handle),
                    temperature_c=_nvml_temperature(nvml, handle),
                    power_draw_w=_nvml_power_draw(nvml, handle),
                    power_limit_w=_nvml_power_limit(nvml, handle),
                )
            )
        return tuple(snapshots)

    def _collect_smi_once(self, messages: list[str]) -> tuple[GpuSnapshot, ...]:
        """跑一次 nvidia-smi 并解析；失败记消息、返回空元组，不抛异常。"""
        try:
            data = self._smi_runner()
        except Exception as exc:  # noqa: BLE001
            messages.append(f"nvidia-smi 调用失败：{exc}")
            return ()
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        return parse_nvidia_smi_csv(text)

    def _resolve_nvml(self) -> Any:
        """取要用的 NVML 模块；显式传入（含 None）时不再自动 import（供测试注入）。"""
        if self._nvml_override is not _NVML_AUTO:
            return self._nvml_override
        try:
            import pynvml  # 局部导入：环境可能没装 nvidia-ml-py
        except ImportError:
            return None
        return pynvml

    # -- CPU 与内存（走 psutil） ---------------------------------------------

    def _collect_psutil(self) -> tuple[float | None, int | None, int | None]:
        module = self._resolve_psutil()
        if module is None:
            return None, None, None
        try:
            cpu_percent = float(module.cpu_percent(interval=None))
        except Exception:  # noqa: BLE001
            cpu_percent = None
        try:
            vm = module.virtual_memory()
            mem_used = int(vm.total) - int(vm.available)
            mem_total = int(vm.total)
        except Exception:  # noqa: BLE001
            mem_used = None
            mem_total = None
        return cpu_percent, mem_used, mem_total

    def _resolve_psutil(self) -> Any:
        """取要用的 psutil 模块，并在第一次真正拿到模块时打底一次（契约 5）。"""
        if not self._psutil_resolved:
            self._psutil_resolved = True
            if self._psutil_override is not _PSUTIL_AUTO:
                self._resolved_psutil = self._psutil_override
            else:
                try:
                    import psutil  # 局部导入：环境可能没装
                except ImportError:
                    self._resolved_psutil = None
                else:
                    self._resolved_psutil = psutil
        module = self._resolved_psutil
        if module is not None and not self._psutil_primed:
            self._psutil_primed = True
            try:
                module.cpu_percent(interval=None)  # 只是打底，返回值没有意义
            except Exception:  # noqa: BLE001
                pass
        return module

    def shutdown(self) -> None:
        """释放 NVML 资源。"""
        if self._backend is MonitorSource.NVML and self._resolved_nvml is not None:
            try:
                self._resolved_nvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# 与真实系统打交道：NVML 单项读数（每项各自吞异常，互不牵连）
# ---------------------------------------------------------------------------


def _nvml_name(nvml: Any, handle: Any) -> str | None:
    try:
        value = nvml.nvmlDeviceGetName(handle)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace") or None
    return str(value) if value else None


def _nvml_memory_info(nvml: Any, handle: Any) -> Any:
    """取显存信息，优先 v2 口径（与任务管理器 / nvidia-smi 一致）。

    NVML v1 的 nvmlDeviceGetMemoryInfo 把驱动 / 硬件保留显存（本机实测约 428 MiB）
    计进 `used`，导致「已用」比任务管理器虚高约 0.4 GiB、空余被同量低估。v2 把这块
    单列成 `reserved` 不计入 used，used/free 与任务管理器一致。v2 需要新版 NVML +
    nvmlMemory_v2 常量；缺任一则回退 v1（回退后 used 偏高，但 free 与 v2 基本一致，
    对「能否装下」的判定无实质影响）。
    """
    version = getattr(nvml, "nvmlMemory_v2", None)
    if version is not None:
        try:
            return nvml.nvmlDeviceGetMemoryInfo(handle, version=version)
        except Exception:  # noqa: BLE001 - 老驱动 / 老 NVML 不支持 v2，回退 v1
            pass
    return nvml.nvmlDeviceGetMemoryInfo(handle)


def _nvml_memory_used(nvml: Any, handle: Any) -> int | None:
    try:
        return int(_nvml_memory_info(nvml, handle).used)
    except Exception:  # noqa: BLE001
        return None


def _nvml_memory_total(nvml: Any, handle: Any) -> int | None:
    try:
        return int(_nvml_memory_info(nvml, handle).total)
    except Exception:  # noqa: BLE001
        return None


def _nvml_sm_clock(nvml: Any, handle: Any) -> int | None:
    try:
        return int(nvml.nvmlDeviceGetClockInfo(handle, nvml.NVML_CLOCK_SM))
    except Exception:  # noqa: BLE001
        return None


def _nvml_temperature(nvml: Any, handle: Any) -> int | None:
    try:
        return int(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))
    except Exception:  # noqa: BLE001
        return None


def _nvml_power_draw(nvml: Any, handle: Any) -> float | None:
    try:
        # NVML 返回毫瓦；换算成瓦特与 nvidia-smi 的 power.draw 单位保持一致
        return int(nvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0
    except Exception:  # noqa: BLE001
        return None


def _nvml_power_limit(nvml: Any, handle: Any) -> float | None:
    try:
        return int(nvml.nvmlDeviceGetEnforcedPowerLimit(handle)) / 1000.0
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 与 core/process.py 对接：VramReader 签名的显存读数函数
# ---------------------------------------------------------------------------


def make_vram_reader(service: MonitorService, index: int = 0) -> VramReader:
    """产出一个与 core/process.py 的 VramReader 签名兼容的闭包。

    core/process.py 的显存回落等待（VramSettleWatcher）需要一个「返回指定 GPU 已用
    显存字节数或 None」的零参回调；本函数把 MonitorService 与一个具体 GPU 索引绑定成
    这样的回调，可直接传给 ServerProcess(vram_reader=make_vram_reader(service, index=0))。

    闭包本身不抛异常：poll 已保证，这里再包一层 try 只是防御性的。

    :param service: 已构造好的监视服务实例
    :param index: 要读的 GPU 索引，默认 0（单卡场景）
    :return: 零参可调用对象，返回已用显存字节数或 None
    """

    def _read() -> int | None:
        try:
            snapshot = service.poll()
        except Exception:  # noqa: BLE001
            return None
        gpu = snapshot.gpu_by_index(index)
        return gpu.mem_used_bytes if gpu is not None else None

    return _read
