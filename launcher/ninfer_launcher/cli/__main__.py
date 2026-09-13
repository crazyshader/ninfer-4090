"""python -m ninfer_launcher.cli 的包入口（薄壳，逻辑在 main.run）。"""

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
