"""回归：预设「另存为 / 保存配置」静默失败（点完无反馈、列表不刷新）。

根因：MainWindow._build_preset_data 把 ParamsTab 的原样值写进 params，其中 BOOL3 参数
（lm_head_draft / no_thinking / vision）的值域类型是 Bool3（Enum）。save_preset 内部的
json.dumps 无法序列化 Bool3，抛 TypeError；而槽函数只捕获 OSError，于是异常逃逸，
刷新列表 / 落日志 / 弹框全部没执行——表现为「另存为后预设列表不显示 / 功能无效」。

本文件锁两点：

1. _param_value_to_config 把 Bool3 归一成 JSON 可序列化字符串（on/off/unset），原生类型透传；
2. 驱动真实 MainWindow 的「另存为」信号链路，新预设必须落盘、进列表、进下拉框。
"""

import json

import pytest

from PySide6.QtWidgets import QInputDialog

from ninfer_launcher.params.spec import Bool3
from ninfer_launcher.ui.main_window import MainWindow, _param_value_to_config


class TestPresetParamNormalization:
    """_param_value_to_config：值域类型 -> JSON 可序列化配置形式。"""

    def test_bool3_normalized_to_string(self):
        assert _param_value_to_config(Bool3.ON) == "on"
        assert _param_value_to_config(Bool3.OFF) == "off"
        assert _param_value_to_config(Bool3.UNSET) == "unset"

    def test_native_types_passthrough(self):
        assert _param_value_to_config(7) == 7
        assert _param_value_to_config(8080) == 8080
        assert _param_value_to_config(1.5) == 1.5
        assert _param_value_to_config("rk4v4-e8") == "rk4v4-e8"
        assert _param_value_to_config(None) is None

    def test_mixed_params_is_json_serializable(self):
        # 复现修复前的崩溃场景：含 Bool3 的 params 字典必须能被 json.dumps 序列化。
        params = {
            "lm_head_draft": Bool3.ON,
            "no_thinking": Bool3.OFF,
            "vision": Bool3.UNSET,
            "draft_tokens": 7,
            "kv_dtype": "rk4v4-e8",
            "reasoning_effort": "low",
            "max_context": 163840,
            "vision_max_tokens": None,
        }
        normalized = {k: _param_value_to_config(v) for k, v in params.items()}
        # 修复前这一行抛 TypeError: Object of type Bool3 is not JSON serializable
        json.dumps(normalized, ensure_ascii=False, indent=2)
        assert normalized["lm_head_draft"] == "on"
        assert normalized["no_thinking"] == "off"
        assert normalized["vision"] == "unset"
        assert normalized["draft_tokens"] == 7
        assert normalized["vision_max_tokens"] is None


class TestSaveAsEndToEnd:
    """驱动真实 MainWindow 的「另存为」信号链路（用户报告：另存为后列表不显示）。"""

    @pytest.fixture
    def window(self, monkeypatch, tmp_path):
        # 把配置根指到临时目录，绝不碰真实 settings.json / presets
        from ninfer_launcher.core import config as config_mod

        monkeypatch.setattr(config_mod, "resolve_config_root", lambda probe=None: tmp_path)
        win = MainWindow()
        yield win
        win.close()

    @staticmethod
    def _stub_input(monkeypatch, name):
        monkeypatch.setattr(
            QInputDialog,
            "getText",
            staticmethod(lambda parent, title, label, **kw: (name, True)),
        )

    def test_save_as_lands_on_disk_and_refreshes_list(self, window, monkeypatch):
        self._stub_input(monkeypatch, "e2e-new-preset")
        group = window._control.get_preset_group()
        before = set(window._store.list_presets())
        assert "e2e-new-preset" not in before

        group.saveAsRequested.emit()  # 真实信号链路（按钮 -> 槽 -> 落盘 -> 刷新）

        # (1) 落盘 (2) 进列表 (3) 进下拉框并被选中
        assert "e2e-new-preset" in window._store.list_presets()
        combo_names = {group.combo.itemText(i) for i in range(group.combo.count())}
        assert "e2e-new-preset" in combo_names
        assert group.combo.currentData() == "e2e-new-preset"

    def test_saved_preset_reloadable_and_bool3_is_string(self, window, monkeypatch, tmp_path):
        # 落盘的预设必须能被读回，且 BOOL3 值以字符串形式存储（可被 from_config 还原）
        self._stub_input(monkeypatch, "e2e-roundtrip")
        window._control.get_preset_group().saveAsRequested.emit()

        path = tmp_path / "presets" / "e2e-roundtrip.json"
        assert path.is_file()
        data = window._store.load_preset("e2e-roundtrip")
        assert data is not None
        assert isinstance(data["params"]["lm_head_draft"], str)
        assert Bool3.from_config(data["params"]["lm_head_draft"]) in (Bool3.ON, Bool3.OFF)
