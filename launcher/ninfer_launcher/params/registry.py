"""参数定义的唯一事实源：12 个参数的完整清单。

界面控件、默认值、落参规则全部由本模块派生。新增参数只需在此加一条 ParamSpec。
"""

from types import MappingProxyType
from typing import Any, Mapping

from .spec import Bool3, ParamKind, ParamSpec

__all__ = [
    "TAB_PARAMS",
    "TAB_ORDER",
    "ALL_PARAMS",
    "PARAMS_BY_KEY",
    "get",
    "params_for_tab",
    "groups_for_tab",
    "default_values",
    "validate_values",
]

TAB_PARAMS = "参数"
TAB_ORDER: tuple[str, ...] = (TAB_PARAMS,)

G_SERVICE = "服务基础"
G_CONTEXT = "上下文与显存"
G_SPECULATIVE = "投机解码"
G_REASONING = "思考与推理"
G_VISION = "视觉"

_INT = ParamKind.INT
_ENUM = ParamKind.ENUM
_BOOL = ParamKind.BOOL3
_PATH = ParamKind.PATH
_TEXT = ParamKind.TEXT

_ON = Bool3.ON
_OFF = Bool3.OFF
_UNSET = Bool3.UNSET

_KV_TYPES: tuple[str, ...] = (
    "bf16",
    "int8",
    "rk8v4",
    "rk4v4",
    "rk4v4-e8",
    "rk2v4-e8",
)

ALL_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        key="model", flag="<model>", kind=_PATH, default="",
        label="模型文件", tab=TAB_PARAMS, group=G_SERVICE,
        tooltip=".ninfer 模型文件路径（位置参数）。MTP 草稿头已内置在模型文件中。",
    ),
    ParamSpec(
        key="port", flag="--port", kind=_INT, default=8080,
        label="监听端口", tab=TAB_PARAMS, group=G_SERVICE,
        tooltip="服务端口。被占用时启动会失败，启动前会先检查。",
        minimum=1, maximum=65535, step=1,
    ),
    ParamSpec(
        key="kv_dtype", flag="--kv-dtype", kind=_ENUM, default="rk4v4-e8",
        label="KV cache 精度", tab=TAB_PARAMS, group=G_CONTEXT,
        tooltip="KV cache 量化精度。rk4v4-e8 为 4-bit E8 晶格量化。",
        choices=_KV_TYPES,
    ),
    ParamSpec(
        key="max_context", flag="--max-context", kind=_INT, default=163840,
        label="最大上下文", tab=TAB_PARAMS, group=G_CONTEXT,
        tooltip="最大上下文 token 数。调大直接增加 KV cache 显存。",
        minimum=1, step=4096,
    ),
    ParamSpec(
        key="prefill_chunk", flag="--prefill-chunk", kind=_INT, default=1024,
        label="预处理分块", tab=TAB_PARAMS, group=G_CONTEXT,
        tooltip="预处理分块大小，必须是 128 的倍数。",
        minimum=128, step=128,
    ),
    ParamSpec(
        key="spec", flag="--spec", kind=_ENUM, default="mtp",
        label="投机解码方式", tab=TAB_PARAMS, group=G_SPECULATIVE,
        tooltip="MTP 投机解码。模型内置 MTP 头，无需额外草稿模型。",
        choices=("none", "mtp"),
    ),
    ParamSpec(
        key="draft_tokens", flag="--draft-tokens", kind=_INT, default=7,
        label="草稿 token 数", tab=TAB_PARAMS, group=G_SPECULATIVE,
        tooltip="一次最多预测的 token 数（1~15）。spec=none 时不生效。",
        minimum=1, maximum=15, step=1,
        disabled_when=(("spec", "none"),),
    ),
    ParamSpec(
        key="lm_head_draft", flag="--lm-head-draft", kind=_BOOL, default=_ON,
        label="LM head 草稿", tab=TAB_PARAMS, group=G_SPECULATIVE,
        tooltip="启用 LM head 草稿加速。spec=none 时不生效。",
        disabled_when=(("spec", "none"),),
    ),
    ParamSpec(
        key="no_thinking", flag="--no-thinking", kind=_BOOL, default=_OFF,
        label="关闭思考模式", tab=TAB_PARAMS, group=G_REASONING,
        tooltip="开启后模型不进入思考模式。关闭时 reasoning-effort 不生效。",
    ),
    ParamSpec(
        key="reasoning_effort", flag="--reasoning-effort", kind=_ENUM, default="low",
        label="推理力度", tab=TAB_PARAMS, group=G_REASONING,
        tooltip="思考模式的推理力度。no-thinking 开启时不生效。",
        choices=("", "low", "medium", "xhigh"),
        disabled_when=(("no_thinking", "on"),),
    ),
    ParamSpec(
        key="vision", flag="--vision", kind=_BOOL, default=_OFF,
        label="视觉能力", tab=TAB_PARAMS, group=G_VISION,
        tooltip="启用模型的图像理解能力（image encoder + vision GPU 分配）。"
                "关闭时视觉输入会返回 vision_disabled 错误。",
    ),
    ParamSpec(
        key="vision_max_tokens", flag="--vision-max-tokens", kind=_INT, default=None,
        label="视觉最大 token", tab=TAB_PARAMS, group=G_VISION,
        tooltip="视觉输入的 token 上限（C++ 默认 8192）。仅在视觉能力开启时生效。",
        minimum=1,
        disabled_when=(("vision", "off"),),
    ),
)

EXPECTED_PARAM_COUNT = 12


def _validate() -> Mapping[str, ParamSpec]:
    by_key: dict[str, ParamSpec] = {}
    seen_flags: dict[str, str] = {}
    for spec in ALL_PARAMS:
        if spec.key in by_key:
            raise ValueError(f"参数 key 重复：{spec.key}")
        if spec.flag in seen_flags:
            raise ValueError(f"参数 flag 重复：{spec.flag}")
        if spec.tab not in TAB_ORDER:
            raise ValueError(f"{spec.key}：未知标签页 {spec.tab!r}")
        by_key[spec.key] = spec
        seen_flags[spec.flag] = spec.key
    if len(ALL_PARAMS) != EXPECTED_PARAM_COUNT:
        raise ValueError(f"参数条目数为 {len(ALL_PARAMS)}，期望 {EXPECTED_PARAM_COUNT}")
    return MappingProxyType(by_key)


PARAMS_BY_KEY: Mapping[str, ParamSpec] = _validate()


def get(key: str) -> ParamSpec:
    try:
        return PARAMS_BY_KEY[key]
    except KeyError:
        raise KeyError(f"未定义的参数 key：{key}") from None


def params_for_tab(tab: str) -> tuple[ParamSpec, ...]:
    if tab not in TAB_ORDER:
        raise ValueError(f"未知标签页：{tab!r}")
    return tuple(spec for spec in ALL_PARAMS if spec.tab == tab)


def groups_for_tab(tab: str) -> "Mapping[str, tuple[ParamSpec, ...]]":
    grouped: dict[str, list[ParamSpec]] = {}
    for spec in params_for_tab(tab):
        grouped.setdefault(spec.group, []).append(spec)
    return MappingProxyType({name: tuple(items) for name, items in grouped.items()})


def default_values() -> dict[str, Any]:
    return {spec.key: spec.default for spec in ALL_PARAMS}


def validate_values(values: Mapping[str, Any]) -> list[str]:
    """校验参数值，返回错误消息列表。"""
    errors: list[str] = []
    for spec in ALL_PARAMS:
        value = values.get(spec.key, spec.default)
        if value is None:
            continue
        if spec.kind is ParamKind.ENUM:
            if value not in spec.choices:
                errors.append(f"{spec.label}：取值 {value!r} 不合法")
        if spec.kind is ParamKind.INT and value is not None:
            try:
                v = int(value)
            except (TypeError, ValueError):
                errors.append(f"{spec.label}：不是有效整数")
                continue
            if spec.minimum is not None and v < int(spec.minimum):
                errors.append(f"{spec.label}：{v} 小于下限 {spec.minimum}")
            if spec.maximum is not None and v > int(spec.maximum):
                errors.append(f"{spec.label}：{v} 超过上限 {spec.maximum}")
            if spec.key == "prefill_chunk" and v % 128 != 0:
                errors.append(f"预处理分块：{v} 不是 128 的倍数")
    return errors