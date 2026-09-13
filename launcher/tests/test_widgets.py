"""测试 widgets.py 的纯逻辑函数（不依赖 Qt 事件循环）。"""

import pytest

from ninfer_launcher.params.spec import Bool3, ParamSpec, ParamKind
from ninfer_launcher.ui.widgets import (
    is_value_default,
    build_param_tooltip,
    highlight_stylesheet,
    PARAM_ACCENT,
)
from ninfer_launcher.params.registry import get


class TestIsValueDefault:
    """is_value_default 纯逻辑测试。"""

    def test_int_equal(self):
        assert is_value_default(8080, 8080) is True

    def test_int_not_equal(self):
        assert is_value_default(8081, 8080) is False

    def test_str_equal(self):
        assert is_value_default("mtp", "mtp") is True

    def test_str_not_equal(self):
        assert is_value_default("mtp", "none") is False

    def test_none_vs_none(self):
        assert is_value_default(None, None) is True

    def test_none_vs_int(self):
        assert is_value_default(None, 0) is False

    def test_none_vs_str(self):
        assert is_value_default(None, "") is False

    def test_bool3_on_vs_on_string(self):
        assert is_value_default(Bool3.ON, "on") is True

    def test_bool3_on_vs_true(self):
        assert is_value_default(Bool3.ON, True) is True

    def test_bool3_off_vs_false(self):
        assert is_value_default(Bool3.OFF, False) is True

    def test_bool3_off_vs_off_string(self):
        assert is_value_default(Bool3.OFF, "off") is True

    def test_bool3_on_vs_off(self):
        assert is_value_default(Bool3.ON, Bool3.OFF) is False

    def test_bool3_on_vs_off_string(self):
        assert is_value_default(Bool3.ON, "off") is False

    def test_float_close(self):
        assert is_value_default(1.0, 1.0) is True

    def test_float_isclose(self):
        # 0.1 + 0.2 != 0.3 in float, but isclose should handle it
        assert is_value_default(0.1 + 0.2, 0.3) is True

    def test_float_not_close(self):
        assert is_value_default(1.0, 2.0) is False

    def test_bool3_vs_none(self):
        # Bool3.OFF vs None: Bool3.from_config(None) = UNSET, Bool3.from_config(Bool3.OFF) = OFF
        # UNSET is not OFF, so they're not equal
        assert is_value_default(Bool3.OFF, None) is False

    def test_int_vs_bool3(self):
        # 1 是 int 不是 bool，Bool3.from_config(1) 抛异常 → 回退裸 ==
        assert is_value_default(Bool3.ON, 1) is False

    def test_true_vs_bool3_on(self):
        # True 是 bool，Bool3.from_config(True) = ON
        assert is_value_default(Bool3.ON, True) is True


class TestBuildParamTooltip:
    """build_param_tooltip 三段式 tooltip 测试。"""

    def test_vision_off(self):
        spec = get("vision")
        tooltip = build_param_tooltip(spec, Bool3.OFF)
        lines = tooltip.split("\n")
        assert len(lines) == 3
        assert lines[0].startswith("落参：")
        assert "不落参" in lines[0]
        assert "图像理解" in lines[1]
        assert lines[2].startswith("默认：")
        assert "关" in lines[2]

    def test_vision_on(self):
        spec = get("vision")
        tooltip = build_param_tooltip(spec, Bool3.ON)
        lines = tooltip.split("\n")
        assert "落参：--vision" in lines[0]
        assert lines[2].startswith("默认：")

    def test_port_default(self):
        spec = get("port")
        tooltip = build_param_tooltip(spec, 8080)
        lines = tooltip.split("\n")
        assert "落参：--port 8080" in lines[0]
        assert "8080" in lines[2]

    def test_port_changed(self):
        spec = get("port")
        tooltip = build_param_tooltip(spec, 9090)
        lines = tooltip.split("\n")
        assert "落参：--port 9090" in lines[0]

    def test_vision_max_tokens_none(self):
        spec = get("vision_max_tokens")
        tooltip = build_param_tooltip(spec, None)
        lines = tooltip.split("\n")
        assert "不落参" in lines[0]
        assert "不指定" in lines[2]

    def test_vision_max_tokens_set(self):
        spec = get("vision_max_tokens")
        tooltip = build_param_tooltip(spec, 4096)
        lines = tooltip.split("\n")
        assert "--vision-max-tokens 4096" in lines[0]

    def test_model_empty(self):
        spec = get("model")
        tooltip = build_param_tooltip(spec, "")
        lines = tooltip.split("\n")
        assert "不落参" in lines[0]

    def test_model_set(self):
        spec = get("model")
        tooltip = build_param_tooltip(spec, "E:\\models\\test.ninfer")
        lines = tooltip.split("\n")
        assert "E:" in lines[0]


class TestHighlightStylesheet:
    def test_returns_qss(self):
        result = highlight_stylesheet(PARAM_ACCENT)
        assert PARAM_ACCENT in result
        assert "color" in result

    def test_bold(self):
        result = highlight_stylesheet("#FF0000")
        assert "bold" in result
