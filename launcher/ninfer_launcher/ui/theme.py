"""深色 / 浅色两套 Qt 样式表，以及「跟随系统」判定与即时切换。

本模块按 core/ports.py / params 层的同一套分法切成两段，边界刻意划清：

1. 纯逻辑（resolve_theme / palette_for / stylesheet_for / interpret_apps_use_light_theme）：
   输入一个主题设置或一个已经读出来的注册表值，输出具体主题 / 调色板 / QSS 文本。
   不碰注册表、无状态，是测试的主战场
2. 与真实系统打交道（read_apps_use_light_theme）：唯一真正读 Windows 注册表的函数，
   通过 reader 参数整段可注入替换，因此测试不需要真实注册表

隐性契约：

1. 主题取值只认 core/config.py 的 THEME_DARK / THEME_LIGHT / THEME_SYSTEM 三个常量，
   不在本模块另写 "dark" / "light" / "system" 字符串字面量。
2. AppsUseLightTheme 是 DWORD，0 = 深色、1 = 浅色。读注册表失败要有回落，不得抛异常：
   非 Windows 环境（winreg 模块不存在）、键不存在、值类型不对，一律回落到深色，
   与「默认深色」用同一个回落方向。
3. 调色板不是「配色建议」，是「值≠默认值」高亮的唯一取色来源。ThemePalette 的
   accent / accent_text 字段就是给 ui/widgets.py 的高亮用的，widgets 不应另起一份
   颜色常量，否则深浅色切换时高亮会跟不上。
4. 样式表按主题整份重建，不做增量修改。stylesheet_for 每次产出完整 QSS 字符串，
   直接喂 QApplication.setStyleSheet() 即可整体替换、立即生效，不需要重启。
5. ThemeManager.apply 在样式表内容没变时必须跳过 setStyleSheet——每次 setStyleSheet
   都会让进程内全部存活控件重算样式，代价与控件总数成正比。
6. 本模块不 import core.config 的 Settings / ConfigStore，只 import 它的三个主题常量。
   调用方（main_window）负责把 ThemeManager 的当前值同步进 settings.json 再落盘。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..core.config import (
    THEME_CHOICES,
    THEME_DARK,
    THEME_LIGHT,
    THEME_SYSTEM,
)

__all__ = [
    "ThemePalette",
    "DARK_PALETTE",
    "LIGHT_PALETTE",
    "REGISTRY_KEY_PATH",
    "REGISTRY_VALUE_NAME",
    "RegistryReader",
    "interpret_apps_use_light_theme",
    "read_apps_use_light_theme",
    "detect_system_theme",
    "resolve_theme",
    "palette_for",
    "stylesheet_for",
    "ThemeManager",
]

#: 「跟随系统」读取的注册表键路径与值名。
REGISTRY_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
REGISTRY_VALUE_NAME = "AppsUseLightTheme"

#: 读注册表的可注入函数签名。
RegistryReader = Callable[[], int]


# ---------------------------------------------------------------------------
# 调色板
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThemePalette:
    """一套主题的配色取值。

    字段只收「界面上真正会用到的语义色」，不收控件级细节。细节由 stylesheet_for
    内部按语义色派生，派生逻辑集中在一处，换配色只改这一份 dataclass 就能带动整份 QSS。

    :param name: 主题名，供日志与调试辨认
    :param window: 窗口 / 顶层容器背景色
    :param surface: 卡片、输入框、面板等「次层」背景色
    :param surface_alt: 表头、分组标题等更浅一级的表面色
    :param text: 主文字色
    :param text_secondary: 次要文字色（tooltip、说明文字、禁用态标签）
    :param border: 控件边框色
    :param accent: 强调色。「值≠默认值」高亮的唯一取色来源，也用于焦点边框
    :param accent_text: 落在 accent 底色上的文字颜色
    :param disabled: 禁用态控件的文字/图标色
    :param error: 错误/警告文案与边框色
    """

    name: str
    window: str
    surface: str
    surface_alt: str
    text: str
    text_secondary: str
    border: str
    accent: str
    accent_text: str
    disabled: str
    error: str


#: 深色主题（默认值）。
DARK_PALETTE = ThemePalette(
    name=THEME_DARK,
    window="#1e1f22",
    surface="#2b2d31",
    surface_alt="#232428",
    text="#e3e5e8",
    text_secondary="#9a9ca0",
    border="#3f4147",
    accent="#5b8cff",
    accent_text="#ffffff",
    disabled="#6a6d73",
    error="#f2555a",
)

#: 浅色主题。
LIGHT_PALETTE = ThemePalette(
    name=THEME_LIGHT,
    window="#f5f5f7",
    surface="#ffffff",
    surface_alt="#ececee",
    text="#1c1c1e",
    text_secondary="#6b6d70",
    border="#d4d4d8",
    accent="#2f6fed",
    accent_text="#ffffff",
    disabled="#a8a9ac",
    error="#c72b30",
)

#: 主题名 → 调色板。palette_for 的唯一事实源，新增主题只改这一处。
_PALETTES: dict[str, ThemePalette] = {
    THEME_DARK: DARK_PALETTE,
    THEME_LIGHT: LIGHT_PALETTE,
}


# ---------------------------------------------------------------------------
# 纯逻辑：跟随系统的判定方向
# ---------------------------------------------------------------------------


def interpret_apps_use_light_theme(value: object) -> str:
    """把 AppsUseLightTheme 的原始 DWORD 值换算成具体主题。

    :param value: 读到的原始值。0 深色、1 浅色；其余（None、其他数字、非数字）一律深色
    :return: THEME_DARK 或 THEME_LIGHT
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，必须先拦住：True == 1 会被误判成浅色
        return THEME_DARK
    if isinstance(value, int) and value == 1:
        return THEME_LIGHT
    return THEME_DARK


def read_apps_use_light_theme(reader: RegistryReader | None = None) -> int | None:
    """读一次 HKCU/.../Personalize/AppsUseLightTheme 的原始值。

    唯一真正碰 Windows 注册表的函数。reader 用于测试注入；不传时用 winreg 真读一次，
    读不到（非 Windows、键不存在、值类型不对、权限问题等）统一返回 None，不抛异常。

    :param reader: 可注入的读取函数，允许抛任意异常
    :return: 原始值；读取失败时 None
    """
    if reader is not None:
        try:
            return reader()
        except Exception:  # noqa: BLE001 - 读取失败统一回落
            return None

    try:
        import winreg  # 仅 Windows 有此模块，非 Windows 环境 import 即失败
    except ImportError:
        return None

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY_PATH) as key:
            value, _kind = winreg.QueryValueEx(key, REGISTRY_VALUE_NAME)
            return value
    except OSError:
        # 键或值不存在（旧版 Windows、被策略移除等）
        return None


def detect_system_theme(reader: RegistryReader | None = None) -> str:
    """判定系统当前是深色还是浅色（「跟随系统」）。

    :param reader: 转发给 read_apps_use_light_theme 的可注入读取函数
    :return: THEME_DARK 或 THEME_LIGHT
    """
    return interpret_apps_use_light_theme(read_apps_use_light_theme(reader))


def resolve_theme(theme: str, *, reader: RegistryReader | None = None) -> str:
    """把一个主题设置换算成「最终要用哪套调色板/样式表」的具体主题。

    THEME_SYSTEM 换成 detect_system_theme 的结论；已经是 THEME_DARK / THEME_LIGHT
    原样返回；无法识别的值回落深色（与「默认深色」同一个方向）。

    :param theme: 主题设置，通常来自 Settings.theme
    :param reader: 转发给 detect_system_theme 的可注入读取函数
    :return: THEME_DARK 或 THEME_LIGHT
    """
    if theme == THEME_SYSTEM:
        return detect_system_theme(reader)
    if theme in (THEME_DARK, THEME_LIGHT):
        return theme
    return THEME_DARK


def palette_for(theme: str) -> ThemePalette:
    """取一个**具体**主题（非 THEME_SYSTEM）对应的调色板。

    调用方若持有的是可能为「跟随系统」的原始设置，先过 resolve_theme。传入未知取值
    时回落深色调色板，不抛异常——调色板要喂给控件上色，抛异常等于界面点一下就崩。

    :param theme: THEME_DARK / THEME_LIGHT（其余值回落深色）
    :return: 对应调色板
    """
    return _PALETTES.get(theme, DARK_PALETTE)


# ---------------------------------------------------------------------------
# 样式表
# ---------------------------------------------------------------------------


def stylesheet_for(theme: str) -> str:
    """生成一份完整 QSS 文本，可直接喂 QApplication.setStyleSheet()。

    覆盖常见控件类型。取色全部来自 palette_for，本函数不出现任何颜色字面量。

    :param theme: THEME_DARK / THEME_LIGHT（其余值回落深色）
    :return: QSS 文本
    """
    p = palette_for(theme)
    return f"""
QWidget {{
    background-color: {p.window};
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}

QLabel {{
    background: transparent;
    color: {p.text};
}}

QLabel[secondary="true"] {{
    color: {p.text_secondary};
}}

QGroupBox {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 12px;
    font-weight: bold;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {p.text_secondary};
}}

QPushButton {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 4px;
    padding: 5px 14px;
}}

QPushButton:hover {{
    border-color: {p.accent};
}}

QPushButton:pressed {{
    background-color: {p.accent};
    color: {p.accent_text};
}}

QPushButton:disabled {{
    color: {p.disabled};
    border-color: {p.border};
}}

QPushButton[accent="true"] {{
    background-color: {p.accent};
    color: {p.accent_text};
    border: none;
    font-weight: bold;
}}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 4px;
    padding: 3px 6px;
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {p.accent};
}}

QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {p.disabled};
    background-color: {p.surface_alt};
}}

QPlainTextEdit[log="true"] {{
    font-family: Consolas, "Cascadia Mono", monospace;
    background-color: {p.surface_alt};
}}

QComboBox {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 4px;
    padding: 3px 6px;
}}

QComboBox:hover {{
    border-color: {p.accent};
}}

QComboBox::drop-down {{
    border: none;
    width: 20px;
}}

QComboBox QAbstractItemView {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}

QCheckBox {{
    color: {p.text};
    spacing: 6px;
}}

QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {p.border};
    border-radius: 3px;
    background-color: {p.surface};
}}

QCheckBox::indicator:checked {{
    background-color: {p.accent};
    border-color: {p.accent};
}}

QProgressBar {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 4px;
}}

QProgressBar::chunk {{
    background-color: {p.accent};
    border-radius: 3px;
}}

QTabWidget::pane {{
    border: 1px solid {p.border};
    background-color: {p.surface};
    border-radius: 4px;
}}

QTabBar::tab {{
    background-color: {p.surface_alt};
    color: {p.text_secondary};
    padding: 6px 14px;
    border: 1px solid {p.border};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}}

QTabBar::tab:selected {{
    background-color: {p.surface};
    color: {p.text};
    border-bottom: 2px solid {p.accent};
}}

QTabBar::tab:hover {{
    color: {p.text};
}}

QScrollBar:vertical {{
    background: {p.window};
    width: 12px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {p.border};
    border-radius: 5px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background: {p.accent};
}}

QScrollBar:horizontal {{
    background: {p.window};
    height: 12px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background: {p.border};
    border-radius: 5px;
    min-width: 24px;
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}

QStatusBar {{
    background-color: {p.surface_alt};
    color: {p.text_secondary};
}}

QToolTip {{
    background-color: {p.surface_alt};
    color: {p.text};
    border: 1px solid {p.border};
    padding: 4px 6px;
}}
""".strip()


# ---------------------------------------------------------------------------
# 即时切换的封装
# ---------------------------------------------------------------------------


class ThemeManager:
    """持有「当前主题设置」，提供调色板与样式表访问，支持即时切换。

    刻意不持有 Settings / ConfigStore 或任何文件路径：本类只是「当前生效值 →
    具体调色板/样式表」这一步的封装，读取上次保存的主题、把新选择写回 settings.json
    是调用方（main_window）的事。这样存储格式改动不会波及本类。

    :param theme: 初始主题设置，可为 THEME_SYSTEM；默认深色
    :param reader: 转发给 resolve_theme 的可注入注册表读取函数，仅用于测试
    """

    def __init__(
        self, theme: str = THEME_DARK, *, reader: RegistryReader | None = None
    ) -> None:
        self._setting = theme if theme in THEME_CHOICES else THEME_DARK
        self._reader = reader

    @property
    def setting(self) -> str:
        """使用者选择的主题设置（可能是 THEME_SYSTEM，未必是具体主题）。"""
        return self._setting

    @property
    def resolved(self) -> str:
        """当前设置换算出的具体主题（THEME_DARK / THEME_LIGHT）。

        每次访问都重新判定，而不是缓存在 set_theme 时算好的值：跟随系统模式下
        系统主题可能在程序运行期间被切换，这里保证读到的始终是当下的系统状态。
        """
        return resolve_theme(self._setting, reader=self._reader)

    @property
    def palette(self) -> ThemePalette:
        """当前生效的调色板，供 ui/widgets 的高亮取色使用。"""
        return palette_for(self.resolved)

    @property
    def stylesheet(self) -> str:
        """当前生效的完整 QSS 文本。"""
        return stylesheet_for(self.resolved)

    def set_theme(self, theme: str) -> str:
        """切换主题设置（即时生效，不需要重启）。

        :param theme: 新设置，须属于 THEME_CHOICES；不属于时回落深色，不抛异常
        :return: 归一后实际生效的设置值（供调用方回写 settings.json）
        """
        self._setting = theme if theme in THEME_CHOICES else THEME_DARK
        return self._setting

    def apply(self, widget: object) -> str:
        """把当前样式表应用到一个对象（通常是 QApplication 实例）。

        接受 object 而不是 QApplication：本模块的纯逻辑部分不 import PySide6，
        只在这个便捷方法里假设传入的对象有 setStyleSheet。

        目标对象当前的样式表与要设的完全相同时跳过这次调用，避免无谓地让
        全部控件重算样式。

        :param widget: 任何提供 setStyleSheet(str) 方法的对象
        :return: 本次应用的具体主题（THEME_DARK / THEME_LIGHT）
        """
        resolved = self.resolved
        sheet = stylesheet_for(resolved)
        reader = getattr(widget, "styleSheet", None)
        if callable(reader) and reader() == sheet:
            return resolved
        widget.setStyleSheet(sheet)
        return resolved
