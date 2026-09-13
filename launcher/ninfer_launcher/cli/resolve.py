"""参数源解析：预设 → 参数值 → exe 路径（docs/01-ninfer-launcher-cli.md 第 4 节）。

优先级（唯一事实源，本模块不许另造默认值）：

- 预设：--preset → settings.last_preset → settings.params 快照 → 报 no-preset；
- 参数值：预设的 params 子字典为基底；model / port 取预设**顶层或 params 内**
  （两处都查，与 config.load_preset 的兼容约定一致，顶层优先）；缺省的键由
  params/registry.py 的默认值补齐（registry 是默认值的唯一事实源）；
- exe：--exe → settings.exe_path → 项目根自动探测
  （find_project_root()/build-ninja/apps/ninfer-serve.exe）。

本模块只做「读出与合并」，不做校验：validate_values（params/registry.py）、
模型文件 isfile、exe isfile 都由调用方（cli/actions.py）按顺序执行，
保证失败时的错误码与 docs 第 7.2 节的步骤一一对应。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..core import config
from ..params import registry

__all__ = [
    "ResolveError",
    "LaunchPlan",
    "resolve_preset",
    "merge_values",
    "resolve_launch",
    "resolve_exe",
    "resolve_port",
]


class ResolveError(Exception):
    """参数解析失败；code 是稳定错误码（调用方直接放进 CliResult.error）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LaunchPlan:
    """解析结果：一份「拿它就能组装命令行」的完整启动计划。"""

    preset_name: str | None
    preset_source: str  # "cli" / "last_preset" / "settings"
    values: dict[str, Any]  # 合并后的原始值（缺省键留给 registry 默认值 / builder 处理）
    model: str | None  # 规范化后（strip 过）；None 表示未设置
    port: int | None  # 转换失败时为 None，交给 validate_values 报 invalid-params
    exe: str


def resolve_preset(
    root: Path, preset_name: str | None = None
) -> tuple[str | None, dict[str, Any] | None, str]:
    """解析「用哪个预设」。返回 (预设名或 None, 预设内容或 None, 来源)。

    失败抛 ResolveError(ERR_NO_PRESET)。last_preset 指向已删除的预设时不报错，
    继续走 params 快照（GUI 保存的参数快照是最后一道可用的参数来源）。
    """
    settings = config.load_settings(root)
    if preset_name:
        data = config.load_preset(root, preset_name)
        if data is None:
            from .result import ERR_NO_PRESET

            raise ResolveError(
                ERR_NO_PRESET,
                "预设 %r 不存在（请检查 --preset 名称，或在 GUI 中创建）" % preset_name,
            )
        return preset_name, data, "cli"
    if settings.last_preset:
        data = config.load_preset(root, settings.last_preset)
        if data is not None:
            return settings.last_preset, data, "last_preset"
    if settings.params:
        return None, dict(settings.params), "settings"
    from .result import ERR_NO_PRESET

    raise ResolveError(
        ERR_NO_PRESET,
        "没有可用预设：请传 --preset <名字>，或先在 GUI 里配置预设 / 保存参数",
    )


def merge_values(preset_data: Mapping[str, Any] | None) -> dict[str, Any]:
    """预设内容 → 原始参数值。

    params 子字典为基底；顶层 model / port 兼容（config.load_preset 的旧格式
    允许 model/port 放顶层），顶层优先于 params 内同名键。
    """
    values: dict[str, Any] = {}
    if preset_data:
        params = preset_data.get("params")
        if isinstance(params, dict):
            values.update(params)
        for key in ("model", "port"):
            top = preset_data.get(key)
            if top is not None:
                values[key] = top
    return values


def _port_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def resolve_port(root: Path, preset_name: str | None = None) -> int:
    """健康检查端口：--preset 的端口 → last_preset 的端口 → settings.params 的端口 → 8080。

    status / stop / ensure 快速路径用它定位「服务应该在哪个端口」。
    """
    settings = config.load_settings(root)
    candidates: list[str] = []
    if preset_name:
        candidates.append(preset_name)
    elif settings.last_preset:
        candidates.append(settings.last_preset)
    for name in candidates:
        data = config.load_preset(root, name)
        if data:
            port = _preset_port(data)
            if port is not None:
                return port
    if settings.params:
        port = _port_int(settings.params.get("port"))
        if port is not None:
            return port
    return int(registry.PARAMS_BY_KEY["port"].default)


def _preset_port(data: Mapping[str, Any]) -> int | None:
    port = None
    params = data.get("params")
    if isinstance(params, Mapping) and params.get("port") is not None:
        port = params.get("port")
    if data.get("port") is not None:
        port = data.get("port")
    return _port_int(port)


def resolve_launch(
    root: Path,
    *,
    preset_name: str | None = None,
    exe_arg: str | None = None,
) -> "LaunchPlan":
    """完整解析一次启动：预设 → 参数值 → 模型 / 端口 / exe。"""
    name, preset_data, source = resolve_preset(root, preset_name)
    if source == "settings":
        values = dict(preset_data or {})
    else:
        values = merge_values(preset_data)

    model = values.get("model")
    if isinstance(model, str):
        model = model.strip() or None
    elif model is not None:
        model = str(model)

    port_value = values.get("port", registry.PARAMS_BY_KEY["port"].default)
    port = _port_int(port_value)  # 转换失败 → None，由 validate_values 报 invalid-params
    exe = resolve_exe(root, exe_arg)
    return LaunchPlan(
        preset_name=name,
        preset_source=source,
        values=values,
        model=model,
        port=port,
        exe=exe,
    )


def resolve_exe(root: Path, exe_arg: str | None = None) -> str:
    """exe 路径：--exe → settings.exe_path → 项目根自动探测。

    显式给出的路径原样返回（存在性由调用方检查，好报准确的 exe-not-found）；
    自动探测落空才抛 ResolveError(ERR_EXE_NOT_FOUND)。
    """
    if exe_arg and exe_arg.strip():
        return os.path.abspath(os.path.expanduser(exe_arg.strip()))
    settings = config.load_settings(root)
    if settings.exe_path.strip():
        return os.path.abspath(os.path.expanduser(settings.exe_path.strip()))
    candidate = config.find_project_root() / "build-ninja" / "apps" / "ninfer-serve.exe"
    if candidate.is_file():
        return str(candidate)
    from .result import ERR_EXE_NOT_FOUND

    raise ResolveError(
        ERR_EXE_NOT_FOUND,
        "无法自动定位 ninfer-serve.exe（期望在 <项目根>/build-ninja/apps/ 下，已尝试 %s）；"
        "可用 --exe 显式指定，或在 GUI 里设置 exe 路径" % candidate,
    )
