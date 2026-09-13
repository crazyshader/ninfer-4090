"""验证 ``main._install_chinese_translation`` 让标准对话框按钮显示中文。

回归目标（用户反馈）：启动器界面全是中文，但确认类对话框（确认删除 /
确认退出 / 未保存改动）的按钮由 Qt 运行时提供，系统区域为英文时显示
英文 "Yes" / "No"，与其余界面割裂。``main`` 在启动时装上 Qt 内置
``qtbase_zh_CN.qm``，把标准按钮统一成「是 / 否 / 确定 / 取消」，不再
依赖系统区域。

测试策略：临时把翻译装到会话级 QApplication 上，断言新建
QMessageBox 的标准按钮文案是中文，结束**立刻拆掉**（并复位
``main._TRANSLATOR``），不泄漏给其他用例。
"""

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

import main as launcher_main


#: qtbase_zh_CN 的标准按钮中文文案。不同 Qt / PySide6 版本里按钮 ``text()``
#: 可能带助记符（``是(&Y)`` / ``是(Y)``，渲染出来都是「是」），所以按集合断言，
#: 只钉死「中文」这个本质，不咬死助记符拼写。
EXPECTED_BUTTON_LABELS = {
    QMessageBox.StandardButton.Yes: {"是", "是(Y)", "是(&Y)"},
    QMessageBox.StandardButton.No: {"否", "否(N)", "否(&N)"},
    QMessageBox.StandardButton.Ok: {"确定", "确定(O)"},
    QMessageBox.StandardButton.Cancel: {"取消", "取消(C)"},
}


@pytest.mark.smoke
def test_standard_buttons_translated_to_chinese():
    """装翻译后，Yes/No 显示「是 / 否」，Ok/Cancel 显示「确定 / 取消」。"""
    app = QApplication.instance()
    translator = launcher_main._install_chinese_translation(app)
    if translator is None:
        pytest.skip("环境 PySide6 缺 qtbase_zh_CN.qm（无翻译文件可装）")
    try:
        box = QMessageBox()
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        assert box.button(QMessageBox.StandardButton.Yes).text() in EXPECTED_BUTTON_LABELS[
            QMessageBox.StandardButton.Yes
        ]
        assert box.button(QMessageBox.StandardButton.No).text() in EXPECTED_BUTTON_LABELS[
            QMessageBox.StandardButton.No
        ]

        box2 = QMessageBox()
        box2.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        assert box2.button(QMessageBox.StandardButton.Ok).text() in EXPECTED_BUTTON_LABELS[
            QMessageBox.StandardButton.Ok
        ]
        assert box2.button(QMessageBox.StandardButton.Cancel).text() in EXPECTED_BUTTON_LABELS[
            QMessageBox.StandardButton.Cancel
        ]
    finally:
        app.removeTranslator(translator)
        launcher_main._TRANSLATOR = None


@pytest.mark.smoke
def test_missing_translation_file_degrades_silently(monkeypatch):
    """翻译文件缺失（如打包产物漏了 spec 的 datas 条目）时静默降级、绝不抛异常。"""
    app = QApplication.instance()
    monkeypatch.setattr(launcher_main, "_TRANSLATION_FILE", "qtbase_zh_CN_missing.qm")
    translator = launcher_main._install_chinese_translation(app)
    assert translator is None
    assert launcher_main._TRANSLATOR is None
