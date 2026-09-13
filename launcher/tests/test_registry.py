"""测试参数注册表。"""

import pytest

from ninfer_launcher.params.registry import (
    ALL_PARAMS,
    PARAMS_BY_KEY,
    EXPECTED_PARAM_COUNT,
    default_values,
    get,
    groups_for_tab,
    params_for_tab,
    validate_values,
)


class TestRegistry:
    def test_param_count(self):
        assert len(ALL_PARAMS) == EXPECTED_PARAM_COUNT == 12

    def test_no_duplicate_keys(self):
        keys = [spec.key for spec in ALL_PARAMS]
        assert len(keys) == len(set(keys))

    def test_no_duplicate_flags(self):
        flags = [spec.flag for spec in ALL_PARAMS]
        assert len(flags) == len(set(flags))

    def test_get_existing(self):
        spec = get("port")
        assert spec.flag == "--port"
        assert spec.default == 8080

    def test_get_missing(self):
        with pytest.raises(KeyError):
            get("nonexistent")

    def test_default_values_keys(self):
        defaults = default_values()
        assert set(defaults.keys()) == set(PARAMS_BY_KEY.keys())

    def test_default_port(self):
        assert default_values()["port"] == 8080

    def test_default_kv_dtype(self):
        assert default_values()["kv_dtype"] == "rk4v4-e8"

    def test_default_spec(self):
        assert default_values()["spec"] == "mtp"

    def test_default_draft_tokens(self):
        assert default_values()["draft_tokens"] == 7

    def test_default_max_context(self):
        assert default_values()["max_context"] == 163840

    def test_default_prefill_chunk(self):
        assert default_values()["prefill_chunk"] == 1024

    def test_params_for_tab(self):
        specs = params_for_tab("参数")
        assert len(specs) == 12

    def test_groups_for_tab(self):
        groups = groups_for_tab("参数")
        assert "服务基础" in groups
        assert "上下文与显存" in groups
        assert "投机解码" in groups
        assert "思考与推理" in groups
        assert "视觉" in groups

    def test_default_vision(self):
        from ninfer_launcher.params.spec import Bool3
        assert default_values()["vision"] is Bool3.OFF

    def test_default_vision_max_tokens(self):
        assert default_values()["vision_max_tokens"] is None

    def test_vision_disabled_when(self):
        spec = get("vision_max_tokens")
        assert ("vision", "off") in spec.disabled_when

    def test_kv_dtype_choices(self):
        spec = get("kv_dtype")
        assert "rk4v4-e8" in spec.choices
        assert "bf16" in spec.choices
        assert len(spec.choices) == 6


class TestValidation:
    def test_valid_defaults(self):
        errors = validate_values(default_values())
        assert errors == []

    def test_invalid_kv_dtype(self):
        values = default_values()
        values["kv_dtype"] = "invalid_type"
        errors = validate_values(values)
        assert len(errors) > 0

    def test_prefill_chunk_not_multiple_of_128(self):
        values = default_values()
        values["prefill_chunk"] = 100
        errors = validate_values(values)
        assert any("128" in e for e in errors)

    def test_draft_tokens_out_of_range(self):
        values = default_values()
        values["draft_tokens"] = 20
        errors = validate_values(values)
        assert any("15" in e or "上限" in e for e in errors)

    def test_port_out_of_range(self):
        values = default_values()
        values["port"] = 0
        errors = validate_values(values)
        assert any("下限" in e for e in errors)
