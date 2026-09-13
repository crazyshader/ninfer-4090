"""显存预检面板：只渲染 PreflightVerdict，不做任何估算 / 枚举。

分层约定与 ui/monitor_panel.py 同构（对齐其「只渲染 SystemSnapshot」的纪律）：
本面板不碰 NVML / psutil、不 import pynvml——数据全部由调用方（ui/main_window.py
的预检编排）取好后经 :meth:`PreflightPanel.apply_verdict` 灌进来。面板内部唯一
用到的逻辑是 core/vram_preflight.processes_to_close（纯函数：从 verdict 的候选
清单算出「应退出的进程集合」用于高亮），以及 format_vram_bytes（纯格式化）。

三态渲染：
- 绿「显存充足」：一行摘要（需求 / 空余）；
- 红「显存不足」：摘要 + 进程清单（名称 + 占用，降序）+ 高亮「退出这些即可满足」
  的进程集合；退光仍不够时附配置调整提示行；
- 灰「无法预检」：读数不可用——不阻断启动（风险自负），面板保留骨架。

「刷新」按钮只发 refresh_requested 信号；真正重算由主窗口完成（把新 verdict 再
灌回来）。这样测试只需构造假 verdict 就能钉死全部渲染分支，无需真实 GPU。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.vram_estimate import (
    MIB,
    SAFETY_MAX_BYTES,
    SAFETY_MIN_BYTES,
    clamp_safety_bytes,
)
from ..core.vram_preflight import (
    PreflightStatus,
    PreflightVerdict,
    format_vram_bytes,
    processes_to_close,
)

__all__ = ["PreflightPanel"]

_STATUS_TEXT = {
    PreflightStatus.OK: "显存充足",
    PreflightStatus.INSUFFICIENT: "显存不足",
    PreflightStatus.UNAVAILABLE: "无法预检",
    PreflightStatus.RUNNING: "服务运行中",
}

_STATUS_COLOR = {
    PreflightStatus.OK: "#6BCB77",
    PreflightStatus.INSUFFICIENT: "#FF6B6B",
    PreflightStatus.UNAVAILABLE: "#9E9E9E",
    PreflightStatus.RUNNING: "#6BCB77",
}


class PreflightPanel(QWidget):
    """显存预检面板（单卡）。

    构造后初始态为灰「等待预检」，等主窗口把第一个 verdict 灌进来才变绿 / 变红。
    本面板零系统接触面：不持有 MonitorService、不 import pynvml / psutil、
    不起定时器——周期重算的节奏由 ui/main_window.py 的 QTimer 决定。
    """

    #: 「刷新」按钮被点击：请求主窗口立即重算一次预检（不必等定时器）。
    refresh_requested = Signal()

    #: 安全垫被用户改动：携带已夹紧到 [SAFETY_MIN_BYTES, SAFETY_MAX_BYTES] 的字节数。
    #: 主窗口据此落 settings.json 并立即重算一次预检。
    safety_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        box = QGroupBox("显存预检")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel("等待预检…")
        self.status_label.setProperty("preflight", "pending")
        self.status_label.setStyleSheet("font-weight: bold;")
        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.setMinimumHeight(24)
        self._btn_refresh.setToolTip("立即重算一次显存预检")
        self._btn_refresh.clicked.connect(self.refresh_requested.emit)
        top.addWidget(self.status_label)
        top.addStretch()
        top.addWidget(self._btn_refresh)

        self.summary_label = QLabel("—")
        self.summary_label.setProperty("secondary", "true")
        self.summary_label.setWordWrap(True)

        #: 进程清单：仅在「显存不足」时显示；项 = 「名称（PID） — 占用」。
        self.process_list = QListWidget()
        self.process_list.setFixedHeight(110)
        self.process_list.setVisible(False)

        self.note_label = QLabel()
        self.note_label.setProperty("secondary", "true")
        self.note_label.setWordWrap(True)
        self.note_label.setVisible(False)

        # 安全垫编辑行：以 MiB 为单位的 SpinBox，范围 [SAFETY_MIN_BYTES, SAFETY_MAX_BYTES]。
        # 用户上调 → 需求变大、更保守；下调 → 更宽松。值变化经 safety_changed 发出（字节）。
        safety_row = QHBoxLayout()
        safety_row.setContentsMargins(0, 0, 0, 0)
        safety_caption = QLabel("安全垫")
        safety_caption.setProperty("secondary", "true")
        safety_caption.setToolTip(
            "显存预检在硬需求（权重 + 运行时 + KV）之上额外预留的余量。\n"
            "调大更保守（更容易判不足），调小更激进。范围 200 MiB–3 GiB。"
        )
        self.safety_spin = QSpinBox()
        self.safety_spin.setRange(SAFETY_MIN_BYTES // MIB, SAFETY_MAX_BYTES // MIB)
        self.safety_spin.setSingleStep(128)
        self.safety_spin.setSuffix(" MiB")
        self.safety_spin.setValue(SAFETY_MIN_BYTES // MIB)
        self.safety_spin.setToolTip(safety_caption.toolTip())
        self.safety_spin.valueChanged.connect(self._on_safety_spin_changed)
        safety_row.addWidget(safety_caption)
        safety_row.addStretch()
        safety_row.addWidget(self.safety_spin)

        layout = QVBoxLayout(box)
        layout.setSpacing(4)
        layout.addLayout(top)
        layout.addWidget(self.summary_label)
        layout.addLayout(safety_row)
        layout.addWidget(self.process_list)
        layout.addWidget(self.note_label)

    # -- 安全垫 -------------------------------------------------------------

    def set_safety_bytes(self, num_bytes: int) -> None:
        """把安全垫字节数设进 SpinBox（供主窗口从 settings 初始化）。

        入值先夹紧到合法范围再换算成 MiB；设值期间屏蔽 valueChanged，避免初始化
        回填触发一次多余的 safety_changed（进而多算一轮预检 / 多写一次盘）。
        """
        mib = clamp_safety_bytes(num_bytes) // MIB
        self.safety_spin.blockSignals(True)
        self.safety_spin.setValue(mib)
        self.safety_spin.blockSignals(False)

    def get_safety_bytes(self) -> int:
        """当前安全垫字节数（SpinBox 的 MiB 值换算 + 夹紧）。"""
        return clamp_safety_bytes(self.safety_spin.value() * MIB)

    def _on_safety_spin_changed(self, mib: int) -> None:
        self.safety_changed.emit(clamp_safety_bytes(mib * MIB))

    # -- 渲染 ---------------------------------------------------------------

    def apply_verdict(self, verdict: PreflightVerdict) -> None:
        """把一次预检结论渲染到面板。纯渲染，不读系统。"""
        self.status_label.setText(_STATUS_TEXT[verdict.status])
        self.status_label.setProperty("preflight", verdict.status.value)
        self.status_label.setStyleSheet(
            f"color: {_STATUS_COLOR[verdict.status]}; font-weight: bold;"
        )

        requirement = verdict.requirement
        if verdict.status is PreflightStatus.OK:
            self.summary_label.setText(
                f"需求 {format_vram_bytes(requirement.total_bytes)} · "
                f"空余 {format_vram_bytes(verdict.free_bytes)}"
            )
            self._clear_list_and_note()
        elif verdict.status is PreflightStatus.UNAVAILABLE:
            self.summary_label.setText("无法读取显存占用：未做预检，启动风险自负")
            self._clear_list_and_note()
        elif verdict.status is PreflightStatus.RUNNING:
            # 服务运行中：不做「冷启动能否装下」的减法，只显示当前占用现状。
            if verdict.free_bytes is not None and verdict.total_bytes is not None:
                used = verdict.total_bytes - verdict.free_bytes
                self.summary_label.setText(
                    f"服务运行中 · 已用 {format_vram_bytes(used)} / "
                    f"{format_vram_bytes(verdict.total_bytes)}"
                )
            else:
                self.summary_label.setText("服务运行中：预检仅在启动前评估")
            self._clear_list_and_note()
        else:  # INSUFFICIENT
            self.summary_label.setText(
                f"需求 {format_vram_bytes(requirement.total_bytes)} · "
                f"空余 {format_vram_bytes(verdict.free_bytes)} · "
                f"缺口 {format_vram_bytes(verdict.shortfall_bytes)}"
            )
            self._render_process_list(verdict)
            if verdict.messages:
                self.note_label.setText(verdict.messages[-1])
                self.note_label.setVisible(True)
            else:
                self.note_label.setVisible(False)

    def _clear_list_and_note(self) -> None:
        self.process_list.clear()
        self.process_list.setVisible(False)
        self.note_label.setVisible(False)

    def _render_process_list(self, verdict: PreflightVerdict) -> None:
        """渲染可退出进程清单；高亮「退出这些即可满足缺口」的最小进程集合。"""
        self.process_list.clear()
        close_set, _ = processes_to_close(verdict.candidates, verdict.shortfall_bytes)
        close_pids = {p.pid for p in close_set}
        for process in verdict.candidates:
            item = QListWidgetItem(
                f"{process.name}（PID {process.pid}） — {format_vram_bytes(process.used_bytes)}"
            )
            if process.pid in close_pids:
                item.setForeground(QBrush(QColor("#FFD93D")))
            self.process_list.addItem(item)
        self.process_list.setVisible(bool(verdict.candidates))
