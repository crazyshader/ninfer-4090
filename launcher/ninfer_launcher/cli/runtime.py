"""CLI 运行时文件层的兼容层：实现已迁至 core/pid_file.py。

2026-09 起，进程登记表 / 启动互斥锁 / 服务日志这一整套文件层从本模块上移到
core/pid_file.py（零 Qt 依赖）：GUI 主窗口每秒的外部服务对账（识别 CLI / 外部
启动的服务并同步四个控制按钮）与「停止外部实例」也需要读写这份登记表，放进
core 才能保证 GUI 与 CLI 共享同一实现。本模块只保留再导出，既有的导入路径
（ninfer_launcher.cli.runtime 及其成员）继续有效。
"""

from __future__ import annotations

from ..core.pid_file import *  # noqa: F401,F403
from ..core.pid_file import __all__ as __all__  # noqa: F401
