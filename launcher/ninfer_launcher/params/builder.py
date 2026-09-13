"""命令行组装：参数值字典 -> list[str]。

纯函数集合：不读文件、不碰环境变量、不改传入的字典。
"""

from typing import Any, Mapping, Sequence

from . import registry
from .spec import Bool3, EmitPolicy, ParamKind, ParamSpec

__all__ = ["build", "build_command", "build_for_spec"]

_ON_VALUE = "on"


def _is_disabled(spec: ParamSpec, values: Mapping[str, Any]) -> bool:
    """检查参数是否因联动规则被禁用。

    disabled_when 是 (key, value) 元组列表：当 values[key] == value 时，该参数被禁用。
    对于 BOOL3 参数，value 是 "on"/"off"/"unset" 字符串形式。
    """
    for (dep_key, dep_value) in spec.disabled_when:
        actual = values.get(dep_key, registry.PARAMS_BY_KEY[dep_key].default if dep_key in registry.PARAMS_BY_KEY else None)
        # 对 BOOL3 值做归一化比较
        if dep_value == "on" and actual in (True, "on", Bool3.ON):
            return True
        if dep_value == "off" and actual in (False, "off", Bool3.OFF):
            return True
        if not isinstance(actual, str) and not isinstance(actual, bool) and actual is not None:
            actual = str(actual)
        if actual == dep_value:
            return True
    return False


def build(
    values: Mapping[str, Any],
    params: Sequence[ParamSpec] | None = None,
) -> list[str]:
    """把参数值字典组装成命令行参数列表（不含 exe 路径）。"""
    specs = registry.ALL_PARAMS if params is None else params
    argv: list[str] = []
    for spec in specs:
        # 联动禁用：参数被禁用时跳过
        if _is_disabled(spec, values):
            continue
        value = values.get(spec.key, spec.default)
        argv.extend(build_for_spec(spec, value))
    return argv


def build_command(
    exe_path: str,
    values: Mapping[str, Any],
    params: Sequence[ParamSpec] | None = None,
) -> list[str]:
    """在 build 的结果前面补上 exe 路径。"""
    program = str(exe_path).strip()
    if not program:
        raise ValueError("exe_path 不能为空")
    return [program] + build(values, params)


def build_for_spec(spec: ParamSpec, value: Any) -> list[str]:
    """组装单个参数，返回它贡献的 0~2 个命令行元素。"""
    policy = spec.emit_policy
    if policy is EmitPolicy.TRISTATE:
        return _emit_tristate(spec, value)
    if policy is EmitPolicy.NON_EMPTY:
        return _emit_non_empty(spec, value)
    return _emit_always(spec, value)


def _emit_always(spec: ParamSpec, value: Any) -> list[str]:
    if value is None:
        return []
    # model 是位置参数，无 flag
    if spec.flag == "<model>":
        if isinstance(value, str) and not value.strip():
            return []
        return [str(value).strip()]
    # ENUM 空串表示"不指定"
    if spec.kind is ParamKind.ENUM and value == "":
        return []
    text = _format_value(spec, value)
    if not text:
        if spec.kind is ParamKind.ENUM:
            return []
        raise ValueError(
            f"{spec.key}（{spec.flag}）：{spec.kind.name} 参数不接受空值"
        )
    return [spec.flag, text]


def _emit_non_empty(spec: ParamSpec, value: Any) -> list[str]:
    if value is None:
        return []
    if spec.flag == "<model>":
        if isinstance(value, str) and not value.strip():
            return []
        return [str(value).strip()]
    text = _format_value(spec, value)
    if not text:
        return []
    return [spec.flag, text]


def _emit_tristate(spec: ParamSpec, value: Any) -> list[str]:
    state = Bool3.from_config(value)
    if state is Bool3.UNSET:
        return []
    if state is Bool3.ON:
        if spec.off_value:
            return [spec.flag, _ON_VALUE]
        return [spec.flag]
    # OFF
    if spec.off_flag:
        return [spec.off_flag]
    if spec.off_value:
        return [spec.flag, spec.off_value]
    # 没有 off 形式：OFF 意味着"不启用"= 默认行为，不落参
    return []


def _format_value(spec: ParamSpec, value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError(
            f"{spec.key}（{spec.flag}）：{spec.kind.name} 参数不接受布尔值"
        )
    return str(value).strip()
