"""显存预检编排：现状读数 + 需求估算 + 进程清单 → 预检结论。

core 层「纯编排」段（对应 core/monitor.py 三段分法的第二段）：本模块不做任何
系统读取——GPU 快照由调用方（ui/main_window.py）经 MonitorService.poll() 取来、
进程清单经 core/gpu_processes.list_gpu_processes() 枚举、需求经
core/vram_estimate.estimate_requirement() 算出，全部作为参数传入
:func:`evaluate`，返回一个 :class:`PreflightVerdict`。测试因此可以整段用假件
替换系统接触面，不需要真实 GPU、也不需要装 pynvml / psutil。

四态判定（决策级准确度，不追求字节级精确）：

- OK            空余显存 >= 需求 → 放行启动；
- INSUFFICIENT  空余显存 < 需求 → **不阻断启动**，改为面板红色警告 + 给出「退出哪些
                进程能补齐」的降序清单（排除 ninfer 自身与系统关键进程），由用户自行
                决定是否照常启动（需求估算含保守安全余量，临界不足未必真跑不动）；
- UNAVAILABLE   读不到显存（监控降级 / NVML 与 nvidia-smi 皆不可用）→ 降级为
                告警，**不阻断启动**（避免监控故障把用户彻底锁死，风险自负）；
- RUNNING       服务已在运行（server_running=True）→ 预检「冷启动能否装下」无意义
                （当前占用已含本服务的权重 / KV / 运行时），改为中性提示，不做减法、
                不误报不足。优先级最高：服务在跑就返回 RUNNING，不再看显存读数。

四态均放行启动（can_start 恒为 True）：预检只做「知情提示」，不再做硬门控。

周期性调用方（主窗口的 QTimer）每次拿到新读数后重算一次：用户退出占显存的
程序后，下一轮预检就会自动放行「启动」按钮。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .gpu_processes import GpuProcess
from .monitor import GpuSnapshot
from .vram_estimate import VramConfig, VramRequirement, estimate_requirement

__all__ = [
    "PREFLIGHT_INTERVAL_MS",
    "PREFLIGHT_PARAM_DEBOUNCE_MS",
    "PreflightStatus",
    "PreflightVerdict",
    "evaluate",
    "processes_to_close",
    "format_vram_bytes",
]

#: 主窗口周期重算预检的间隔（毫秒）。2~3 秒量级：显存占用是慢变量（用户退程序、
#: 浏览器开页面），不必 1 秒一次；又要在用户退出程序后尽快自动解禁「启动」。
PREFLIGHT_INTERVAL_MS = 2000

#: 参数 / 模型变化后重算预检的去抖延迟（毫秒）。spinbox 连续拖动会连发一串
#: valueChanged，去抖后只在稳定后重算一次（每次重算含一次 NVML poll + 进程枚举）。
PREFLIGHT_PARAM_DEBOUNCE_MS = 150


class PreflightStatus(Enum):
    """预检结论三态。"""

    OK = "ok"
    """显存充足，可启动。"""

    INSUFFICIENT = "insufficient"
    """显存不足，面板红色警告但不阻断启动（缺口与可退出进程清单见 verdict 其余字段）。"""

    UNAVAILABLE = "unavailable"
    """无法预检（读不到显存）：降级为告警，不阻断启动。"""

    RUNNING = "running"
    """服务已在运行：预检（「冷启动能否装下」）此刻无意义——当前占用里已含本服务的
    权重 / KV / 运行时，拿空余去减需求会得出荒谬缺口。改为中性提示，不阻断。"""


@dataclass(frozen=True)
class PreflightVerdict:
    """一次预检的完整结论，主窗口据此门控「启动」按钮 + 预检面板渲染。

    :param status: 三态结论
    :param requirement: 估算需求分解（权重 / 固定 / KV / 安全余量）
    :param free_bytes: 当前空余显存字节；读不到时 None
    :param total_bytes: 显存总量字节；读不到时 None
    :param shortfall_bytes: 还需腾出的字节（OK / UNAVAILABLE 时为 0）
    :param candidates: 建议退出的进程（降序；已排除 ninfer 自身与系统关键进程）
    :param messages: 面向使用者的说明文案（面板 / 对话框直接展示）
    """

    status: PreflightStatus
    requirement: VramRequirement
    free_bytes: int | None = None
    total_bytes: int | None = None
    shortfall_bytes: int = 0
    candidates: tuple[GpuProcess, ...] = ()
    messages: tuple[str, ...] = ()

    @property
    def can_start(self) -> bool:
        """是否放行启动：三态均放行。预检只做知情提示，不再硬门控——显存不足由面板
        红色警告告知，是否启动交给用户决定。"""
        return True


def format_vram_bytes(num_bytes: int | None) -> str:
    """把字节数格式化成人类可读的中文文案（16.95 GiB / 512 MiB / 512 B）。

    None 表示读不到 → 「未知」；负数按 0 处理（防御性，显存读数不应为负）。
    """
    if num_bytes is None:
        return "未知"
    value = float(max(0, num_bytes))
    unit = "B"
    for name in ("KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0:
            break
        value /= 1024.0
        unit = name
    if unit == "B":
        return f"{max(0, int(num_bytes))} B"
    if value >= 100:
        return f"{value:.0f} {unit}"
    return f"{value:.1f}".removesuffix(".0") + f" {unit}"


def _adjustment_tips(config: VramConfig) -> str:
    """「退光进程仍不够」时的配置调整建议。"""
    tips = ["可尝试调低 max_context，或换更省显存的 KV cache 精度（如 rk2v4-e8）"]
    if config.vision:
        tips.append("或关闭视觉能力")
    return "、".join(tips) + "后重试"


def processes_to_close(
    candidates: tuple[GpuProcess, ...], shortfall_bytes: int
) -> tuple[tuple[GpuProcess, ...], int]:
    """从占用最多的候选进程开始累加，求「覆盖缺口」的最小进程集合。纯逻辑。

    :return: (建议退出的进程集合, 覆盖后仍缺的字节数)。
        - 缺口 <= 0：空集合、缺口 0；
        - 累加到某点已覆盖缺口：返回该最小前缀集合，剩余缺口 0；
        - 全部退完仍不够：返回全部候选 + 剩余缺口（调用方据此提示用户改配置）。

    候选里 used_bytes 为 None 的进程不参与累加（占用未知，无从计入「可腾出量」）。
    """
    if shortfall_bytes <= 0:
        return (), 0
    ordered = sorted(
        (p for p in candidates if p.used_bytes),
        key=lambda p: (-p.used_bytes, p.pid),
    )
    total = 0
    picked: list[GpuProcess] = []
    for process in ordered:
        picked.append(process)
        total += process.used_bytes  # type: ignore[operator]
        if total >= shortfall_bytes:
            return tuple(picked), 0
    return tuple(ordered), shortfall_bytes - total


def evaluate(
    config: VramConfig,
    gpu: GpuSnapshot | None,
    processes: tuple[GpuProcess, ...],
    server_running: bool = False,
) -> PreflightVerdict:
    """纯编排：读现状 + 估需求 + 列进程 → 预检结论。不碰系统、永不抛异常。

    :param config: 目标启动配置（权重 / 上下文 / KV 精度 / 投机解码 / 视觉）
    :param gpu: 当前 GPU 读数（MonitorService.poll().gpu_by_index(0)）；None 或
        显存字段读不到 → UNAVAILABLE
    :param processes: 占用显存的进程清单（gpu_processes.list_gpu_processes 的输出）
    :param server_running: 目标服务是否已在运行（自身进程或外部实例）。为真时预检
        「冷启动能否装下」无意义（当前占用已含本服务），直接返回 RUNNING 中性结论；
        优先级高于 UNAVAILABLE——服务在跑就是在跑，读不读得到显存都不影响这个事实。
    """
    requirement = estimate_requirement(config)

    if server_running:
        free = (
            gpu.mem_total_bytes - gpu.mem_used_bytes
            if gpu is not None
            and gpu.mem_total_bytes is not None
            and gpu.mem_used_bytes is not None
            else None
        )
        return PreflightVerdict(
            PreflightStatus.RUNNING,
            requirement,
            free,
            gpu.mem_total_bytes if gpu is not None else None,
            0,
            (),
            ("服务运行中：显存预检仅在启动前评估，当前占用已包含本服务。",),
        )

    if gpu is None or gpu.mem_total_bytes is None or gpu.mem_used_bytes is None:
        return PreflightVerdict(
            PreflightStatus.UNAVAILABLE,
            requirement,
            None,
            None,
            0,
            (),
            ("无法读取显存占用，未做预检；启动风险自负",),
        )

    free = gpu.mem_total_bytes - gpu.mem_used_bytes
    if free >= requirement.total_bytes:
        return PreflightVerdict(
            PreflightStatus.OK,
            requirement,
            free,
            gpu.mem_total_bytes,
            0,
            (),
            (
                f"显存充足：需求 {format_vram_bytes(requirement.total_bytes)}，"
                f"当前空余 {format_vram_bytes(free)}。",
            ),
        )

    shortfall = requirement.total_bytes - free
    candidates = tuple(
        p
        for p in processes
        if not p.is_self and not p.is_protected and p.used_bytes
    )
    close_set, remaining = processes_to_close(candidates, shortfall)
    messages = [
        f"目标需要 {format_vram_bytes(requirement.total_bytes)}，"
        f"当前空余 {format_vram_bytes(free)}，还差 {format_vram_bytes(shortfall)}。"
    ]

    if candidates:
        top = ", ".join(
            f"{p.name} {format_vram_bytes(p.used_bytes)}" for p in candidates[:3]
        )
        messages.append(f"占用最多：{top}。")
        if remaining <= 0:
            freed = sum(p.used_bytes for p in close_set if p.used_bytes)
            names = "、".join(p.name for p in close_set)
            messages.append(
                f"退出 {len(close_set)} 个进程可腾出 {format_vram_bytes(freed)}（{names}），即可满足。"
            )
        else:
            total = sum(p.used_bytes for p in candidates if p.used_bytes)
            messages.append(
                f"即使退出全部 {len(candidates)} 个可退进程（共 {format_vram_bytes(total)}）"
                f"仍差 {format_vram_bytes(remaining)}：{_adjustment_tips(config)}。"
            )
    else:
        messages.append("未发现可退出的进程——请手动关闭占用显存的程序（任务管理器可查）")
        messages.append(f"即使如此仍差 {format_vram_bytes(shortfall)}：{_adjustment_tips(config)}。")

    return PreflightVerdict(
        PreflightStatus.INSUFFICIENT,
        requirement,
        free,
        gpu.mem_total_bytes,
        shortfall,
        candidates,
        tuple(messages),
    )
