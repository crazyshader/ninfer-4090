"""自定义控件与样式：状态徽章、高亮机制、动态 tooltip、两态复选框。

本模块只提供**控件与纯函数**，不知道任何具体参数是什么——``params_tab.py`` 按
``ParamSpec.kind`` 决定实例化哪个类。高亮颜色来自 ``PARAM_ACCENT`` 常量（无主题
管理器，固定取一个与 UI 协调的色值）。

核心纯函数：
- ``is_value_default(current, default)``：语义级相等判定（Bool3 归一 / float isclose）
- ``build_param_tooltip(spec, value)``：三段式动态 tooltip（落参预览 + 说明 + 默认值）
- ``highlight_stylesheet(accent)``：高亮 QSS 片段

控件类：
- ``Bool3CheckBox``：两态复选框（勾=ON / 不勾=OFF），用于无 off 形式的 BOOL3 参数
- ``NullableSpinBox``：带「启用」开关的整数框，用于 default=None 的 INT 参数
- ``HighlightedSpinBox`` / ``HighlightedComboBox`` / ``HighlightedLineEdit``：带高亮
- ``FlagLabel``：参数行标签（中文标签 + flag），tooltip 由外部动态更新
"""

from __future__ import annotations

import math
from typing import Any, Callable, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from ..params.spec import Bool3, ParamKind, ParamSpec
from ..params.builder import build_for_spec

__all__ = [
    "StatusBadge",
    "PARAM_ACCENT",
    "accent_provider",
    "is_value_default",
    "highlight_stylesheet",
    "build_param_tooltip",
    "Bool3CheckBox",
    "NullableSpinBox",
    "HighlightedSpinBox",
    "HighlightedComboBox",
    "HighlightedLineEdit",
    "FlagLabel",
]

#: 参数高亮强调色（无主题管理器，恒定值）。
PARAM_ACCENT = "#4FC3F7"


#: 可注入的强调色提供者。默认返回 :data:`PARAM_ACCENT`；主题管理器应用后由
#: main_window 调用 :func:`set_accent_provider` 改成「返回当前主题调色板的 accent」，
#: 这样「值≠默认值」高亮的颜色能跟随主题切换（深/浅两套不同强调色）。
_accent_provider: Callable[[], str] | None = None


def set_accent_provider(fn: Callable[[], str] | None) -> None:
    """设置全局强调色提供者。

    传 None 恢复成返回 :data:`PARAM_ACCENT` 的默认行为。主题切换后应调用本函数
    把提供者改成「返回当前主题 accent」，再让各高亮控件重算高亮样式。
    """
    global _accent_provider
    _accent_provider = fn


def accent_provider() -> str:
    """取当前强调色：注入的提供者优先，否则回落到 :data:`PARAM_ACCENT`。"""
    if _accent_provider is not None:
        return _accent_provider()
    return PARAM_ACCENT


# ---------------------------------------------------------------------------
# 纯逻辑：「值≠默认值」判定
# ---------------------------------------------------------------------------


def is_value_default(current: Any, default: Any) -> bool:
    """判定 ``current`` 是否等于 ``default``（语义级，非裸 ``==``）。

    - Bool3 归一后比较（``Bool3.ON`` == ``"on"`` == ``True``）
    - float 用 ``math.isclose``
    - 其余（int / str / None）裸 ``==``
    """
    if isinstance(current, Bool3) or isinstance(default, Bool3):
        try:
            return Bool3.from_config(current) is Bool3.from_config(default)
        except ValueError:
            return current == default
    if isinstance(current, float) or isinstance(default, float):
        if current is None or default is None:
            return current == default
        try:
            return math.isclose(float(current), float(default), rel_tol=1e-9, abs_tol=1e-9)
        except (TypeError, ValueError):
            return current == default
    return current == default


def highlight_stylesheet(accent: str) -> str:
    """生成高亮态 QSS。"""
    return f"color: {accent}; font-weight: bold;"


# ---------------------------------------------------------------------------
# 三段式动态 tooltip
# ---------------------------------------------------------------------------


def _format_default(default: Any) -> str:
    """把默认值格式化为可读字符串。"""
    if isinstance(default, Bool3):
        return "开" if default is Bool3.ON else "关"
    if default is None:
        return "（不指定）"
    return str(default)


def build_param_tooltip(spec: ParamSpec, value: Any) -> str:
    """构建三段式动态 tooltip。

    ① 落参预览：按当前值算出实际会落出的命令行片段
    ② 参数说明：registry 中的中文 tooltip
    ③ 默认值提示
    """
    lines = []

    # ① 落参预览
    emitted = build_for_spec(spec, value)
    if emitted:
        preview = " ".join(emitted)
    else:
        preview = "（不落参）"
    lines.append(f"落参：{preview}")

    # ② 参数说明
    lines.append(spec.tooltip)

    # ③ 默认值
    lines.append(f"默认：{_format_default(spec.default)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 状态徽章
# ---------------------------------------------------------------------------


class StatusBadge(QLabel):
    """状态显示标签，带颜色。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(80)
        self._color = "#9E9E9E"
        self._set_style()

    def set_state(self, label: str, color: str) -> None:
        self._color = color
        self.setText(label)
        self._set_style()

    def _set_style(self) -> None:
        self.setStyleSheet(
            f"color: {self._color}; font-weight: bold; font-size: 13px;"
            f"padding: 4px 12px; border-radius: 4px;"
            f"background-color: {self._color}20;"
        )


# ---------------------------------------------------------------------------
# 两态复选框（BOOL3 纯开关参数）
# ---------------------------------------------------------------------------


class Bool3CheckBox(QCheckBox):
    """两态复选框：勾选=ON（落 flag），不勾选=OFF（不落 flag）。

    适用于没有 off 形式的 BOOL3 参数（lm_head_draft / no_thinking / vision）。
    UNSET 与 OFF 等价（都不落参），所以不需要三态。
    """

    #: 值变化信号，携带新的 Bool3 值。
    valueChanged3 = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = Bool3.OFF
        self.toggled.connect(self._on_toggled)

    def value(self) -> Bool3:
        """当前值（统一接口）。"""
        return self._value

    def set_value(self, value: Any) -> None:
        """设置值（统一接口），接受 Bool3.from_config 能识别的任意形式。"""
        normalized = Bool3.from_config(value)
        target = Bool3.ON if normalized is Bool3.ON else Bool3.OFF
        if target == self._value:
            return
        self.blockSignals(True)
        self.setChecked(target is Bool3.ON)
        self.blockSignals(False)
        self._value = target
        self.valueChanged3.emit(target)

    def _on_toggled(self, checked: bool) -> None:
        target = Bool3.ON if checked else Bool3.OFF
        if target == self._value:
            return
        self._value = target
        self.valueChanged3.emit(target)


# ---------------------------------------------------------------------------
# 可空整数输入框（default=None 的 INT 参数）
# ---------------------------------------------------------------------------


class NullableSpinBox(QWidget):
    """带「启用」开关的整数输入框。

    未勾选 → ``value()`` 返回 ``None``（不落参）；勾选 → 返回 spinbox 值。
    用于 ``vision_max_tokens`` 等 default=None 的 INT 参数。
    """

    valueChanged = Signal()

    def __init__(self, default: int | None = None, *,
        minimum: int = 1, maximum: int = 2147483647, step: int = 1, parent=None) -> None:
        super().__init__(parent)
        self._default = default
        self._check = QCheckBox("启用")
        self._spin = QSpinBox()
        self._spin.setRange(minimum, maximum)
        self._spin.setSingleStep(step)

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(self._check)
        h.addWidget(self._spin, 1)

        if default is not None:
            self._check.setChecked(True)
            self._spin.setValue(int(default))
        else:
            self._check.setChecked(False)

        self._spin.setEnabled(self._check.isChecked())
        self._check.toggled.connect(self._on_check_toggled)
        self._spin.valueChanged.connect(lambda _v: self.valueChanged.emit())

    def _on_check_toggled(self, checked: bool) -> None:
        self._spin.setEnabled(checked)
        self.valueChanged.emit()

    def value(self) -> int | None:
        if not self._check.isChecked():
            return None
        return self._spin.value()

    def set_value(self, value: Any) -> None:
        if value is None:
            self._check.blockSignals(True)
            self._check.setChecked(False)
            self._check.blockSignals(False)
            self._spin.setEnabled(False)
        else:
            self._check.blockSignals(True)
            self._check.setChecked(True)
            self._check.blockSignals(False)
            self._spin.setEnabled(True)
            self._spin.blockSignals(True)
            self._spin.setValue(int(value))
            self._spin.blockSignals(False)

    def refresh_highlight(self) -> None:
        active = not is_value_default(self.value(), self._default)
        if active:
            self.setStyleSheet(highlight_stylesheet(accent_provider()))
        else:
            self.setStyleSheet("")

    def init_highlight(self) -> None:
        self.refresh_highlight()
        self.valueChanged.connect(self.refresh_highlight)


# ---------------------------------------------------------------------------
# 带高亮的控件
# ---------------------------------------------------------------------------


class HighlightedSpinBox(QSpinBox):
    """带「值≠默认值」高亮的整数输入框。

    ``value()`` 是 ``QSpinBox`` 原生方法，未重写；只补 ``set_value`` 统一接口。
    """

    def __init__(self, default: int = 0, *,
        minimum: int | None = None, maximum: int | None = None,
        step: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self._default = default
        self.setMinimum(int(minimum) if minimum is not None else -2_147_483_648)
        self.setMaximum(int(maximum) if maximum is not None else 2_147_483_647)
        if step is not None:
            self.setSingleStep(int(step))
        initial = int(default) if default is not None else 0
        self.setValue(initial)
        self.valueChanged.connect(lambda _v: self.refresh_highlight())
        self.refresh_highlight()

    def set_value(self, value: Any) -> None:
        if value is None:
            return
        self.blockSignals(True)
        self.setValue(int(value))
        self.blockSignals(False)
        self.refresh_highlight()

    def refresh_highlight(self) -> None:
        active = not is_value_default(self.value(), self._default)
        if active:
            self.setStyleSheet(highlight_stylesheet(accent_provider()))
            font = self.font()
            if not font.bold():
                font.setBold(True)
                self.setFont(font)
        else:
            self.setStyleSheet("")
            font = self.font()
            if font.bold():
                font.setBold(False)
                self.setFont(font)


class HighlightedComboBox(QComboBox):
    """带高亮的下拉框，供 ``ParamKind.ENUM`` 使用。

    存储真实值作为 ``itemData``，显示文本可以是中文别名（如 ``""`` → ``（不指定）``）。
    """

    def __init__(self, default: str = "", *,
        choices: Sequence[str] = (), parent=None) -> None:
        super().__init__(parent)
        self._default = default
        for choice in choices:
            label = "（不指定）" if choice == "" else str(choice)
            self.addItem(label, choice)
        idx = self.findData(str(default) if default is not None else "")
        if idx >= 0:
            self.setCurrentIndex(idx)
        self.currentIndexChanged.connect(lambda _i: self.refresh_highlight())
        self.refresh_highlight()

    def value(self) -> Any:
        return self.currentData()

    def set_value(self, value: Any) -> None:
        text = str(value) if value is not None else ""
        idx = self.findData(text)
        if idx >= 0:
            self.blockSignals(True)
            self.setCurrentIndex(idx)
            self.blockSignals(False)
            self.refresh_highlight()

    def refresh_highlight(self) -> None:
        active = not is_value_default(self.value(), self._default)
        if active:
            self.setStyleSheet(highlight_stylesheet(accent_provider()))
        else:
            self.setStyleSheet("")


class HighlightedLineEdit(QLineEdit):
    """带高亮的单行文本框，供 ``ParamKind.PATH`` / ``ParamKind.TEXT`` 使用。"""

    def __init__(self, default: str = "", *, parent=None) -> None:
        super().__init__(parent)
        self._default = default or ""
        self.setText(self._default)
        self.textChanged.connect(lambda _t: self.refresh_highlight())
        self.refresh_highlight()

    def value(self) -> str:
        return self.text()

    def set_value(self, value: Any) -> None:
        self.blockSignals(True)
        self.setText("" if value is None else str(value))
        self.blockSignals(False)
        self.refresh_highlight()

    def refresh_highlight(self) -> None:
        active = not is_value_default(self.text(), self._default)
        if active:
            self.setStyleSheet(highlight_stylesheet(accent_provider()))
        else:
            self.setStyleSheet("")


# ---------------------------------------------------------------------------
# 参数行标签
# ---------------------------------------------------------------------------


class FlagLabel(QLabel):
    """参数行标签：中文标签 + 灰色 flag 名。

    tooltip 由 ``ParamsTab`` 在值变化时通过 ``setToolTip`` 动态更新
    （调用 :func:`build_param_tooltip`）。
    """

    def __init__(self, label_text: str, flag: str = "", parent=None) -> None:
        super().__init__(parent)
        # flag 信息已移至 tooltip 落参预览，标签只显示中文名称
        self.setText(label_text)