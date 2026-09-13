"""权重显存实测缓存：日志行解析 + 缓存更新的纯逻辑。

背景：core/vram_estimate.py 的权重字节默认值（DEFAULT_WEIGHT_BYTES）是 qwen3.8-27b
的标定值；对其它模型 / 量化档只能算「量级对」。ninfer-serve 启动时会在 stderr 打印
加载进度行（C++ src/product/load_progress/load_progress.cpp，format_line 排版）：

    load weights                     100.00%   16.95 GiB /   16.95 GiB 12.345 s

100.00% 完成时第一个字节数（done）恰好等于权重 H2D 总量，即该模型的真实权重显存。
ui/main_window.py 在 _on_log_line 里用 :func:`parse_weight_bytes_from_log` 解析这一行，
按「模型文件路径 → 权重字节」缓存进 settings.json 的 weight_bytes_cache 字段
（:func:`update_weight_cache` 判定是否需要写盘）。下次对同一模型预检就用实测值，
首跑用标定兜底，换模型自适应。

本模块是纯函数集合（输入文本 / dict，输出 int / bool），不碰文件、不碰日志流，
落 settings.json 的动作由调用方完成——测试无需真实 GPU、也无需真实配置目录。
"""

from __future__ import annotations

import re

__all__ = [
    "parse_weight_bytes_from_log",
    "update_weight_cache",
]

#: load_progress 的 100% 完成行："load weights ... 100.00% <done> <unit> / <total> ..."
#: 取 100% 时的第一个字节数（done == total，即权重 H2D 总量）。
_LOAD_PROGRESS_DONE_RE = re.compile(
    r"load\s+weights\s+100(?:\.\d+)?%\s+(\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB)"
)

#: 兼容文档中提到的另一种摘要行格式："weight H2D 16.95 GiB"（大小写不敏感）。
_WEIGHT_H2D_RE = re.compile(
    r"weight\s+H2D\s+(\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB)",
    re.IGNORECASE,
)

_UNIT_BYTES: dict[str, int] = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024 ** 2,
    "GiB": 1024 ** 3,
    "TiB": 1024 ** 4,
}


def parse_weight_bytes_from_log(line: str) -> int | None:
    """从一行日志里解析权重 H2D 字节数。

    命中 load_progress 的 100% 行或 \"weight H2D <大小>\" 摘要行时返回字节数；
    其它行（未达 100% 的进度行、普通日志、空行）一律返回 None。纯函数、永不抛异常。
    """
    if not line or not isinstance(line, str):
        return None
    for pattern in (_LOAD_PROGRESS_DONE_RE, _WEIGHT_H2D_RE):
        match = pattern.search(line)
        if match is None:
            continue
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        unit = match.group(2)
        scale = _UNIT_BYTES.get(unit)
        if scale is None:
            continue
        return int(value * scale)
    return None


def update_weight_cache(
    cache: dict[str, int], model_path: str, weight_bytes: int
) -> bool:
    """把「模型路径 → 权重字节」写入缓存。纯函数：直接改传入的 dict。

    :return: 缓存内容是否发生变化（调用方据此决定是否把 settings.json 落盘；
        值与旧值相同 / 路径为空 / 字节非正时返回 False，不落盘）。
    """
    if not model_path or weight_bytes is None or weight_bytes <= 0:
        return False
    if cache.get(model_path) == weight_bytes:
        return False
    cache[model_path] = weight_bytes
    return True
