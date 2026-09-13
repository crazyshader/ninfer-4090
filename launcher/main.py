"""ninfer-launcher 入口。

开发模式：python main.py
打包后：exe 直接运行
"""

import sys
from pathlib import Path

import PySide6  # noqa: F401  仅用 __file__ 定位开发模式下的 Qt 翻译目录
from PySide6.QtCore import QTranslator
from PySide6.QtWidgets import QApplication

#: 要安装的 Qt 内置翻译文件（简体中文）。启动器界面文案全部是硬编码中文，但
#: QMessageBox / QInputDialog / 文件对话框的**标准按钮**文案由 Qt 运行时
#: 提供——系统区域为英文时会显示英文 "Yes" / "No" / "OK"，与其余界面割裂。
#: 强制加载本文件后，标准按钮统一为「是 / 否 / 确定 / 取消」，不再依赖系统区域。
_TRANSLATION_FILE = "qtbase_zh_CN.qm"

#: 已安装翻译器的**保活引用**：QTranslator 只持有翻译目录句柄，实例一旦被
#: 回收 Qt 就停止使用这份翻译。模块级全局保证引用覆盖整个进程生命周期。
_TRANSLATOR: QTranslator | None = None


def _translation_candidates() -> tuple[Path, ...]:
    """按优先级返回 Qt 翻译文件候选目录。

    - 打包模式（PyInstaller onedir）：spec 的 datas 把 .qm 打进
      _internal/PySide6/translations/，而 sys._MEIPASS 恰好指向 _internal/。
      注意打包模式下 PySide6.__file__ 指向 PYZ 压缩包内的路径，其「父目录」
      在磁盘上并不存在，所以不能只靠它兜底。
    - 开发模式：已安装包的 PySide6/translations/。
    """
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "PySide6" / "translations")
    candidates.append(Path(PySide6.__file__).resolve().parent / "translations")
    return tuple(candidates)


def _install_chinese_translation(app) -> "QTranslator | None":
    """给应用装上 Qt 内置简体中文翻译（见 _TRANSLATION_FILE 说明）。

    必须在 QApplication 创建之后、**任何窗口 / 对话框创建之前**调用：
    标准按钮文案在按钮构造时按已装翻译器查找，晚装则已建好的对话框仍是英文。

    永不抛异常：翻译文件缺失（如打包产物漏了 spec 里的 datas 条目）时静默
    降级为系统区域按钮，功能不受影响（启动器其余文案本就硬编码中文）。

    返回成功安装的 QTranslator（本模块已持有全局引用保活）；失败返回 None。
    """
    global _TRANSLATOR
    for directory in _translation_candidates():
        qm_path = directory / _TRANSLATION_FILE
        if not qm_path.is_file():
            continue
        translator = QTranslator()
        if translator.load(str(qm_path)):
            app.installTranslator(translator)
            _TRANSLATOR = translator
            return translator
    return None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NInfer Launcher")
    app.setApplicationVersion("1.0.0")
    # 先装中文翻译再建窗口：确认类对话框（未保存改动 / 确认删除 / 确认退出）的
    # Yes/No、警告框的 OK、文件对话框的打开/取消全部来自 Qt 标准按钮，
    # 装上翻译器后才显示为「是 / 否 / 确定 / 取消」。
    _install_chinese_translation(app)

    from ninfer_launcher.ui.main_window import MainWindow

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
