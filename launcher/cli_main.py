"""ninfer-launcher CLI 入口（PyInstaller 打包用；开发模式用 python -m ninfer_launcher.cli）。

注意：本入口**不得 import PySide6 / ninfer_launcher.ui**（零 Qt 约束，见
ninfer_launcher/cli/__init__.py 顶部注释）。打包 spec 已把 PySide6 放进 excludes，
不小心 import ui 会在构建期直接报错。
"""

import sys

from ninfer_launcher.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
