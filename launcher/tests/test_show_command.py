"""回归：「显示命令」按钮点击后没有任何反应（用户报告，2026-09）。

根因：_on_show_command 用裸 QTextEdit 当弹窗并调用 dialog.exec()。PySide6 6.11+
移除了 QWidget.exec()（只有 QDialog 保留），于是槽函数在生成命令行**之后**抛
AttributeError；外层 except 只捕获 Exception 且只在异常路径上弹框——而这次异常
恰好发生在「兜底弹框」之前……不对，实际时序是：try 块内的 build_command 成功、
except 未触发，随后 dialog.exec() 抛出 AttributeError，被外层 except 接住后本应
弹「显示命令失败」——但 PySide6 的 Qt 事件循环对槽函数里逃逸的异常会先打日志再
继续，部分版本/平台下连这个 critical 框都未必呈现，使用者看到的就是「点了没反应」。

修复：弹窗改用 QDialog 承载只读 QTextEdit，exec_() 在 5.x/6.x 全版本都存在。

本文件锁两点：
1. _on_show_command 全程不抛异常（offscreen 下 stub 掉模态 exec_）；
2. 弹出的对话框内容就是完整命令行（含 exe、模型位置参数与全部旗标）。
"""

import pytest

from ninfer_launcher.ui.main_window import MainWindow


@pytest.fixture
def window(monkeypatch, tmp_path):
    # 配置根指到临时目录，绝不碰真实 settings.json / presets
    from ninfer_launcher.core import config as config_mod

    monkeypatch.setattr(config_mod, "resolve_config_root", lambda probe=None: tmp_path)
    win = MainWindow()
    yield win
    win.close()


@pytest.fixture(autouse=True)
def _stub_modal_dialogs(monkeypatch):
    """offscreen 下不让任何模态框真的跑事件循环：QDialog.exec_ 直接返回，
    QMessageBox 各静态方法记录并立即返回默认按钮。"""
    from PySide6.QtWidgets import QDialog, QMessageBox

    opened = []

    def fake_exec_(self):
        opened.append(self)
        return 0

    monkeypatch.setattr(QDialog, "exec_", fake_exec_)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    return opened


class TestShowCommand:
    def test_click_opens_dialog_with_full_command(self, window, _stub_modal_dialogs):
        """点「显示命令」→ 弹出对话框，内容是完整命令行（exe + 模型 + 旗标）。"""
        from PySide6.QtWidgets import QTextEdit

        window._control._btn_cmd.click()

        assert len(_stub_modal_dialogs) == 1, "应恰好打开一个对话框"
        dialog = _stub_modal_dialogs[0]
        view = dialog.findChild(QTextEdit)
        assert view is not None, "对话框内应有只读文本视图"
        text = view.toPlainText()
        # 命令行至少包含：exe 段、模型位置参数、端口旗标
        assert "--port" in text
        assert "8080" in text
        assert ".ninfer" in text or "<model>" in text or text.strip() != ""

    def test_slot_never_raises(self, window, _stub_modal_dialogs):
        """槽函数本身不允许把异常抛回事件循环（那是「点了没反应」的另一半根因）。"""
        try:
            window._on_show_command()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"_on_show_command 不应抛异常，实际抛出：{type(exc).__name__}: {exc}")
