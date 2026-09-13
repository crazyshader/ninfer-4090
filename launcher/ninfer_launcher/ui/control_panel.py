"""控制面板：响应式双列布局 + 通用设置组 + 预设组 + 服务器控制组。

布局（宽 >= 860px 双列，窄 < 860px 单列）：
- 左列：「通用设置」QGroupBox（主题/Exe路径/模型文件/端口）+「预设配置」QGroupBox
- 右列：「服务器控制」QGroupBox（启动/停止/打开WebUI/显示命令）+ 资源监视面板

预设组：单一「当前预设」下拉（全部预设平铺）+ 四按钮
〔保存配置〕〔另存为〕〔删除〕〔重置默认〕
选中即载入（``presetSelected`` 信号），真正存取由 main_window 处理。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt, QEvent, QObject, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.config import find_project_root
from ..core.process import ServerState
from ..core.monitor import MonitorService
from .gpu_power_switch import GpuPowerSwitch
from .monitor_panel import MonitorPanel

__all__ = ["ControlPanel"]

_STATE_COLORS = {
    ServerState.STOPPED: "#9E9E9E",
    ServerState.STARTING: "#FFD93D",
    ServerState.RUNNING: "#6BCB77",
    ServerState.STOPPING: "#FF6B6B",
}

_BREAKPOINT = 860


class _PresetGroup(QWidget):
    """预设配置组：当前预设下拉 + 四按钮。

    对 ConfigStore 一无所知——只负责「显示哪些名字、选中了哪个、点了哪个按钮」。
    真正的存取、只读保护、未保存改动确认全部由 main_window 处理。
    """

    presetSelected = Signal(str)
    saveRequested = Signal()
    saveAsRequested = Signal()
    deleteRequested = Signal()
    resetRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        box = QGroupBox("预设配置")
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)

        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        # 上排：当前预设下拉 + 另存为 + 删除
        top = QHBoxLayout()
        top.addWidget(QLabel("当前预设"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(160)
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo.activated.connect(self._on_activated)
        top.addWidget(self.combo, 1)
        self._btn_save_as = QPushButton("另存为")
        self._btn_save_as.setMinimumHeight(32)
        self._btn_save_as.clicked.connect(self.saveAsRequested.emit)
        top.addWidget(self._btn_save_as)
        self._btn_delete = QPushButton("删除")
        self._btn_delete.setMinimumHeight(32)
        self._btn_delete.clicked.connect(self.deleteRequested.emit)
        top.addWidget(self._btn_delete)
        layout.addLayout(top)

        # 下排：保存配置 / 重置默认（等宽 2 列）
        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        self._btn_save = QPushButton("保存配置")
        self._btn_save.setMinimumHeight(32)
        self._btn_save.setToolTip("把当前参数覆盖保存到选中的预设")
        self._btn_save.clicked.connect(self.saveRequested.emit)
        self._btn_reset = QPushButton("重置默认")
        self._btn_reset.setMinimumHeight(32)
        self._btn_reset.setToolTip("把全部 12 项参数恢复为出厂默认值")
        self._btn_reset.clicked.connect(self.resetRequested.emit)
        for i, btn in enumerate((self._btn_save, self._btn_reset)):
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            grid.addWidget(btn, 0, i)
            grid.setColumnStretch(i, 1)
        layout.addLayout(grid)

    def _on_activated(self, index: int) -> None:
        name = self.combo.itemData(index)
        if name:
            self.presetSelected.emit(name)

    def set_preset_names(self, names: Sequence[str], current: str | None = None) -> None:
        """重建下拉框并恢复选中。

        期望选中的预设（``current``，为 None 时取此前选中的项）仍在列表 → 选中它；
        它已被删除/不存在 → 落到**第一个可用预设**（即「下一个」），这样删除当前预设
        后下拉框不会停在空态、而是指向下一个可选项；只有当一个预设都不剩时才置空
        （index -1）。
        """
        previous = self.combo.currentData()
        self.combo.blockSignals(True)
        try:
            self.combo.clear()
            if not names:
                self.combo.addItem("（无预设）", None)
            else:
                for name in names:
                    self.combo.addItem(name, name)
            target = current if current is not None else (previous if isinstance(previous, str) else None)
            if target is not None:
                idx = self.combo.findData(target)
                # 目标还在 → 选中；已被删 → 落到第一个可用（删光了才置空）
                self.combo.setCurrentIndex(idx if idx >= 0 else (0 if names else -1))
            else:
                # 无历史选中（首次启动）：默认选第一个
                self.combo.setCurrentIndex(0 if names else -1)
        finally:
            self.combo.blockSignals(False)

    def current_preset_name(self) -> str | None:
        data = self.combo.currentData()
        return data if isinstance(data, str) else None

    def select_preset_silently(self, name: str | None) -> None:
        if name is None:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(-1)
            self.combo.blockSignals(False)
            return
        idx = self.combo.findData(name)
        if idx >= 0:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(idx)
            self.combo.blockSignals(False)


class ControlPanel(QWidget):
    """控制面板：响应式双列 + 通用设置组 + 预设组 + 服务器控制组。

    信号：
    - start_requested / stop_requested / open_webui_requested
    - show_command_requested：显示命令对话框
    - model_changed(str) / port_changed(int) / exe_path_changed(str)
    """

    start_requested = Signal()
    stop_requested = Signal()
    open_webui_requested = Signal()
    show_command_requested = Signal()
    model_changed = Signal(str)
    port_changed = Signal(int)
    exe_path_changed = Signal(str)

    def __init__(
        self,
        model_dir: str = "E:\\ai\\ninfer-4090",
        monitor_service: MonitorService | None = None,
        gpu_power_switch: "GpuPowerSwitch | None" = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._model_dir = model_dir
        self._port = 8080
        self._exe_path = ""
        self._monitor = (
            monitor_service if monitor_service is not None else MonitorService()
        )
        self._monitor_panel = MonitorPanel(service=self._monitor, auto_start=True)
        self._preset_group = _PresetGroup()
        self._theme_combo = QComboBox()
        self._theme_combo.addItems(["深色", "浅色", "跟随系统"])
        self._theme_combo.setFixedWidth(110)

        # 性能模式开关（NVIDIA 驱动电源管理模式）：由 main_window 构造并注入读写回调，
        # 本面板只负责把它摆进布局（契约见 ui/gpu_power_switch.py）。未注入时回退到默认
        # 实例（真实驱动实现；测试里由 conftest 全局拦掉，不会碰本机显卡驱动）。
        self._gpu_power_switch = (
            gpu_power_switch if gpu_power_switch is not None else GpuPowerSwitch()
        )

        # 按钮
        self._btn_start = QPushButton("启动")
        self._btn_start.setMinimumHeight(32)
        self._btn_start.clicked.connect(self.start_requested.emit)
        self._btn_stop = QPushButton("停止")
        self._btn_stop.setMinimumHeight(32)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self.stop_requested.emit)
        self._btn_webui = QPushButton("打开 WebUI")
        self._btn_webui.setMinimumHeight(32)
        self._btn_webui.setEnabled(False)
        self._btn_webui.clicked.connect(self.open_webui_requested.emit)
        self._btn_cmd = QPushButton("显示命令")
        self._btn_cmd.setMinimumHeight(32)
        self._btn_cmd.setToolTip("查看将要执行的完整命令行")
        # 初始态 STOPPED：显示命令可用（与 apply_state(STOPPED) 的矩阵一致）。
        self._btn_cmd.setEnabled(True)
        self._btn_cmd.clicked.connect(self.show_command_requested.emit)

        # 模型 / 端口 / exe
        self._model_combo = QComboBox()
        self._model_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._model_combo.currentTextChanged.connect(self._on_model_selected)
        self._btn_browse = QPushButton("浏览...")
        self._btn_browse.setMinimumHeight(32)
        self._btn_browse.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._btn_browse.clicked.connect(self._on_browse_model)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(8080)
        self._port_spin.valueChanged.connect(self._on_port_changed)
        self._exe_edit = QLineEdit()
        self._exe_edit.setPlaceholderText("留空则自动探测")
        self._exe_edit.textChanged.connect(self._on_exe_changed)
        self._btn_browse_exe = QPushButton("浏览...")
        self._btn_browse_exe.setMinimumHeight(32)
        self._btn_browse_exe.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._btn_browse_exe.clicked.connect(self._on_browse_exe)

        self._build_ui()
        self._scan_models()
        self._auto_detect()

    # -- 布局 --

    def _build_ui(self) -> None:
        # 左列：通用设置 + 预设
        self._left = QWidget()
        left_layout = QVBoxLayout(self._left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        # 通用设置组
        general_box = QGroupBox("通用设置")
        general_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        form = QFormLayout(general_box)
        form.setSpacing(6)
        form.addRow("主题", self._theme_combo)
        # 性能模式开关：勾选后让 NVIDIA 驱动电源管理模式切到「最高性能优先」。
        form.addRow("性能模式", self._gpu_power_switch)

        # Exe 路径：edit + 浏览按钮（置于模型文件上方）
        exe_widget = QWidget()
        exe_h = QHBoxLayout(exe_widget)
        exe_h.setContentsMargins(0, 0, 0, 0)
        exe_h.setSpacing(4)
        exe_h.addWidget(self._exe_edit, 1)
        exe_h.addWidget(self._btn_browse_exe)
        form.addRow("Exe 路径", exe_widget)

        # 模型文件：combo + 浏览按钮
        model_widget = QWidget()
        model_h = QHBoxLayout(model_widget)
        model_h.setContentsMargins(0, 0, 0, 0)
        model_h.setSpacing(4)
        model_h.addWidget(self._model_combo, 1)
        model_h.addWidget(self._btn_browse)
        form.addRow("模型文件", model_widget)

        form.addRow("端口", self._port_spin)

        left_layout.addWidget(general_box)
        left_layout.addWidget(self._preset_group)
        left_layout.addStretch()

        # 右列：服务器控制 + 资源监视
        self._right = QWidget()
        right_layout = QVBoxLayout(self._right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        # 服务器控制组
        server_box = QGroupBox("服务器控制")
        server_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        server_grid = QGridLayout(server_box)
        server_grid.setHorizontalSpacing(6)
        server_grid.setVerticalSpacing(6)
        buttons = (
            (self._btn_start, self._btn_stop),
            (self._btn_webui, self._btn_cmd),
        )
        for r, (b1, b2) in enumerate(buttons):
            b1.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            server_grid.addWidget(b1, r, 0)
            b2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            server_grid.addWidget(b2, r, 1)
            server_grid.setColumnStretch(0, 1)
            server_grid.setColumnStretch(1, 1)
        right_layout.addWidget(server_box)

        # 资源监视面板（含显存进度条）
        right_layout.addWidget(self._monitor_panel)
        right_layout.addStretch()

        # 外层：QGridLayout 实现响应式（宽=横排，窄=竖排）
        self._container = QWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addWidget(self._container)
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(12)
        self._grid.addWidget(self._left, 0, 0)
        self._grid.addWidget(self._right, 0, 1)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)

        self._is_wide = True

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = self.width()
        target_wide = width >= _BREAKPOINT
        if target_wide != self._is_wide:
            self._is_wide = target_wide
            self._relayout()

    def _relayout(self) -> None:
        self._grid.removeWidget(self._left)
        self._grid.removeWidget(self._right)
        if self._is_wide:
            self._grid.addWidget(self._left, 0, 0)
            self._grid.addWidget(self._right, 0, 1)
            self._grid.setColumnStretch(0, 1)
            self._grid.setColumnStretch(1, 1)
        else:
            self._grid.addWidget(self._left, 0, 0)
            self._grid.addWidget(self._right, 1, 0)
            self._grid.setColumnStretch(0, 1)

    # -- 模型扫描 --

    def _scan_models(self) -> None:
        self._model_combo.clear()
        models_dir = Path(self._model_dir)
        if models_dir.is_dir():
            files = sorted(models_dir.glob("*.ninfer"))
            for f in files:
                self._model_combo.addItem(str(f), str(f))
            if not files:
                self._model_combo.addItem("（目录中无 .ninfer 文件）", "")
        else:
            self._model_combo.addItem("（模型目录不存在）", "")
        # 自动选中第一个
        if self._model_combo.count() > 0:
            self._model_combo.setCurrentIndex(0)

    def _auto_detect(self) -> None:
        """自动检测 exe 路径和模型文件，找到则自动填写。"""
        root = find_project_root()

        # 1. 自动检测 exe：build-ninja/apps/ninfer-serve.exe
        exe_candidate = root / "build-ninja" / "apps" / "ninfer-serve.exe"
        if exe_candidate.is_file() and not self._exe_edit.text().strip():
            self._exe_edit.setText(str(exe_candidate))
            self._exe_path = str(exe_candidate)
            self.exe_path_changed.emit(self._exe_path)

        # 2. 自动检测模型：launcher/models/*.ninfer
        models_dir = root / "launcher" / "models"
        if models_dir.is_dir():
            files = sorted(models_dir.glob("*.ninfer"))
            if files:
                # 用扫描到的文件重建 combo
                self._model_dir = str(models_dir)
                self._model_combo.clear()
                for f in files:
                    self._model_combo.addItem(str(f), str(f))
                self._model_combo.setCurrentIndex(0)
                data = self._model_combo.currentData()
                if data:
                    self.model_changed.emit(data)

    def _on_model_selected(self, text: str) -> None:
        data = self._model_combo.currentData()
        if data:
            self.model_changed.emit(data)

    def _on_browse_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择模型文件", self._model_dir, "NInfer 模型 (*.ninfer)"
        )
        if path:
            idx = self._model_combo.findText(path)
            if idx >= 0:
                self._model_combo.setCurrentIndex(idx)
            else:
                self._model_combo.addItem(path, path)
                self._model_combo.setCurrentIndex(self._model_combo.count() - 1)
            self.model_changed.emit(path)

    def _on_port_changed(self, value: int) -> None:
        self._port = value
        self.port_changed.emit(value)

    def _on_exe_changed(self, text: str) -> None:
        self._exe_path = text.strip()
        if self._exe_path:
            self.exe_path_changed.emit(self._exe_path)

    def _on_browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ninfer-serve.exe", "", "可执行文件 (*.exe)",
        )
        if path:
            self._exe_edit.setText(path)

    # -- 状态同步 --

    def apply_state(self, state: ServerState, port: int = 8080) -> None:
        """按状态机同步四个按钮的可用性。

        契约（2026-09 用户反馈后收紧）：
        - 「打开 WebUI」只在 RUNNING 可用——服务没就绪时 WebUI 必然不可用；
        - 「显示命令」在 STOPPED / STARTING / RUNNING 都可用，仅 STOPPING 禁用
          （停止过程中命令行已无意义，且此时 exe/参数都不该再被查看）；
        - 「启动」仅 STOPPED 可用；「停止」仅 STARTING / RUNNING 可用。
        """
        if state is ServerState.STOPPED:
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)
            self._btn_webui.setEnabled(False)
            self._btn_cmd.setEnabled(True)
        elif state is ServerState.STARTING:
            self._btn_start.setEnabled(False)
            self._btn_stop.setEnabled(True)
            self._btn_webui.setEnabled(False)
            self._btn_cmd.setEnabled(True)
        elif state is ServerState.RUNNING:
            self._btn_start.setEnabled(False)
            self._btn_stop.setEnabled(True)
            self._btn_webui.setEnabled(True)
            self._btn_cmd.setEnabled(True)
        else:  # STOPPING
            self._btn_start.setEnabled(False)
            self._btn_stop.setEnabled(False)
            self._btn_webui.setEnabled(False)
            self._btn_cmd.setEnabled(False)

    # -- 取值接口 --

    def get_model_path(self) -> str:
        return self._model_combo.currentData() or ""

    def get_port(self) -> int:
        return self._port

    def set_port(self, port: int) -> None:
        self._port = port
        self._port_spin.setValue(port)

    def get_exe_path(self) -> str:
        return self._exe_path

    def set_exe_path(self, path: str) -> None:
        self._exe_path = path
        self._exe_edit.setText(path or "")

    def webui_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def get_preset_group(self) -> _PresetGroup:
        return self._preset_group

    def get_theme_combo(self) -> QComboBox:
        return self._theme_combo

    def get_gpu_power_switch(self) -> GpuPowerSwitch:
        return self._gpu_power_switch

    def shutdown(self) -> None:
        self._monitor_panel.stop()
        self._monitor.shutdown()
