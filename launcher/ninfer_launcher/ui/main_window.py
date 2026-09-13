"""主窗口：三标签页（控制/参数/日志）+ 进程生命周期 + 预设存取 + 显示命令。

本模块是唯一持有 ServerProcess / HealthPoller / ConfigStore 实例的地方。
各面板（control_panel / params_tab / log_panel）不知道进程管理与配置存取的实现细节，
跨面板编排全部集中在本模块。
"""

from __future__ import annotations

import os
import webbrowser
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QInputDialog,
    QMessageBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
)

from ..core.config import (
    ConfigStore,
    Settings,
    THEME_DARK,
    THEME_LIGHT,
    THEME_SYSTEM,
    check_preset_name,
    seed_builtin_presets,
    builtin_preset_names,
)
from ..core.health import HealthPoller
from ..core.ports import check_port
from ..core.process import (
    ExternalProcessStopper,
    LogLine,
    LogSource,
    ServerProcess,
    ServerState,
)
from ..core.service_status import observe_service_state
from ..core.monitor import MonitorService, make_vram_reader
from ..params.builder import build, build_command
from ..params.registry import default_values, validate_values
from ..params.spec import Bool3
from .control_panel import ControlPanel
from .gpu_power_switch import GpuPowerSwitch
from .log_panel import LogPanel
from .params_tab import ParamsTab
from .theme import ThemeManager

__all__ = ["MainWindow"]


def _param_value_to_config(value: Any) -> Any:
    """把参数值域类型归一成 JSON 可序列化的配置形式。

    ``ParamsTab.get_values()`` 返回的是原样值，其中 BOOL3 参数的值域类型是
    :class:`Bool3`（Enum）。所有把参数值落 JSON 的路径——「另存为 / 保存配置」的
    ``_build_preset_data``，以及关闭时 ``_save_params`` 持久化 ``settings.params``——
    都要先把 ``Bool3`` 落回字符串（``Bool3.to_config()``：``on``/``off``/``unset``），
    否则 ``json.dumps`` 抛 ``TypeError``，表现为静默失败：预设列表不刷新、关闭时
    settings.json 写不进去。其余类型（int/float/str/None）本就 JSON 原生，原样透传。
    读取侧由 ``Bool3.from_config()`` 对称还原（见 ``Bool3CheckBox.set_value`` / ``_emit_tristate``）。
    """
    if isinstance(value, Bool3):
        return value.to_config()
    return value


class MainWindow(QMainWindow):
    """ninfer-launcher 主窗口。"""

    def __init__(
        self,
        gpu_power_reader: "Callable[[], Any] | None" = None,
        gpu_power_writer: "Callable[[bool], Any] | None" = None,
        gpu_power_probe: "Callable[[], Any] | None" = None,
    ) -> None:
        super().__init__()
        self._last_preset_name: str | None = None
        self.setWindowTitle("NInfer Launcher")
        self.setMinimumSize(1024, 700)
        self.resize(1100, 750)

        # 配置
        self._store = ConfigStore.open_default()
        seeded = seed_builtin_presets(self._store.root)
        self._settings = self._store.load_settings()
        if self._settings.window_width > 100:
            self.resize(self._settings.window_width, self._settings.window_height)

        # 核心服务（monitor 实例同时供控制面板监视面板、进程层显存回落、外部服务停止
        # 的显存回落等待复用——同一读数来源，避免多开 NVML 句柄）
        monitor = MonitorService()
        self._vram_reader = make_vram_reader(monitor)
        self._process = ServerProcess(vram_reader=self._vram_reader)
        self._health = HealthPoller(abort_check=self._process.abort_reason)
        self._port = int(self._settings.params.get("port", 8080))
        self._model_dir = self._settings.model_dir or "E:\\ai\\ninfer-4090"

        # 预设快照（用于未保存改动判定）
        self._preset_snapshot: dict | None = None

        # 主题：单实例，深色/浅色/跟随系统三态，即时切换并持久化
        self._theme = ThemeManager(self._settings.theme)

        # 性能模式开关（NVIDIA 驱动电源管理模式）：读写回调由本模块注入——未注入时用真实
        # 驱动实现（测试里由 conftest 全局拦掉），开关实例交给 ControlPanel 摆进「通用设置」
        # 组（见 ui/control_panel.py）。本模块是唯一编排 turn_off 的地方。
        self._gpu_power_reader = gpu_power_reader
        self._gpu_power_writer = gpu_power_writer
        self._gpu_power_probe = gpu_power_probe

        # UI
        self._tabs = QTabWidget()
        self._gpu_switch = GpuPowerSwitch(
            reader=self._gpu_power_reader,
            writer=self._gpu_power_writer,
            availability_probe=self._gpu_power_probe,
        )
        self._control = ControlPanel(
            model_dir=self._model_dir,
            monitor_service=monitor,
            gpu_power_switch=self._gpu_switch,
        )
        self._params = ParamsTab()
        self._log = LogPanel()
        self._tabs.addTab(self._control, "控制")
        self._tabs.addTab(self._params, "参数")
        self._tabs.addTab(self._log, "日志")
        self.setCentralWidget(self._tabs)

        # 预设下拉初始化
        self._refresh_preset_list()

        # 连接信号
        self._process.state_changed.connect(self._on_state_changed)
        self._process.log_line.connect(self._on_log_line)
        self._process.exited.connect(self._on_exited)
        self._health.ready.connect(self._on_health_ready)
        self._health.aborted.connect(self._on_health_aborted)

        # 控制板信号
        self._control.start_requested.connect(self._on_start)
        self._control.stop_requested.connect(self._on_stop)
        self._control.open_webui_requested.connect(self._on_open_webui)
        self._control.show_command_requested.connect(self._on_show_command)
        self._control.model_changed.connect(self._on_model_changed)
        self._control.port_changed.connect(self._on_port_changed)
        self._control.exe_path_changed.connect(self._on_exe_path_changed)
        # 性能模式开关读/写驱动的结果（含失败原因）落日志区。
        self._gpu_switch.messageReady.connect(self._on_gpu_power_message)

        # 外部服务对账（识别 CLI / 外部启动、本 GUI 并不知情的服务）：自身进程处于停止态时
        # 每秒对账一次，真值顺序与 CLI status 完全一致（/health 探测 > OS 层进程存活 >
        # PID 登记表，见 core/service_status）。识别到外部服务后把四个控制按钮同步到对应
        # 状态，避免「后台服务在跑、GUI 按钮停在停止态点不动」的失同步。对账是纯只读
        # 观察者（不删登记表、不杀进程、不抢锁）；停止外部实例是用户点「停止」后的动作。
        self._external_state: ServerState | None = None  # 面板当前按外部服务显示的观测状态
        self._external_pid: int | None = None            # 外部实例 PID（None = 无登记表 / 进程已死）
        self._external_port: int | None = None           # 外部服务实际端口（登记表优先，否则当前设置）
        self._external_owner: str | None = None          # 登记表归属（cli / external）
        self._external_stop_in_progress = False          # 外部停止流程进行中（对账不得覆盖按钮）
        self._external_stopper: ExternalProcessStopper | None = None
        self._ext_timer = QTimer(self)
        self._ext_timer.setInterval(1000)
        self._ext_timer.timeout.connect(self._reconcile_external_service)
        self._ext_timer.start()

        # 预设组信号
        preset_group = self._control.get_preset_group()
        preset_group.presetSelected.connect(self._on_preset_selected)
        preset_group.saveRequested.connect(self._on_save_preset)
        preset_group.saveAsRequested.connect(self._on_save_as_preset)
        preset_group.deleteRequested.connect(self._on_delete_preset)
        preset_group.resetRequested.connect(self._on_reset_defaults)

        # 播种日志
        if seeded:
            seed_msg = ", ".join(seeded)
            self._log.append_line(LogLine(LogSource.LAUNCHER, f"已播种内置预设：{seed_msg}"))

        # 主题：同步 combo 选中项、应用初始样式、把高亮取色接进当前调色板
        self._sync_theme_combo()
        self._control.get_theme_combo().currentIndexChanged.connect(self._on_theme_changed)
        self._theme.apply(self)
        from .widgets import set_accent_provider
        set_accent_provider(lambda: self._theme.palette.accent)

        # 恢复上次参数
        self._restore_params()

        # 性能模式默认关闭：每次启动都把 NVIDIA 电源管理模式写回出厂默认——使用者上次
        # 忘关、或中途自己到控制面板开过，都不该带进下一次运行。结果（含失败原因）经
        # messageReady 落到日志区。
        self._gpu_switch.turn_off()

    # -- 信号槽：进程 / 健康 --

    def _on_state_changed(self, state) -> None:
        self._control.apply_state(state, self._port)
        if state is ServerState.STARTING:
            self._health.host = "127.0.0.1"
            self._health.port = self._port
            self._health.start()
        elif state is ServerState.STOPPED:
            self._health.stop()
        # 自身进程状态机一动（启动 / 运行 / 停止 / 回到停止），面板真值源就交还给进程层：
        # 清掉外部服务观测，避免陈旧的外部状态与进程状态「抢」按钮（本行 apply_state 已把
        # 面板切到进程状态）。回到停止态后，下一轮对账会重新识别仍存活的外部服务。
        if self._external_state is not None and not self._external_stop_in_progress:
            self._clear_external_observation()

    def _on_log_line(self, line) -> None:
        self._log.append_line(line)

    def _on_gpu_power_message(self, lines) -> None:
        """性能模式开关读/写驱动的结果落日志区（开关已把成功/失败原因说清）。"""
        for text in lines:
            self._log.append_line(LogLine(LogSource.LAUNCHER, text))

    def _on_exited(self, info) -> None:
        if info.unexpected:
            self._log.append_line(LogLine(LogSource.LAUNCHER, "进程异常退出！"))
        # 服务退出（被停止 / 崩溃 / 探通前退出）时关闭性能模式，无需使用者手动清理。
        self._gpu_switch.turn_off()

    def _on_health_ready(self) -> None:
        self._process.mark_ready()

    def _on_health_aborted(self, reason: str) -> None:
        if self._process.state is ServerState.STARTING:
            self._process.mark_ready()

    # -- 信号槽：控制板 --

    def _on_start(self) -> None:
        if self._process.state is not ServerState.STOPPED:
            return

        values = self._params.get_values()
        values["model"] = self._control.get_model_path()
        values["port"] = self._control.get_port()
        self._port = values["port"]

        errors = validate_values(values)
        model = values.get("model") or ""
        if not model:
            errors.append("未选择模型文件")
        elif not os.path.isfile(model):
            # 启动前就拦截：避免 serve 进程起来后才报 CreateFileW 找不到文件
            errors.append(f"模型文件不存在：{model}")
        port_check = check_port(self._port)
        if port_check.status.name == "IN_USE":
            errors.append(f"端口 {self._port} 已被占用")

        if errors:
            QMessageBox.warning(self, "启动前校验失败", "\n".join(errors))
            return

        try:
            args = build(values)
        except ValueError as exc:
            QMessageBox.critical(self, "参数错误", str(exc))
            return

        exe = self._resolve_exe()
        if not exe or not os.path.isfile(exe):
            QMessageBox.critical(self, "找不到服务器程序",
                f"未找到 ninfer-serve.exe\n当前路径：{exe}")
            return

        self._process.start(exe, args)
        self._preset_snapshot = self._params.get_values()

    def _on_stop(self) -> None:
        # 自身进程（GUI 拉起）在运行 / 启动中：走常规停止链（terminate → taskkill → 强杀升级）。
        if self._process.state is not ServerState.STOPPED:
            self._process.stop()
            return
        # 外部服务（CLI / 外部拉起、本 GUI 不知情）：按 PID 走 OS 层停止升级链。
        if (
            self._external_state in (ServerState.RUNNING, ServerState.STARTING)
            and not self._external_stop_in_progress
        ):
            self._stop_external()
            return
        # 两者皆停：无可停，静默返回。

    def _on_open_webui(self) -> None:
        # 外部服务在跑（本 GUI 未拉起进程）时：打开外部实例实际端口上的 WebUI；
        # 否则打开 GUI 自身服务端口。
        if self._external_state is ServerState.RUNNING and self._external_port:
            webbrowser.open(f"http://127.0.0.1:{self._external_port}")
            return
        webbrowser.open(self._control.webui_url())

    def _on_model_changed(self, path: str) -> None:
        pass

    def _on_port_changed(self, port: int) -> None:
        self._port = port

    def _on_exe_path_changed(self, path: str) -> None:
        self._settings.exe_path = path
        self._store.save_settings(self._settings)

    # -- 外部服务对账（识别 CLI / 外部启动、本 GUI 并不知情的服务）--

    def _clear_external_observation(self) -> None:
        """清掉外部服务观测（面板真值交还给进程状态机）。"""
        self._external_state = None
        self._external_pid = None
        self._external_port = None
        self._external_owner = None

    def _reconcile_external_service(self) -> None:
        """每秒对账一次：自身进程处于停止态时观测 OS / 服务实况并同步四个控制按钮。

        只在「自身进程停止且无进行中的外部停止流程」时对账：STARTING / RUNNING / STOPPING
        由状态机 + 健康轮询保持按钮正确，且此时端口被自身服务占用，外部对账无意义。本方法
        是纯只读观测（不删登记表、不杀进程、不抢锁）；观测结果经与进程层相同的 apply_state
        落到面板——面板显示与 ServerProcess 状态机解耦，因此可安全地把处于停止态状态机的
        面板推进到 RUNNING（ALLOWED_TRANSITIONS 禁止 STOPPED→RUNNING 迁移，故绝不改动
        self._process 本身）。
        """
        if self._external_stop_in_progress:
            return
        if self._process.state is not ServerState.STOPPED:
            # 自身进程非停止态：面板由进程状态机主导，清掉陈旧外部观测即可。
            if self._external_state is not None:
                self._clear_external_observation()
            return
        try:
            obs = observe_service_state(
                host="127.0.0.1",
                port=self._port,
                root=self._store.root,
            )
        except Exception:  # noqa: BLE001 —— 观测异常（网络 / IO）不打断 GUI 主流程
            return
        if obs.state is ServerState.STOPPED:
            # 无外部服务：若面板此前显示的是外部状态，则切回停止态矩阵（自身进程本就在停止态）。
            if self._external_state is not None:
                self._control.apply_state(ServerState.STOPPED, self._port)
            self._clear_external_observation()
            return
        # 外部服务在跑（STARTING / RUNNING）：把面板同步到对应状态。
        self._external_state = obs.state
        self._external_pid = obs.pid
        self._external_port = obs.port
        self._external_owner = obs.owner
        self._control.apply_state(obs.state, obs.port)

    def _stop_external(self) -> None:
        """停止外部服务（CLI / 外部拉起、本 GUI 无 QProcess 句柄的实例）。

        外部实例没有 QProcess 句柄，走 ServerProcess.stop() 无从谈起；这里用
        ExternalProcessStopper 复用与 CLI action_stop 一致的升级链（taskkill 杀进程树 →
        仍存活则 Win32 强杀 + OS 校验），进程确认死亡后走同源显存回落等待。stopper 挂到
        窗口下随窗口销毁。用户已点「停止」，是显式授权，故不再二次确认（与停止自身服务一致）。
        """
        if self._external_stop_in_progress or self._external_stopper is not None:
            return
        if self._external_state is None:
            return
        pid = self._external_pid
        if not pid:
            # 服务可达（/health 通）但无 PID 登记表：无法定位进程，提示用户手动处置。
            self._log.append_line(
                LogLine(
                    LogSource.LAUNCHER,
                    "外部服务在运行但无法定位其进程（无 PID 登记表），"
                    "请在任务管理器 → 详细信息中结束占用该端口的进程",
                )
            )
            return
        self._external_stop_in_progress = True
        owner = self._external_owner or "未知"
        self._log.append_line(
            LogLine(LogSource.LAUNCHER, f"正在停止外部服务（PID {pid}，归属 {owner}）…")
        )
        stopper = ExternalProcessStopper(pid, vram_reader=self._vram_reader, parent=self)
        stopper.message.connect(self._on_external_stop_message)
        stopper.finished.connect(self._on_external_stopped)
        self._external_stopper = stopper
        stopper.start()

    def _on_external_stop_message(self, text: str) -> None:
        self._log.append_line(LogLine(LogSource.LAUNCHER, text))

    def _on_external_stopped(self, ok: bool, message: str) -> None:
        self._log.append_line(LogLine(LogSource.LAUNCHER, message))
        stopper = self._external_stopper
        self._external_stopper = None
        self._external_stop_in_progress = False
        if stopper is not None:
            stopper.deleteLater()
        if ok:
            # 进程已确认死亡：清掉外部观测，并把面板切回停止态矩阵（自身进程本就在停止态）。
            self._clear_external_observation()
            self._control.apply_state(ServerState.STOPPED, self._port)
            # 服务已停：关闭性能模式（与自身服务退出时一致，覆盖「手动开着」的情形）。
            self._gpu_switch.turn_off()
        # ok=False（升级时限用尽仍存活）：不清观测——服务大概率仍在跑，下一轮对账会
        # 重新识别并保持按钮可操作，用户可重试停止或在任务管理器手动结束。

    # -- 显示命令 --

    def _on_show_command(self) -> None:
        """显示将要执行的完整命令行；任何未料错误都可见化，杜绝「点了没反应」。"""
        text = ""
        try:
            values = self._params.get_values()
            values["model"] = self._control.get_model_path()
            values["port"] = self._control.get_port()
            exe = self._resolve_exe()
            try:
                cmd = build_command(exe or "<exe>", values)
            except ValueError as exc:
                QMessageBox.critical(self, "参数错误", str(exc))
                return
            parts = []
            for c in cmd:
                parts.append(f'"{c}"' if " " in c else c)
            text = " ".join(parts)
        except Exception as exc:  # 兜底：槽函数里任何未料异常都要给用户可见反馈
            QMessageBox.critical(self, "显示命令失败", f"生成命令行时出错：{exc}")
            return
        # PySide6 6.11+ 移除了 QWidget.exec()（只有 QDialog 有），QTextEdit 直接调 exec
        # 会抛 AttributeError——被外层 except 吞掉后表现为「点了没反应」。
        # 用 QDialog 承载只读文本框，exec_ 在 5.x/6.x 各版本都存在。
        dialog = QDialog(self)
        dialog.setWindowTitle("完整命令")
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(view)
        dialog.resize(700, 300)
        dialog.exec_()

    # -- 预设操作 --

    def _on_preset_selected(self, name: str, force: bool = False) -> None:
        # force=True：删除当前预设后由程序主动切到下一个可用预设，此时参数本就该被
        # 新预设覆盖，跳过「未保存改动」确认弹窗（否则刚删完又弹一次框，很怪）。
        if not force and self._has_unsaved_changes():
            reply = QMessageBox.question(
                self, "未保存的改动",
                "当前参数有未保存的改动，切换预设将丢弃这些改动。继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:  # 用 == 比较：PySide6 的 question 真实返回 int，is 比较永远不成立
                self._control.get_preset_group().select_preset_silently(self._last_preset_name)
                return

        preset = self._store.load_preset(name)
        if preset is None:
            QMessageBox.warning(self, "预设不存在", f"找不到预设：{name}")
            return

        raw_params = preset.get("params", {})
        params = {k: v for k, v in raw_params.items()
                  if k not in ("model", "port")}
        if params:
            self._params.set_values(params)

        model = preset.get("model") or raw_params.get("model")
        if model:
            self._sync_model_from_preset(model)
        port = preset.get("port") or raw_params.get("port")
        if port:
            self._control.set_port(int(port))
            self._port = int(port)

        self._last_preset_name = name
        self._settings.last_preset = name
        self._store.save_settings(self._settings)
        self._preset_snapshot = self._params.get_values()

    def _on_save_preset(self) -> None:
        name = self._control.get_preset_group().current_preset_name()
        if not name:
            QMessageBox.information(self, "提示", "请先选择一个预设，或点「另存为」新建。")
            return
        err = check_preset_name(name)
        if err:
            QMessageBox.critical(self, "保存预设失败", err)
            return
        data = self._build_preset_data(name)
        try:
            self._store.save_preset(name, data)
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.critical(
                self, "保存预设失败",
                f"写入预设文件失败：{exc}\n名称：{name}"
            )
            return
        self._log.append_line(LogLine(LogSource.LAUNCHER, f"已保存预设：{name}"))
        self._preset_snapshot = self._params.get_values()

    def _on_save_as_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "另存为", "预设名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        err = check_preset_name(name)
        if err:
            QMessageBox.critical(self, "另存为失败", err)
            return
        data = self._build_preset_data(name)
        try:
            self._store.save_preset(name, data)
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.critical(
                self, "另存为失败",
                f"写入预设文件失败：{exc}\n名称：{name}"
            )
            self._refresh_preset_list()
            return
        self._refresh_preset_list(current=name)
        self._log.append_line(LogLine(LogSource.LAUNCHER, f"已另存预设：{name}"))
        self._settings.last_preset = name
        self._store.save_settings(self._settings)
        self._preset_snapshot = self._params.get_values()

    def _on_delete_preset(self) -> None:
        group = self._control.get_preset_group()
        name = group.current_preset_name()
        if not name:
            QMessageBox.information(self, "提示", "请先选择要删除的预设。")
            return
        is_builtin = name in builtin_preset_names()
        hint = "（内置预设，删除后不会自动恢复）" if is_builtin else ""
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定删除预设「{name}」吗？{hint}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:  # 用 == 比较：PySide6 的 question 真实返回 int，is 比较永远不成立
            return
        self._store.delete_preset(name)
        # 内置预设若不记一笔，下次启动 seed_builtin_presets 会从 resources/presets
        # 把它补回来（「删了又复活」）。记入墓碑名单后删除才持久生效。
        if is_builtin:
            self._store.record_builtin_deletion(name)
        # 清掉指向已删预设的状态再刷新：刷新时 current 为空，下拉框落到第一个可用预设
        # （删光则置空），不会停在上一个被删的预设上。
        self._last_preset_name = None
        self._settings.last_preset = ""
        self._store.save_settings(self._settings)
        self._refresh_preset_list()
        # 若还有可用预设，把参数同步到新的选中项，避免「下拉框指着 B、参数还是已删的 A」。
        # force=True 跳过未保存改动弹窗（参数本就该被新预设覆盖）；删光了则重置脏检测基线。
        next_name = self._control.get_preset_group().current_preset_name()
        if next_name:
            self._on_preset_selected(next_name, force=True)
        else:
            self._preset_snapshot = self._params.get_values()
        tail = "，内置预设已不会再自动恢复" if is_builtin else ""
        self._log.append_line(LogLine(LogSource.LAUNCHER, f"已删除预设：{name}{tail}"))

    def _on_reset_defaults(self) -> None:
        self._params.reset_defaults()
        self._control.get_preset_group().select_preset_silently(None)
        self._last_preset_name = None
        self._preset_snapshot = self._params.get_values()
        self._log.append_line(LogLine(LogSource.LAUNCHER, "已重置全部参数为出厂默认值"))

    # -- 预设辅助 --

    def _refresh_preset_list(self, current: str | None = None) -> None:
        names = self._store.list_presets()
        if current is None:
            current = self._settings.last_preset or None
        self._control.get_preset_group().set_preset_names(names, current=current)

    def _build_preset_data(self, name: str) -> dict:
        params = {
            k: _param_value_to_config(v)
            for k, v in self._params.get_values().items()
            if k not in ("model", "port")
        }
        return {
            "name": name,
            "schema": 1,
            "model": self._control.get_model_path(),
            "port": self._control.get_port(),
            "params": params,
        }

    def _has_unsaved_changes(self) -> bool:
        if self._preset_snapshot is None:
            return False
        current = self._params.get_values()
        for key, val in self._preset_snapshot.items():
            if current.get(key) != val:
                return True
        return False

    def _sync_model_from_preset(self, model_path: str) -> None:
        combo = self._control._model_combo
        idx = combo.findText(model_path)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.addItem(model_path, model_path)
            combo.setCurrentIndex(combo.count() - 1)

    # -- 恢复 / 辅助 --

    def _restore_params(self) -> None:
        params = {k: v for k, v in self._settings.params.items()
                  if k not in ("model", "port")}
        if params:
            self._params.set_values(params)
        if self._settings.last_preset:
            preset = self._store.load_preset(self._settings.last_preset)
            if preset:
                raw = preset.get("params", {})
                preset_params = {k: v for k, v in raw.items()
                                 if k not in ("model", "port")}
                if preset_params:
                    self._params.set_values(preset_params)
                model = preset.get("model") or raw.get("model")
                if model:
                    self._sync_model_from_preset(model)
                port = preset.get("port") or raw.get("port")
                if port:
                    self._control.set_port(int(port))
                    self._port = int(port)
                self._last_preset_name = self._settings.last_preset
                self._preset_snapshot = self._params.get_values()

    def _resolve_exe(self) -> str:
        """解析 ninfer-serve.exe 的完整路径。

        取值优先级（第一个命中即返回）：
        1. 控制面板当前显示的 exe 路径——界面里使用者看到的唯一事实源，
           ``ControlPanel._auto_detect`` 在构造期就已把它填好；
        2. settings.json 里持久化的 ``exe_path``（手动设定并保存过的）；
        3. 自动探测：复用 ``core.config.find_project_root`` 找到含 ``build-ninja/`` 的项目根，
           取其下 ``apps/ninfer-serve.exe``（冻结/开发两种模式都可靠）。

        关键：不能只读 ``settings.exe_path``。``_auto_detect`` 在 ControlPanel
        构造期就 emit 了 ``exe_path_changed``，而 main_window 此刻尚未连接该
        信号，自动探测到的路径落不进 settings——只读 settings 就会漏掉界面里
        明明显示着的 exe 路径，回退到 ``__file__`` 推导（冻结下不可靠）而误报
        「找不到服务程序」。
        """
        # 1. 控制面板的实时值（界面显示的就是它）
        live = self._control.get_exe_path().strip()
        if live and os.path.isfile(live):
            return live
        # 2. 持久化配置
        if self._settings.exe_path and os.path.isfile(self._settings.exe_path):
            return self._settings.exe_path
        # 3. 自动探测（复用控制面板的定位逻辑，冻结/开发均可靠）
        from ..core.config import find_project_root
        cand = find_project_root() / "build-ninja" / "apps" / "ninfer-serve.exe"
        if cand.is_file():
            return str(cand)
        # 全部落空：返回 live（使用者手填但不存在时给真实报错），否则返回探测路径
        return live or str(cand)

    def _save_params(self) -> None:
        # get_values() 含 BOOL3 的 Bool3 实例，落 JSON 前统一归一成字符串，
        # 否则 save_settings 的 json.dumps 抛 TypeError（关闭时 settings.json 写不进去）。
        self._settings.params = {
            k: _param_value_to_config(v) for k, v in self._params.get_values().items()
        }
        self._store.save_settings(self._settings)

    # -- 主题 --

    _THEME_VALUES = {0: THEME_DARK, 1: THEME_LIGHT, 2: THEME_SYSTEM}

    def _sync_theme_combo(self) -> None:
        """把主题 combo 的选中项同步到当前设置（阻塞信号，避免触发切换回调）。"""
        index = {THEME_DARK: 0, THEME_LIGHT: 1, THEME_SYSTEM: 2}.get(
            self._settings.theme, 0
        )
        combo = self._control.get_theme_combo()
        combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _on_theme_changed(self, index: int) -> None:
        """主题切换：即时应用样式 + 持久化 + 高亮取色跟随调色板。"""
        value = self._THEME_VALUES.get(index, THEME_DARK)
        applied = self._theme.set_theme(value)
        self._settings.theme = applied
        self._store.save_settings(self._settings)
        self._theme.apply(self)
        from .widgets import set_accent_provider
        set_accent_provider(lambda: self._theme.palette.accent)
        self._params._init_highlights()

    def closeEvent(self, event) -> None:
        # 关闭判定取「状态机 OR OS 层进程存活」：Windows 上 CUDA 进程（ninfer-serve）
        # 可能在驱动态长期拒绝终止，状态机 / Qt 缓存也可能与 OS 实况脱节——只要进程
        # 实际还活着，就必须走完整停止流程（terminate→taskkill→强杀升级+OS 校验），
        # 绝不让启动器退出时把 ninfer-serve 残留下来。
        if self._process.state.active or self._process.is_alive():
            reply = QMessageBox.question(
                self, "确认退出",
                "服务器正在运行，退出将停止它。确定吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:  # 用 == 比较：PySide6 的 question 真实返回 int，is 比较永远不成立
                event.ignore()
                return
            self._process.stop_and_wait()

        geo = self.geometry()
        self._settings.window_width = geo.width()
        self._settings.window_height = geo.height()
        self._save_params()
        # 关闭启动器时也关闭性能模式（服务器若刚经 stop_and_wait 退出，_on_exited 已关过，
        # 这里再关一次是安全的空写，覆盖「服务器本就已停、性能模式却被手动开着」的情形）。
        self._gpu_switch.turn_off()
        self._control.shutdown()
        event.accept()
