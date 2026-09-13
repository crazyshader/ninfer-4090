"""显存需求估算：配置 -> ninfer-serve 启动常驻显存所需字节数。

纯逻辑模块（对应 core/monitor.py 三段分法的第一段）：输入是从启动器参数抽取的
:class:`VramConfig`，输出 :class:`VramRequirement`；无副作用、不碰系统、永不抛
异常，是单元测试主战场。标定常量来自实测（docs/02-vram-preflight-design.md 附录 A：
RTX 4090 / qwen3.8-27b groupwise-int / rk4v4-e8 + mtp(draft=7) + vision）。

需求模型（仿射，与 C++ 侧 SequenceCapacityCurve 的
`minimum + (pages - min_pages) * 每页增量` 同构）：

    需求 ≈ weight_bytes                    权重常驻（优先取实测缓存，否则 DEFAULT_WEIGHT_BYTES）
         + FIXED_RUNTIME_BYTES             固定开销（GDN state / workspace / graph / persistent）
         + max_context × 每 token KV 字节  随上下文线性
         + safety                          用户可调安全垫，夹紧到 [SAFETY_MIN_BYTES, SAFETY_MAX_BYTES]

每 token KV 按 kv_dtype 分档（见附录 B 待实测校准清单，rk4v4-e8 为实测基准）；
启用 mtp 时叠加 _MTP_KV_MULTIPLIER。标定常量在「vision 开启」状态下测得：关闭
vision 时估算偏保守（略偏高），只会把决策推得更安全，不会产生「误判不足」之外的
风险；安全余量吸收其余估算误差。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..params.spec import Bool3

__all__ = [
    "MIB",
    "GIB",
    "DEFAULT_WEIGHT_BYTES",
    "FIXED_RUNTIME_BYTES",
    "SAFETY_FLOOR_BYTES",
    "SAFETY_MIN_BYTES",
    "SAFETY_MAX_BYTES",
    "DEFAULT_SAFETY_BYTES",
    "clamp_safety_bytes",
    "DEFAULT_KV_DTYPE",
    "DEFAULT_MAX_CONTEXT",
    "VramConfig",
    "VramRequirement",
    "estimate_requirement",
    "config_from_values",
]

MIB = 1024 * 1024
GIB = 1024 ** 3

#: 基准模型权重字节数（qwen3.8-27b groupwise-int，实测 16.95 GiB）。
#: 仅当拿不到该模型的实测权重字节（见 core/weight_cache.py 的日志缓存）时兜底。
DEFAULT_WEIGHT_BYTES = int(16.95 * GIB)

#: 固定运行时开销（与 max_context 无关的那部分 runtime reservation，
#: GDN state + workspace + graph allowance + persistent 其余，实测约 1.12 GiB，
#: 取 1.15 GiB 含少量并发 / graph 波动余量）。
FIXED_RUNTIME_BYTES = int(1.15 * GIB)

#: 安全垫可调范围下限（= 默认值）。原先安全余量取 max(下限, base × 5%)，大上下文下
#: 5% 那条会算到约 1 GiB，把临界配置（硬需求已够、仅差百余 MiB）顶出红线误报不足。
#: 现改为用户在预检面板上可调的安全垫，默认取此最小值 200 MiB（只吸收运行时瞬态），
#: 需要更保守时可上调。「显存不足」也不再阻断启动（改为面板警告），风险可控。
SAFETY_MIN_BYTES = 200 * MIB

#: 安全垫可调范围上限（3 GiB）。足以覆盖高并发 / 大 prompt-cache 快照等保守场景。
SAFETY_MAX_BYTES = 3 * GIB

#: 安全垫默认值（面板控件初值 / settings 缺失时兜底）：取最小值。
DEFAULT_SAFETY_BYTES = SAFETY_MIN_BYTES

#: 向后兼容别名（历史代码 / 测试引用）：等于安全垫下限。
SAFETY_FLOOR_BYTES = SAFETY_MIN_BYTES


def clamp_safety_bytes(value: Any) -> int:
    """把任意输入夹紧到 [SAFETY_MIN_BYTES, SAFETY_MAX_BYTES]；解不动 / None 回落默认值。

    纯函数、永不抛异常：供估算侧与 config_from_values 复用同一套归一规则。
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SAFETY_BYTES
    if v < SAFETY_MIN_BYTES:
        return SAFETY_MIN_BYTES
    if v > SAFETY_MAX_BYTES:
        return SAFETY_MAX_BYTES
    return v

#: 每 token KV 字节的基准档位取值缺失时使用的 kv_dtype（注册表默认值）。
DEFAULT_KV_DTYPE = "rk4v4-e8"

#: max_context 取值缺失 / 非法时的兜底（注册表默认值）。
DEFAULT_MAX_CONTEXT = 163840

#: 每 token 的 text-KV 字节（单请求 / 并发=1），按 kv_dtype 分档。
#: 基准 rk4v4-e8 为实测 17 KiB/token；其余档按 plane 宽度比例推算（待实测回填）。
_KV_BYTES_PER_TOKEN: dict[str, int] = {
    "bf16": 64 * 1024,
    "int8": 33 * 1024,
    "rk8v4": 25 * 1024,
    "rk4v4": 17 * 1024,
    "rk4v4-e8": 17 * 1024,
    "rk2v4-e8": 13 * 1024,
}

#: 启用 mtp 时 KV 需求的乘数（MTP 额外一层 + draft window 页，实测约 +6%）。
_MTP_KV_MULTIPLIER = 1.06

#: 未知 kv_dtype 分档时的兜底：取全档位最大值（决策级宁可偏保守）。
_MAX_KV_BYTES_PER_TOKEN = max(_KV_BYTES_PER_TOKEN.values())


@dataclass(frozen=True)
class VramConfig:
    """影响显存需求的配置字段（从启动器参数中抽取）。

    :param max_context: 最大上下文 token 数（KV cache 随它线性增长）
    :param kv_dtype: KV cache 精度档位（bf16 / int8 / rk8v4 / rk4v4 / rk4v4-e8 / rk2v4-e8）
    :param spec: 投机解码方式，"none" 或 "mtp"
    :param vision: 是否启用视觉能力（标定常量含 vision 开销，关闭时估算偏保守）
    :param weight_bytes: 已知真实权重字节（实测缓存）时传入；None 用 DEFAULT_WEIGHT_BYTES
    :param safety_bytes: 用户可调安全垫字节；None 用 DEFAULT_SAFETY_BYTES，估算时夹紧到
        [SAFETY_MIN_BYTES, SAFETY_MAX_BYTES]
    """

    max_context: int
    kv_dtype: str
    spec: str
    vision: bool
    weight_bytes: int | None = None
    safety_bytes: int | None = None


@dataclass(frozen=True)
class VramRequirement:
    """一次启动预估需要的显存（四项分解，便于界面展示与日后校准）。"""

    weight_bytes: int
    fixed_runtime_bytes: int
    kv_bytes: int
    safety_bytes: int

    @property
    def total_bytes(self) -> int:
        return (
            self.weight_bytes
            + self.fixed_runtime_bytes
            + self.kv_bytes
            + self.safety_bytes
        )


def _kv_bytes_per_token(kv_dtype: str) -> int:
    """查分档表；未知档位取全档最大值兜底（不抛异常、不静默用 0）。"""
    return _KV_BYTES_PER_TOKEN.get(kv_dtype, _MAX_KV_BYTES_PER_TOKEN)


def estimate_requirement(config: VramConfig) -> VramRequirement:
    """纯函数：配置 → 显存需求。无副作用、不碰系统。

    - 权重字节：`weight_bytes` 为正整数时用它，否则回落到 DEFAULT_WEIGHT_BYTES；
    - KV 字节：max(0, max_context) × 分档每 token 字节 ×（mtp ? 1.06 : 1.0）；
    - 安全余量：config.safety_bytes 夹紧到 [SAFETY_MIN_BYTES, SAFETY_MAX_BYTES]（None → 默认）。
    """
    weight = (
        config.weight_bytes
        if config.weight_bytes is not None and config.weight_bytes > 0
        else DEFAULT_WEIGHT_BYTES
    )
    context = max(0, int(config.max_context))
    per_token = _kv_bytes_per_token(config.kv_dtype)
    multiplier = _MTP_KV_MULTIPLIER if config.spec == "mtp" else 1.0
    kv_bytes = int(context * per_token * multiplier)
    safety = (
        DEFAULT_SAFETY_BYTES
        if config.safety_bytes is None
        else clamp_safety_bytes(config.safety_bytes)
    )
    return VramRequirement(
        weight_bytes=weight,
        fixed_runtime_bytes=FIXED_RUNTIME_BYTES,
        kv_bytes=kv_bytes,
        safety_bytes=safety,
    )


def _as_bool3(value: Any) -> Bool3:
    """把参数值域里的三态布尔归一成 Bool3；解不了 / 非法一律回落 UNSET（= 视为关闭）。"""
    try:
        return Bool3.from_config(value)
    except ValueError:
        return Bool3.UNSET


def config_from_values(
    values: Mapping[str, Any],
    weight_bytes: int | None = None,
    safety_bytes: int | None = None,
) -> VramConfig:
    """从 ParamsTab.get_values() 的结果构造 :class:`VramConfig`。纯函数、永不抛异常。

    归一规则（对齐 params/builder.py 的落参语义）：
    - max_context：非正整数回落 DEFAULT_MAX_CONTEXT；
    - kv_dtype：大小写 / 空白归一，空串回落 DEFAULT_KV_DTYPE（分档表查不到时估算
      侧自动取最保守档）；
    - spec：仅 "mtp" 生效，其余（"none" / 空 / 未知）一律 "none"；
    - vision：Bool3 归一，ON → True，OFF / UNSET / 非法 → False。
    """
    raw_context = values.get("max_context")
    try:
        max_context = int(raw_context)
    except (TypeError, ValueError):
        max_context = DEFAULT_MAX_CONTEXT
    if max_context <= 0:
        max_context = DEFAULT_MAX_CONTEXT

    kv_dtype = str(values.get("kv_dtype") or "").strip().lower() or DEFAULT_KV_DTYPE

    raw_spec = values.get("spec")
    if raw_spec is None:
        spec = "mtp"  # 键缺失：按注册表默认值兜底（ParamsTab 的 store 恒有该键，纯防御）
    else:
        spec = str(raw_spec).strip().lower()
        spec = "mtp" if spec == "mtp" else "none"  # 空串 / 未知取值按「未启用」处理（保守方向）

    vision = _as_bool3(values.get("vision")) is Bool3.ON

    return VramConfig(
        max_context=max_context,
        kv_dtype=kv_dtype,
        spec=spec,
        vision=vision,
        weight_bytes=weight_bytes,
        safety_bytes=safety_bytes,
    )
