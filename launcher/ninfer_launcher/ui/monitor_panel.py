"""资源监视面板：显存 / SM 频率 / 温度 / 功耗 / 系统内存 / CPU 的周期刷新读数。

单卡精简版（AGENTS.md：RTX 4090 单卡场景）：相比 llama.cpp 启动器的完整版，
这里不做多 GPU 下拉框（单卡场景下拉框永远只有一项，是死控件），固定显示 GPU 0。
其余能力对齐：NVML 优先、nvidia-smi 回落时的「刷新已降频」常亮标注、数值与
「不可用」的严格区分。

分层约定：本面板不采集任何数据，只持有一个 core.monitor.MonitorService 实例、一个
QTimer，把 poll() 的结果转成界面文案。测试通过构造参数注入假 MonitorService（或
monkeypatch 其 poll）即可。

隐性契约：

1. 刷新周期跟着 core/monitor.py 判定的来源走，本面板不自己猜。每轮 poll 结束后按
   SystemSnapshot.source.refresh_interval_ms 重设 QTimer.setInterval：从 NVML 回落到
   nvidia-smi 那一刻起，下一次 tick 就用 3 秒周期，不需要使用者重启程序。
2. 「降频」标注只要 source.degraded 为真就一直挂着，不是「只在刚发生的那一刻闪一下」。
3. 「不可用」文案与「数值是 0」必须能区分：format_optional_* 系列收到 None 才输出
   「不可用」，收到 0 就如实显示 0——绝不用 value or UNAVAILABLE_TEXT 这种把 0 也判成
   假值的写法。
4. GPU 整体不可用（gpu_available 为假）时，各读数行显示「不可用」，但面板骨架保留，
   不是清空整个面板——使用者仍要能看出「这个面板在、只是没数据」。
5. 本面板自己不 import pynvml / psutil、不调 subprocess：全部真实系统交互在
   core/monitor.py 里，本面板只持有构造期传入的 MonitorService。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ..core.monitor import (
    UNAVAILABLE_TEXT,
    MonitorService,
    SystemSnapshot,
)

__all__ = [
    "format_optional_number",
    "format_bytes_pair",
    "format_percent",
    "format_temperature",
    "format_power_pair",
    "format_clock",
    "MonitorPanel",
]


# ---------------------------------------------------------------------------
# 纯逻辑：数值 -> 中文文案（可脱离真实 Qt 环境测试）
# ---------------------------------------------------------------------------


def format_optional_number(value, unit="", decimals=0):
    """把一个可能为 None 的数值格式化成中文文案。

    value is None 才输出「不可用」，0 会原样格式化成 "0" + unit。

    :param value: 待格式化的数值；None 表示读不到
    :param unit: 数值后缀的单位文字（如 " MHz"、"°C"）
    :param decimals: 小数位数
    :return: 中文文案
    """
    if value is None:
        return UNAVAILABLE_TEXT
    return f"{value:.{decimals}f}{unit}"


def format_bytes_pair(used, total, percent):
    """把「已用/总量/百分比」格式化成一行文案。任一项为 None 时该子项显示「不可用」。"""
    used_text = format_optional_number(_bytes_to_mib(used), " MiB")
    total_text = format_optional_number(_bytes_to_mib(total), " MiB")
    percent_text = format_optional_number(percent, "%", decimals=1)
    return f"{used_text} / {total_text}（{percent_text}）"


def format_percent(value):
    """把百分比数值格式化成带 % 的中文文案；None 时「不可用」。"""
    return format_optional_number(value, "%", decimals=1)


def format_temperature(value):
    """把摄氏度数值格式化；None 时「不可用」。"""
    return format_optional_number(value, "°C")


def format_power_pair(draw, limit):
    """把「当前功耗/上限功耗」格式化成一行文案；任一项缺失只影响对应半句。"""
    draw_text = format_optional_number(draw, " W", decimals=1)
    limit_text = format_optional_number(limit, " W", decimals=1)
    return f"{draw_text} / {limit_text}"


def format_clock(value):
    """把频率数值（MHz）格式化；None 时「不可用」。"""
    return format_optional_number(value, " MHz")


def _bytes_to_mib(value):
    """字节转 MiB；None 原样传递（不是 0）。"""
    return None if value is None else value / 1024.0 / 1024.0


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------


class MonitorPanel(QWidget):
    """资源监视面板（单卡）：GPU 读数 + CPU + 系统内存，QTimer 周期刷新。

    :param service: 已构造好的 MonitorService；不传则自己构造一个默认实例
        （真的会去尝试 NVML / nvidia-smi / psutil）。测试应显式注入假实例
    :param interval_ms: 初始刷新周期（毫秒），默认 1000。首轮 refresh 之后会按
        实际判定的来源自动调整
    :param auto_start: 构造后是否立即启动定时器；测试通常传 False，自己控制何时
        调 refresh，避免依赖真实墙上时钟
    :param parent: Qt 父对象
    """

    #: 每轮刷新完成后发出，携带本轮完整快照
    snapshotUpdated = Signal(object)

    def __init__(
        self,
        *,
        service=None,
        interval_ms=None,
        auto_start=True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service if service is not None else MonitorService()
        self._last_snapshot = None

        self._build_ui()

        self._timer = QTimer(self)
        if interval_ms is None:
            from ..core.monitor import NORMAL_INTERVAL_MS
            interval_ms = NORMAL_INTERVAL_MS
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.refresh)
        if auto_start:
            self._timer.start()

    def _build_ui(self) -> None:
        box = QGroupBox("资源监视")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)

        top = QVBoxLayout()
        # source_label 保留属性以兼容 API，但不显示在界面上
        self.source_label = QLabel()
        self.source_label.setProperty("secondary", "true")

        form = QFormLayout()
        form.setSpacing(4)
        self.gpu_label = QLabel(UNAVAILABLE_TEXT)
        self.vram_label = QLabel(UNAVAILABLE_TEXT)
        self.clock_label = QLabel(UNAVAILABLE_TEXT)
        self.temp_label = QLabel(UNAVAILABLE_TEXT)
        self.power_label = QLabel(UNAVAILABLE_TEXT)
        self.mem_label = QLabel(UNAVAILABLE_TEXT)
        self.cpu_label = QLabel(UNAVAILABLE_TEXT)

        form.addRow("GPU", self.gpu_label)
        form.addRow("SM 频率", self.clock_label)
        form.addRow("温度", self.temp_label)
        form.addRow("功耗", self.power_label)
        form.addRow("系统内存", self.mem_label)
        form.addRow("CPU", self.cpu_label)
        top.addLayout(form)

        # 显存进度条（底部，含百分比）——GPU 不可用时整行隐藏
        self._vram_row = QWidget()
        vram_h = QHBoxLayout(self._vram_row)
        vram_h.setContentsMargins(0, 0, 0, 0)
        vram_h.setSpacing(6)
        self._vram_bar = QProgressBar()
        self._vram_bar.setRange(0, 100)
        self._vram_bar.setValue(0)
        self._vram_bar.setTextVisible(False)
        self._vram_bar.setFixedHeight(10)
        self._vram_text = QLabel("显存：--")
        vram_h.addWidget(self._vram_bar, 1)
        vram_h.addWidget(self._vram_text)
        self._vram_row.setVisible(False)
        top.addWidget(self._vram_row)

        box.setLayout(top)

    # -- 周期刷新 -----------------------------------------------------------

    def refresh(self) -> None:
        """采一轮数据并刷新界面，同时按来源调整下一轮周期。"""
        snapshot = self._service.poll()
        self._last_snapshot = snapshot
        self._apply(snapshot)
        self._timer.setInterval(snapshot.source.refresh_interval_ms)
        self.snapshotUpdated.emit(snapshot)

    def _apply(self, snapshot) -> None:
        if not snapshot.gpu_available:
            self._set_all_unavailable()
            if snapshot.messages:
                self.source_label.setText(
                    f"来源：{snapshot.source.label}　{snapshot.messages[-1]}"
                )
            else:
                self.source_label.setText(f"来源：{snapshot.source.label}")
            return

        # 来源行 + 降频常亮标注
        source_text = f"来源：{snapshot.source.label}"
        if snapshot.source.degraded:
            source_text += "　（刷新已降频）"
        if snapshot.messages:
            source_text += "　" + snapshot.messages[-1]
        self.source_label.setText(source_text)

        gpu = snapshot.gpus[0]  # 单卡：固定 GPU 0
        self._vram_row.setVisible(True)
        self.gpu_label.setText(gpu.display_name)
        self.vram_label.setText(
            format_bytes_pair(gpu.mem_used_bytes, gpu.mem_total_bytes, gpu.mem_percent)
        )
        # 显存进度条
        if (
            gpu.mem_used_bytes is not None
            and gpu.mem_total_bytes
            and gpu.mem_total_bytes > 0
        ):
            used_gb = gpu.mem_used_bytes / 1024 / 1024 / 1024
            total_gb = gpu.mem_total_bytes / 1024 / 1024 / 1024
            pct = min(100, int(used_gb / total_gb * 100))
            self._vram_text.setText(f"显存：{used_gb:.1f}/{total_gb:.0f} GB ({pct}%)")
            self._vram_bar.setValue(pct)
            if pct < 60:
                color = "#6BCB77"
            elif pct < 85:
                color = "#FFD93D"
            else:
                color = "#FF6B6B"
            self._vram_bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}")
        else:
            self._vram_text.setText("显存：不可用")
            self._vram_bar.setValue(0)
        self.clock_label.setText(format_clock(gpu.sm_clock_mhz))
        self.temp_label.setText(format_temperature(gpu.temperature_c))
        self.power_label.setText(format_power_pair(gpu.power_draw_w, gpu.power_limit_w))
        self.mem_label.setText(
            format_bytes_pair(snapshot.mem_used_bytes, snapshot.mem_total_bytes, snapshot.mem_percent)
        )
        self.cpu_label.setText(format_percent(snapshot.cpu_percent))

    def _set_all_unavailable(self) -> None:
        for label in (
            self.gpu_label, self.vram_label, self.clock_label,
            self.temp_label, self.power_label, self.mem_label, self.cpu_label,
        ):
            label.setText(UNAVAILABLE_TEXT)
        self._vram_row.setVisible(False)

    # -- 生命周期 -----------------------------------------------------------

    def stop(self) -> None:
        """停止定时器。主窗口关闭时应调用，避免面板销毁后定时器仍持有引用触发。"""
        self._timer.stop()
