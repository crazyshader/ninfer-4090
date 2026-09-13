"""ui/theme.py：纯逻辑（resolve_theme / palette_for / stylesheet_for / interpret）
+ ThemeManager 状态机。

本文件不碰真实注册表——reader 参数注入假读取函数。
"""

import pytest

from ninfer_launcher.ui.theme import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    ThemeManager,
    ThemePalette,
    detect_system_theme,
    interpret_apps_use_light_theme,
    palette_for,
    read_apps_use_light_theme,
    resolve_theme,
    stylesheet_for,
)
from ninfer_launcher.core.config import (
    THEME_CHOICES,
    THEME_DARK,
    THEME_LIGHT,
    THEME_SYSTEM,
)


class TestInterpretAppsUseLightTheme:
    """interpret_apps_use_light_theme：DWORD → 主题名。"""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, THEME_DARK),
            (1, THEME_LIGHT),
            (None, THEME_DARK),
            (2, THEME_DARK),   # 非法值 → 深色
            (-1, THEME_DARK),  # 负数 → 深色
            ("dark", THEME_DARK),  # 非数字 → 深色
        ],
    )
    def test_interpret(self, value, expected):
        assert interpret_apps_use_light_theme(value) == expected

    def test_bool_true_is_dark(self):
        """bool True 是 int 1 的子类，必须先拦截 → 深色。"""
        assert interpret_apps_use_light_theme(True) == THEME_DARK

    def test_bool_false_is_dark(self):
        assert interpret_apps_use_light_theme(False) == THEME_DARK


class TestReadAppsUseLightTheme:
    """read_apps_use_light_theme：注入 reader 或真实读取。"""

    def test_injected_reader(self):
        assert read_apps_use_light_theme(reader=lambda: 1) == 1

    def test_injected_reader_raises(self):
        def bad_reader():
            raise OSError("no such key")
        assert read_apps_use_light_theme(reader=bad_reader) is None

    def test_injected_reader_returns_none(self):
        assert read_apps_use_light_theme(reader=lambda: None) is None


class TestDetectSystemTheme:
    """detect_system_theme：组合 read + interpret。"""

    def test_light(self):
        assert detect_system_theme(reader=lambda: 1) == THEME_LIGHT

    def test_dark(self):
        assert detect_system_theme(reader=lambda: 0) == THEME_DARK

    def test_reader_fails(self):
        """注册表读取失败 → 深色。"""
        def bad():
            raise Exception("boom")
        assert detect_system_theme(reader=bad) == THEME_DARK


class TestResolveTheme:
    """resolve_theme：设置值 → 具体主题。"""

    def test_dark_passthrough(self):
        assert resolve_theme(THEME_DARK) == THEME_DARK

    def test_light_passthrough(self):
        assert resolve_theme(THEME_LIGHT) == THEME_LIGHT

    def test_system_resolves_to_light(self):
        assert resolve_theme(THEME_SYSTEM, reader=lambda: 1) == THEME_LIGHT

    def test_system_resolves_to_dark(self):
        assert resolve_theme(THEME_SYSTEM, reader=lambda: 0) == THEME_DARK

    def test_unknown_falls_back_to_dark(self):
        assert resolve_theme("blue") == THEME_DARK

    def test_empty_string_falls_back_to_dark(self):
        assert resolve_theme("") == THEME_DARK


class TestPaletteFor:
    """palette_for：主题名 → ThemePalette。"""

    def test_dark(self):
        assert palette_for(THEME_DARK) is DARK_PALETTE

    def test_light(self):
        assert palette_for(THEME_LIGHT) is LIGHT_PALETTE

    def test_unknown_falls_back_to_dark(self):
        assert palette_for("blue") is DARK_PALETTE

    def test_palette_fields(self):
        p = palette_for(THEME_DARK)
        assert p.name == THEME_DARK
        assert p.window
        assert p.surface
        assert p.text
        assert p.accent
        assert p.error


class TestStylesheetFor:
    """stylesheet_for：生成完整 QSS 文本。"""

    def test_dark_stylesheet_not_empty(self):
        qss = stylesheet_for(THEME_DARK)
        assert len(qss) > 100
        assert "QWidget" in qss

    def test_light_stylesheet_not_empty(self):
        qss = stylesheet_for(THEME_LIGHT)
        assert len(qss) > 100

    def test_dark_and_light_differ(self):
        assert stylesheet_for(THEME_DARK) != stylesheet_for(THEME_LIGHT)

    def test_unknown_theme_uses_dark(self):
        assert stylesheet_for("blue") == stylesheet_for(THEME_DARK)


class TestThemeManager:
    """ThemeManager：状态持有 + 即时切换。"""

    def test_default_is_dark(self):
        tm = ThemeManager()
        assert tm.setting == THEME_DARK

    def test_set_theme_light(self):
        tm = ThemeManager()
        applied = tm.set_theme(THEME_LIGHT)
        assert applied == THEME_LIGHT
        assert tm.setting == THEME_LIGHT

    def test_set_theme_system(self):
        tm = ThemeManager(theme=THEME_SYSTEM, reader=lambda: 1)
        assert tm.setting == THEME_SYSTEM
        assert tm.resolved == THEME_LIGHT

    def test_set_theme_unknown_falls_back(self):
        tm = ThemeManager()
        applied = tm.set_theme("neon")
        assert applied == THEME_DARK
        assert tm.setting == THEME_DARK

    def test_resolved_follows_reader(self):
        state = {"value": 0}
        tm = ThemeManager(theme=THEME_SYSTEM, reader=lambda: state["value"])
        assert tm.resolved == THEME_DARK
        state["value"] = 1
        assert tm.resolved == THEME_LIGHT

    def test_palette_tracks_resolved(self):
        tm = ThemeManager(theme=THEME_DARK)
        assert tm.palette is DARK_PALETTE
        tm.set_theme(THEME_LIGHT)
        assert tm.palette is LIGHT_PALETTE

    def test_apply_calls_set_stylesheet(self):
        """apply 调用 widget.setStyleSheet。"""
        applied_sheets = []

        class FakeWidget:
            def __init__(self):
                self._sheet = ""
            def setStyleSheet(self, s):
                self._sheet = s
            def styleSheet(self):
                return self._sheet

        w = FakeWidget()
        tm = ThemeManager(theme=THEME_DARK)
        result = tm.apply(w)
        assert result == THEME_DARK
        assert len(w._sheet) > 0

    def test_apply_skips_if_unchanged(self):
        """样式表未变时跳过 setStyleSheet（避免无谓重算）。"""
        call_count = 0

        class FakeWidget:
            def __init__(self):
                self._sheet = ""
            def setStyleSheet(self, s):
                nonlocal call_count
                call_count += 1
                self._sheet = s
            def styleSheet(self):
                return self._sheet

        w = FakeWidget()
        tm = ThemeManager(theme=THEME_DARK)
        tm.apply(w)
        first_count = call_count
        tm.apply(w)  # 相同主题 → 跳过
        assert call_count == first_count
