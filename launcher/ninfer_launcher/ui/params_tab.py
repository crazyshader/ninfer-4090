"""参数面板：registry 驱动 + 响应式 2 列布局 + 动态 tooltip + 高亮 + 联动。

核心架构：
- ``ParamValueStore``：单一值存放点 + 信号广播，参数值的唯一事实源
- ``ParamsTab``：按 registry 分组生成控件，通过 store 统一管理值
- 值变化 → store.valueChanged → 动态 tooltip 刷新 + 联动禁用
- 响应式：viewport 宽度 >= 860px 时 2 列，否则 1 列
"""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QObject, Qt, Signal, QTimer, QPoint
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..params.spec import Bool3, ParamKind, ParamSpec
from ..params.registry import (
    TAB_PARAMS,
    groups_for_tab,
    default_values,
    get as get_spec,
)
from .widgets import (
    Bool3CheckBox,
    FlagLabel,
    HighlightedComboBox,
    HighlightedLineEdit,
    HighlightedSpinBox,
    NullableSpinBox,
    build_param_tooltip,
)

__all__ = ["ParamValueStore", "ParamsTab"]


# ---------------------------------------------------------------------------
# 单一值存放点
# ---------------------------------------------------------------------------


class ParamValueStore(QObject):
    """参数值的单一存放点 + 变化广播。

    所有参数值读写的唯一入口。控件通过 ``set`` 写入值，
    通过 ``get_all`` 读取全量值。``valueChanged`` 信号在值
    真正变化（经 :func:`is_value_default` 语义判定）时发出。
    """

    valueChanged = Signal(str, object)

    def __init__(self, initial_values: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self._values: dict[str, Any] = dict(initial_values)

    def get(self, key: str) -> Any:
        return self._values.get(key)

    def get_all(self) -> dict[str, Any]:
        return dict(self._values)

    def set(self, key: str, value: Any) -> None:
        if key not in self._values:
            self._values[key] = value
            self.valueChanged.emit(key, value)
            return
        from .widgets import is_value_default
        old = self._values[key]
        if is_value_default(value, old):
            return
        self._values[key] = value
        self.valueChanged.emit(key, value)

    def load(self, values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            self.set(key, value)

    def reset(self) -> None:
        self.load(default_values())


# ---------------------------------------------------------------------------
# 参数面板
# ---------------------------------------------------------------------------


class ParamsTab(QWidget):
    """参数面板：registry 驱动 + 响应式 2 列 + 动态 tooltip + 高亮 + 联动。

    公开接口：
    - ``get_values()`` → ``dict[str, Any]``
    - ``set_values(values)``：批量载入（预设载入 / 重置默认）
    - ``reset_defaults()``：恢复全部参数为出厂默认值
    - ``store``：``ParamValueStore`` 实例（main_window 可直接访问）
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.store = ParamValueStore(default_values(), parent=self)
        self._widgets: dict[str, QWidget] = {}
        self._labels: dict[str, FlagLabel] = {}
        self._groups: dict[str, QGroupBox] = {}
        self._group_layouts: dict[str, QGridLayout] = {}
        self._current_columns: int = 1
        self._search_keywords: str = ""
        self._flash_timer: QTimer | None = None

        self._build_ui()
        self.store.valueChanged.connect(self._on_store_changed)
        self._init_highlights()
        self._apply_linkage()

    # -- 布局构建 --

    def _build_ui(self) -> None:
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        vlayout = QVBoxLayout(container)
        vlayout.setContentsMargins(8, 8, 8, 8)
        vlayout.setSpacing(8)

        groups = dict(groups_for_tab(TAB_PARAMS))
        groups.pop("服务基础", None)  # 模型和端口已在控制面板，参数面板不重复
        for group_name, specs in groups.items():
            box = QGroupBox(group_name)
            grid = QGridLayout(box)
            grid.setSpacing(6)
            self._groups[group_name] = box
            self._group_layouts[group_name] = grid
            vlayout.addWidget(box)
            self._build_group(group_name, specs, grid, self._current_columns)

        vlayout.addStretch()
        self._scroll.setWidget(container)

        # 顶部搜索行（固定在滚动区之上，不随参数内容滚动）
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索参数…（名称 / 命令行旗标，回车定位第一个匹配）")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setFixedWidth(320)
        self._search_edit.textChanged.connect(self._on_search_changed)
        self._search_edit.returnPressed.connect(self._on_search_return)
        self._search_count_label = QLabel("共 12 项")
        self._search_count_label.setProperty("secondary", "true")

        search_row = QHBoxLayout()
        search_row.setContentsMargins(8, 8, 8, 0)
        search_row.addWidget(self._search_edit)
        search_row.addStretch()
        search_row.addWidget(self._search_count_label)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(search_row)
        outer.addWidget(self._scroll)

    def _build_group(
        self, group_name: str, specs: tuple, grid: QGridLayout, columns: int
    ) -> None:
        for i, spec in enumerate(specs):
            row = i // columns
            col = (i % columns) * 2
            label = self._make_label(spec)
            widget = self._make_control(spec)
            self._labels[spec.key] = label
            self._widgets[spec.key] = widget
            label.setToolTip(build_param_tooltip(spec, self.store.get(spec.key)))
            self._connect_widget_to_store(spec.key, widget)
            grid.addWidget(label, row, col)
            grid.addWidget(widget, row, col + 1)
        if columns == 2:
            grid.setColumnStretch(3, 1)
        else:
            grid.setColumnStretch(1, 1)

    # -- 控件创建 --

    def _make_label(self, spec: ParamSpec) -> FlagLabel:
        flag_display = spec.flag if spec.flag != "<model>" else ""
        return FlagLabel(spec.label, flag_display)

    def _make_control(self, spec: ParamSpec) -> QWidget:
        if spec.kind is ParamKind.INT:
            if spec.default is None:
                return NullableSpinBox(
                    default=None,
                    minimum=int(spec.minimum) if spec.minimum is not None else 1,
                    step=int(spec.step) if spec.step else 1,
                )
            return HighlightedSpinBox(
                default=int(spec.default),
                minimum=int(spec.minimum) if spec.minimum is not None else None,
                maximum=int(spec.maximum) if spec.maximum is not None else None,
                step=int(spec.step) if spec.step else None,
            )

        if spec.kind is ParamKind.ENUM:
            return HighlightedComboBox(
                default=spec.default if spec.default is not None else "",
                choices=spec.choices,
            )

        if spec.kind is ParamKind.BOOL3:
            cb = Bool3CheckBox()
            cb.set_value(spec.default)
            return cb

        if spec.kind is ParamKind.PATH:
            return self._make_path_widget(str(spec.default or ""))

        if spec.kind is ParamKind.TEXT:
            return HighlightedLineEdit(default=str(spec.default or ""))

        return HighlightedLineEdit(default="")

    def _make_path_widget(self, default: str) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        edit = HighlightedLineEdit(default=default)
        btn = QPushButton("...")
        btn.setFixedWidth(30)
        btn.setMinimumHeight(32)
        btn.setToolTip("浏览文件...")
        h.addWidget(edit, 1)
        h.addWidget(btn)

        # 统一接口（供 _sync_widgets / _on_widget_value_changed 调用）
        row.value = lambda: edit.value()
        row.set_value = lambda v: edit.set_value(v)
        row.refresh_highlight = lambda: edit.refresh_highlight()
        edit.textChanged.connect(lambda _t: self._on_widget_value_changed_for_path(row))
        btn.clicked.connect(lambda: self._browse(edit))
        return row

    # -- 值流转 --

    def _connect_widget_to_store(self, key: str, widget: QWidget) -> None:
        spec = get_spec(key)
        if spec.kind is ParamKind.INT:
            if isinstance(widget, NullableSpinBox):
                widget.valueChanged.connect(lambda: self._on_widget_changed(key))
            elif hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(lambda _v: self._on_widget_changed(key))
        elif spec.kind is ParamKind.ENUM:
            widget.currentIndexChanged.connect(lambda _i: self._on_widget_changed(key))
        elif spec.kind is ParamKind.BOOL3:
            widget.valueChanged3.connect(lambda _v: self._on_widget_changed(key))
        elif spec.kind is ParamKind.TEXT:
            if hasattr(widget, "textChanged"):
                widget.textChanged.connect(lambda _t: self._on_widget_changed(key))

    def _on_widget_value_changed_for_path(self, row_widget: QWidget) -> None:
        # 找到对应的 key
        for key, w in self._widgets.items():
            if w is row_widget:
                self._on_widget_changed(key)
                break

    def _on_widget_changed(self, key: str) -> None:
        widget = self._widgets.get(key)
        if widget is None:
            return
        value = widget.value()
        self.store.set(key, value)

    def _on_store_changed(self, key: str, value: Any) -> None:
        self._update_tooltip(key)
        self._apply_linkage()

    def _update_tooltip(self, key: str) -> None:
        label = self._labels.get(key)
        if label is None:
            return
        try:
            spec = get_spec(key)
        except KeyError:
            return
        value = self.store.get(key)
        label.setToolTip(build_param_tooltip(spec, value))

    def _apply_linkage(self) -> None:
        values = self.store.get_all()

        spec_val = values.get("spec", "mtp")
        disabled_spec_none = spec_val == "none"
        for key in ("draft_tokens", "lm_head_draft"):
            w = self._widgets.get(key)
            if w:
                w.setEnabled(not disabled_spec_none)

        no_think = values.get("no_thinking", Bool3.OFF)
        if not isinstance(no_think, Bool3):
            no_think = Bool3.from_config(no_think) if no_think is not None else Bool3.OFF
        w = self._widgets.get("reasoning_effort")
        if w:
            w.setEnabled(no_think is not Bool3.ON)

        vision = values.get("vision", Bool3.OFF)
        if not isinstance(vision, Bool3):
            vision = Bool3.from_config(vision) if vision is not None else Bool3.OFF
        w = self._widgets.get("vision_max_tokens")
        if w:
            w.setEnabled(vision is not Bool3.OFF)

    def _init_highlights(self) -> None:
        for widget in self._widgets.values():
            init = getattr(widget, "init_highlight", None)
            if callable(init):
                init()
            else:
                refresh = getattr(widget, "refresh_highlight", None)
                if callable(refresh):
                    refresh()

    # -- 公开接口 --

    def get_values(self) -> dict[str, Any]:
        return self.store.get_all()

    def set_values(self, values: dict[str, Any]) -> None:
        self.store.load(values)
        self._sync_widgets()
        self._apply_linkage()
        for key in self._labels:
            self._update_tooltip(key)

    def _sync_widgets(self) -> None:
        for key, widget in self._widgets.items():
            value = self.store.get(key)
            try:
                widget.set_value(value)
            except (AttributeError, TypeError):
                pass

    def reset_defaults(self) -> None:
        self.store.reset()
        self._sync_widgets()
        self._apply_linkage()
        for key in self._labels:
            self._update_tooltip(key)

    def _browse(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", "", "所有文件 (*)")
        if path:
            edit.setText(path)

    # -- 搜索：过滤 / 定位 / 闪烁 --

    def _on_search_changed(self, text: str) -> None:
        """搜索框内容变化：实时过滤 + 刷新匹配计数。"""
        self._search_keywords = text.strip().lower()
        self._apply_search_filter()

    def _on_search_return(self) -> None:
        """回车：定位（滚动并闪烁）第一个匹配的参数。"""
        first = self._first_match()
        if first is not None:
            self.jump_to_param(first)

    def _matches_keyword(self, key: str) -> bool:
        """判断某参数是否命中当前搜索词（名称 / 旗标 / key，均大小写不敏感）。"""
        if not self._search_keywords:
            return True
        spec = get_spec(key)
        if spec is None:
            return False
        haystacks = (spec.label, spec.flag, spec.key)
        return any(self._search_keywords in h.lower() for h in haystacks if h)

    def _first_match(self):
        """返回第一个命中搜索词的参数 key；无命中返回 None。"""
        for key in self._widgets:
            if self._matches_keyword(key):
                return key
        return None

    def _apply_search_filter(self) -> None:
        """按搜索词显隐各参数与其所属分组；同步匹配计数。"""
        matched = 0
        for group_name, box in self._groups.items():
            group_has_visible = False
            for key, widget in self._widgets.items():
                visible = self._matches_keyword(key)
                label = self._labels.get(key)
                widget.setVisible(visible)
                if label is not None:
                    label.setVisible(visible)
                if visible:
                    group_has_visible = True
                    matched += 1
            box.setVisible(group_has_visible)
        total = len(self._widgets)
        if self._search_keywords:
            self._search_count_label.setText(f"匹配 {matched} / {total} 项")
        else:
            self._search_count_label.setText(f"共 {total} 项")

    def _clear_search(self) -> None:
        """清空搜索框并恢复全部参数可见。"""
        self._search_edit.blockSignals(True)
        self._search_edit.clear()
        self._search_edit.blockSignals(False)
        self._search_keywords = ""
        self._apply_search_filter()

    def jump_to_param(self, key: str) -> None:
        """滚动到指定参数并闪烁高亮其标签。

        :param key: 参数 key；不存在时静默忽略
        """
        label = self._labels.get(key)
        if label is None:
            return
        self._scroll_to_widget(label)
        self._flash_widget(label)

    def _scroll_to_widget(self, widget: QWidget) -> None:
        """把滚动区滚到某控件居中位置（PySide6 未暴露 scrollIntoView，手动定位）。"""
        vb = self._scroll.verticalScrollBar()
        try:
            target = widget.mapTo(self._scroll.widget(), QPoint(0, widget.height() // 2)).y()
        except Exception:  # offscreen / 未布局时几何可能为 0
            target = 0
        viewport_h = self._scroll.viewport().height()
        max_h = vb.maximum()
        if max_h > 0:
            vb.setValue(max(0, min(target - viewport_h // 2, max_h)))

    def _flash_widget(self, widget: QWidget) -> None:
        """短暂闪烁某控件的背景，用于定位提示。600ms 后清空回落主题样式。"""
        from .widgets import accent_provider
        accent = accent_provider()
        widget.setStyleSheet(
            f"QLabel {{ background-color: {accent}; color: #111827; "
            f"border-radius: 4px; padding: 2px; }}"
        )
        if self._flash_timer is None:
            self._flash_timer = QTimer(self)
            self._flash_timer.setSingleShot(True)
            self._flash_timer.timeout.connect(self._end_flash)
        self._flash_timer.start(600)

    def _end_flash(self) -> None:
        """闪烁结束：清空临时背景样式，回落到主题全局 QSS。"""
        for label in self._labels.values():
            label.setStyleSheet("")

    def closeEvent(self, event) -> None:
        """关闭时停止闪烁定时器，避免销毁后回调触发。"""
        if self._flash_timer is not None:
            self._flash_timer.stop()
        super().closeEvent(event)