"""日志面板：实时显示服务器输出与启动器消息，支持导出。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QPlainTextEdit,
    QFileDialog,
    QWidget,
    QVBoxLayout,
)

from ..core.process import LogLine, LogSource

__all__ = ["LogPanel"]

_COLOR_STDERR = "#FF6B6B"
_COLOR_STDOUT = "#95E1D3"
_COLOR_LAUNCHER = "#FFD93D"
_COLOR_DEFAULT = "#D1D5DB"


class LogPanel(QWidget):
    """日志面板：只读文本 + 导出按钮。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(5000)
        font = self._text.font()
        font.setFamily("Consolas")
        font.setPointSize(9)
        self._text.setFont(font)

        bar = QHBoxLayout()
        self._btn_export = QPushButton("导出日志")
        self._btn_export.setFixedWidth(80)
        self._btn_export.clicked.connect(self._on_export)
        self._btn_clear = QPushButton("清空")
        self._btn_clear.setFixedWidth(60)
        self._btn_clear.clicked.connect(self._text.clear)
        bar.addStretch()
        bar.addWidget(self._btn_clear)
        bar.addWidget(self._btn_export)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._text)
        layout.addLayout(bar)

    def append_line(self, line: LogLine) -> None:
        if line.source is LogSource.STDERR:
            color = _COLOR_STDERR
        elif line.source is LogSource.STDOUT:
            color = _COLOR_STDOUT
        else:
            color = _COLOR_LAUNCHER
        self._text.appendHtml(f'<span style="color:{color};">{line.formatted}</span>')

    def text(self) -> str:
        return self._text.toPlainText()

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", "server.log", "文本文件 (*.txt *.log)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._text.toPlainText())
