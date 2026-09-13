"""「性能模式」开关控件：把 NVIDIA 驱动的电源管理模式做成常用页里的一个复选框。

对应 ninfer 启动器的「性能模式」开关。真正读写驱动的是 ``core/gpu_power.py``，本模块只管三件事：
显示当前状态、把点击转成一次读写、失败时回弹并把原因交出去。

放在独立文件而不是 ``ui/widgets.py``：那里是与业务无关的通用控件库
（``Highlighted*`` 系列），本控件依赖 ``core/gpu_power``，放进去会让通用控件库
反过来依赖一个具体业务模块。

隐性契约（改动前务必读完）：

1. **写驱动期间必须屏蔽自己的 ``toggled`` 信号**（:attr:`_applying`）。写失败要把勾选
   状态回弹，而 ``setChecked`` 会再发一次 ``toggled`` —— 不屏蔽的话回弹本身又触发一次
   写入，一次失败会变成来回写驱动的循环。这里用一个布尔闸而不是
   ``blockSignals``：``blockSignals`` 会连带屏蔽 Qt 内部的状态同步信号，
   而我们只想忽略自己那一个处理器。
2. **勾选状态的事实源是显卡驱动，不是本控件**（``core/gpu_power.py`` 契约 9）。
   每次 :meth:`refresh` 都重新读驱动；使用者可能刚在 NVIDIA 控制面板里改过，
   本控件缓存一份状态必然漂移，而漂移后界面显示的是错的。
3. **不可用时既要禁用控件、也要把原因写进 tooltip**。只灰掉不说明原因，使用者会以为
   程序坏了；这个功能天然会在「没有 NVIDIA 卡」「驱动版本过旧」「权限不足」三种情况下
   不可用，每一种的处置方式都不同，必须让人看得到是哪一种。
4. **读写驱动是同步阻塞调用**（要建 DRS session）。本机实测一次往返在百毫秒内，
   按点击频率算可以接受，因此不起线程——如实标注在此，不假装已经异步化了。
   构造期只做一次 :func:`~core.gpu_power.describe_availability`（不建 session，更便宜），
   真正读档位交给调用方决定何时调 :meth:`refresh`。
5. **``reader`` / ``writer`` 可注入**，默认接 ``core/gpu_power`` 的真实实现。
   测试必须注入假实现——真跑一遍会改使用者机器上的显卡驱动设置。
6. **:meth:`GpuPowerSwitch.turn_off` 是程序代为关闭的入口，不是给使用者点击用的**。
   启动器启动时、ninfer-serve 退出或被停止时、关闭启动器时都要把性能模式关掉
   （需求：默认关闭、每次运行后关闭、不由使用者手动清理），这几处都不是「使用者点了
   开关」，所以不能走 :meth:`_on_toggled`——那个方法的入口是 ``toggled`` 信号，
   而信号只在使用者点击或 :meth:`_set_checked_silently` 主动设置时触发（后者又被
   排除在写入之外）。:meth:`turn_off` 直接调 writer，成功才同步勾选，失败照样把
   原因交出去，不吞掉。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QWidget

# ★ 刻意 import 模块而不是 ``from ... import read_power_mode`` 那几个函数：
# 后者会在本模块导入时就把函数对象绑到本模块的命名空间上，之后替换
# ``core.gpu_power`` 的模块属性（测试用 monkeypatch 拦住真实驱动读写，见
# ``tests/conftest.py`` 契约 4）就影响不到这里——拦不住的表现只是「测试变慢了」
# 和「测试真的改了本机驱动设置」，没有任何断言会因此变红。
from ..core import gpu_power
from ..core.gpu_power import DISABLE_MODE, ENABLE_MODE, PowerModeResult

__all__ = [
    "SWITCH_LABEL",
    "SWITCH_TOOLTIP",
    "GpuPowerSwitch",
]

#: 界面上这一行的标签文字（由 ``main_window`` 注入时使用）。
SWITCH_LABEL = "性能模式"

#: 悬浮提示。三件事必须都说：改的是什么、不是 ninfer-serve 的参数、关闭的语义
#: （``core/gpu_power.py`` 契约 8：关闭 = 写回出厂默认，不是恢复你原先的档位）。
SWITCH_TOOLTIP = (
    "开启后把 NVIDIA 驱动的「电源管理模式」设为「最高性能优先」，"
    "让 GPU 不在负载间隙降频。对应 NVIDIA 控制面板 → 管理 3D 设置 → 全局设置 → "
    "电源管理模式，是全局设置，对所有程序生效，重启后保持。\n"
    "★ 这不是 ninfer-serve 的命令行参数，不会写进预设文件，也不出现在「显示命令」里。\n"
    "★ 关闭时写回驱动出厂默认「正常（最佳功耗）」，不是恢复你原先手动设过的档位。\n"
    "★ 代价：GPU 空闲时也不降频，待机功耗与温度会上升。\n"
    "需要 NVIDIA 显卡与驱动；修改驱动设置通常需要管理员权限。"
)

#: 读当前档位的回调签名。
ModeReader = Callable[[], PowerModeResult]

#: 写档位的回调签名（参数为「是否开启性能模式」）。
ModeWriter = Callable[[bool], PowerModeResult]

#: 探测可用性的回调签名。
AvailabilityProbe = Callable[[], PowerModeResult]


class GpuPowerSwitch(QCheckBox):
    """NVIDIA 电源管理模式的开关。

    :param reader: 读当前档位，默认 :func:`~core.gpu_power.read_power_mode`
    :param writer: 写档位，默认 :func:`~core.gpu_power.apply_power_mode`
    :param availability_probe: 探测可用性，默认
        :func:`~core.gpu_power.describe_availability`
    :param parent: Qt 父对象

    ★ 测试必须注入这三个回调（契约 5）：默认实现会真的改本机显卡驱动设置。
    """

    #: 每次读或写结束后发出，携带 ``tuple[str, ...]``（一行或多行中文说明），
    #: 由 ``main_window`` 转给日志区。与 ``ui/control_panel.py`` 的
    #: :attr:`~ui.control_panel.ControlPanel.testResultReady` 同一种约定。
    messageReady = Signal(object)

    def __init__(
        self,
        *,
        reader: ModeReader | None = None,
        writer: ModeWriter | None = None,
        availability_probe: AvailabilityProbe | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # 没注入时用一层 lambda 去**调用时**查模块属性，而不是现在就把函数对象绑下来：
        # 绑下来的话 monkeypatch 模块属性对已构造的实例无效（见本文件顶部的 import 说明）。
        self._reader: ModeReader = reader or (lambda: gpu_power.read_power_mode())
        self._writer: ModeWriter = writer or (
            lambda enable: gpu_power.apply_power_mode(enable)
        )
        self._probe: AvailabilityProbe = availability_probe or (
            lambda: gpu_power.describe_availability()
        )
        self._applying = False
        self._available = False

        self.setText("")
        self.setToolTip(SWITCH_TOOLTIP)
        self.toggled.connect(self._on_toggled)

        # 契约 4：构造期只探可用性，不建 DRS session 去读档位。
        probe = self._probe()
        self._set_available(probe.ok, probe.message)

    # -- 状态 ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        """NVAPI 是否可用（不可用时控件被禁用）。"""
        return self._available

    def _set_available(self, available: bool, reason: str) -> None:
        self._available = available
        self.setEnabled(available)
        if available:
            self.setToolTip(SWITCH_TOOLTIP)
            return
        # 契约 3：禁用的同时把原因摆出来。
        self.setToolTip(f"当前不可用：{reason}\n\n{SWITCH_TOOLTIP}")

    def refresh(self) -> PowerModeResult:
        """重新读驱动并同步勾选状态（契约 2：驱动才是事实源）。

        :return: 本次读取结果；不可用时是构造期那次探测失败的同类结果
        """
        result = self._reader()
        self._set_available(result.ok, result.message)
        self._set_checked_silently(result.performance_enabled)
        return result

    def _set_checked_silently(self, checked: bool) -> None:
        """改勾选状态但不触发写驱动（契约 1）。"""
        previous = self._applying
        self._applying = True
        try:
            self.setChecked(checked)
        finally:
            self._applying = previous

    def turn_off(self) -> PowerModeResult:
        """程序主动关闭性能模式，不经使用者点击（启动器启动 / 服务退出或停止 /
        关闭启动器时调用）。

        走的是同一套写入闸门（契约 1），结果同样经 :attr:`messageReady` 发出——
        这几处触发点都不是用户点击开关本身，调用方（``main_window.py``）没有
        另外的地方能把「关闭失败」的原因交出去，必须由这里代为发出，否则一次
        NVAPI 失败会被无声吞掉。

        :return: 写入结果；``ok`` 为假时驱动当前档位维持不变，勾选状态也不改
            （契约 2：勾选状态只信驱动实际值，写失败就不该显示成「已关」）
        """
        self._applying = True
        try:
            result = self._writer(False)
        finally:
            self._applying = False
        if result.ok:
            self._set_checked_silently(False)
            self.messageReady.emit(result.format_lines())
        else:
            self.messageReady.emit((f"未能关闭性能模式：{result.message}",))
        return result

    # -- 点击 → 写驱动 -------------------------------------------------------

    def _on_toggled(self, checked: bool) -> None:
        if self._applying:
            # 契约 1：这一次变化是我们自己同步进来的，不是使用者点的。
            return
        # 契约 4：同步阻塞。写完立刻按结果决定勾选状态，不做乐观显示——
        # 乐观显示会在失败时留下一个「看起来已开启」的勾，比不显示更糟。
        self._applying = True
        try:
            result = self._writer(checked)
        finally:
            self._applying = False

        target = ENABLE_MODE if checked else DISABLE_MODE
        if result.ok:
            self.messageReady.emit(result.format_lines())
            return
        # 失败：回弹到「没有生效」的那一侧，并把原因交出去。
        self._set_checked_silently(not checked)
        self.messageReady.emit(
            (f"未能把电源管理模式设为{target.label}：{result.message}",)
        )
