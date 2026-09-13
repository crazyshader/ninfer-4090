"""ninfer-launcher CLI：无 GUI 的命令行入口（status / start / stop / ensure）。

模块级硬契约（docs/01-ninfer-launcher-cli.md 约束 1，违反即架构事故）：

1. 本包及其所有子模块**禁止 import ninfer_launcher.ui 下的任何模块**。
   一碰 ui 就把 PySide6 拖进来：进程启动白慢几百毫秒，打包版
   （ninfer-launcher-cli.spec 已 excludes PySide6）则会在运行期直接 import 失败。
2. 只允许 import 的纯逻辑模块：ninfer_launcher.core 的
   config / ports / health_probe / process_control / monitor（全部零 Qt 依赖），
   以及 ninfer_launcher.params（registry / builder）。
   注意 **core.health 与 core.process 也不许 import**：这两个模块顶层 import 了
   PySide6（HealthPoller / ServerProcess），对应纯逻辑在
   core.health_probe / core.process_control 里。
3. CLI 只读配置（settings.json / presets/），**绝不写 settings.json**，
   尤其不动 last_preset；运行时数据只落在配置根的 runtime/ 子目录（见 cli/runtime.py）。

输出契约（docs/01-ninfer-launcher-cli.md 第 6 节为唯一事实源）：stdout 只输出一行
JSON，键齐全（缺省为 null），error 是稳定标识符、中文措辞进 message。
"""

__version__ = "1.0.0"
