"""回归：预设「删除」的历史缺陷。

1. 删除当前预设后下拉框要落到下一个可用预设：选中的预设被删掉后，下拉框应指向
   第一个可用预设（即「下一个」），让用户立刻能选下一个、参数同步切过去；只有当一个
   预设都不剩时才置空（index -1）。参数也要随之同步，避免「下拉框指着 B、参数还是 A」。

2. 删除内置（出厂）预设后，下次启动 seed_builtin_presets 会从 resources/presets
   把它重新播种回来，表现为「重启后删除的预设依然存在」。正确行为是**允许删除**并
   持久生效：删除时把内置预设名记入墓碑名单，seed 跳过它，重启后不再复活。
   （此前曾误改为「禁止删除内置预设」，把用户合法的删除操作拦掉了。）
"""

from PySide6.QtWidgets import QMessageBox

import pytest

from ninfer_launcher.core import config as config_mod
from ninfer_launcher.core.config import (
    builtin_preset_names,
    load_deleted_builtin,
    list_presets,
    record_builtin_deletion,
    save_preset,
    seed_builtin_presets,
)
from ninfer_launcher.ui.control_panel import _PresetGroup
from ninfer_launcher.ui.main_window import MainWindow


class _MsgRecorder:
    """把 QMessageBox 的交互调用记录下来，question 一律回答 Yes（模拟用户点确认）。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def question(self, *a, **k):
        self.calls.append(("question", a))
        # 模拟真实 GUI：PySide6 的 question 实际返回 int（如 16384），不是枚举单例。
        # 用 int 才能让「代码误用 is 比较」的回归在这里变红，而不是被枚举替身掩盖。
        return int(QMessageBox.StandardButton.Yes)

    def warning(self, *a, **k):
        self.calls.append(("warning", a))
        return QMessageBox.StandardButton.Ok

    def information(self, *a, **k):
        self.calls.append(("information", a))
        return QMessageBox.StandardButton.Ok

    def critical(self, *a, **k):
        self.calls.append(("critical", a))
        return QMessageBox.StandardButton.Ok

    def texts_of(self, method):
        return [a[-1] for kind, a in self.calls if kind == method and len(a) >= 3]


@pytest.fixture
def recorder(monkeypatch):
    rec = _MsgRecorder()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(rec.question))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(rec.warning))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(rec.information))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(rec.critical))
    return rec


@pytest.fixture
def window(monkeypatch, tmp_path):
    # 配置根指到临时目录，绝不碰真实 settings.json / presets
    monkeypatch.setattr(config_mod, "resolve_config_root", lambda probe=None: tmp_path)
    win = MainWindow()
    yield win
    win.close()


class TestPresetComboRefresh:
    """set_preset_names：选中的预设被删掉后要落到第一个可用预设（删光才置空）。"""

    def test_keeps_current_when_still_present(self):
        g = _PresetGroup()
        g.set_preset_names(["alpha", "beta"], current="alpha")
        assert g.combo.currentData() == "alpha"
        # 普通刷新（current=None）且 alpha 仍在 → 保持选中
        g.set_preset_names(["alpha", "beta", "gamma"], current=None)
        assert g.combo.currentData() == "alpha"

    def test_falls_to_first_available_when_selected_was_deleted(self):
        g = _PresetGroup()
        g.set_preset_names(["alpha", "beta"], current="alpha")
        assert g.combo.currentData() == "alpha"
        # alpha 被删：新列表只剩 beta → 下拉框应落到 beta（下一个可用），而非停在空态。
        g.set_preset_names(["beta"], current=None)
        assert g.combo.currentData() == "beta", "删掉当前预设后下拉框应落到下一个可用预设"

    def test_first_launch_defaults_to_first(self):
        g = _PresetGroup()
        # 从未选中过（previous 为 None）→ 默认选第一个
        g.set_preset_names(["alpha", "beta"], current=None)
        assert g.combo.currentData() == "alpha"

    def test_empty_list_deselects(self):
        g = _PresetGroup()
        g.set_preset_names(["alpha"], current="alpha")
        g.set_preset_names([], current=None)
        assert g.combo.currentData() is None


class TestDeleteBuiltinPersists:
    """删内置（出厂）预设应真正删掉并持久生效，重启后不再被 seed 复活。"""

    def test_builtin_delete_persists_across_reseed(self, window, recorder, tmp_path):
        builtins = builtin_preset_names()
        assert builtins, "开发态 resources/presets 应含内置预设"
        name = sorted(builtins)[0]
        # 确保该内置预设此刻在磁盘上（MainWindow 启动时已 seed 过，这里兜底再确认）
        assert name in list_presets(tmp_path), "内置预设应已被 seed 到配置根"
        group = window._control.get_preset_group()
        group.select_preset_silently(name)
        assert group.current_preset_name() == name

        group.deleteRequested.emit()  # 走真实删除链路（含确认框，recorder 答 Yes）

        # (1) 文件真的被删 (2) 记入墓碑名单 (3) 下拉框置空
        assert not (tmp_path / "presets" / (name + ".json")).exists()
        assert name in load_deleted_builtin(tmp_path)
        assert group.combo.currentData() is None
        assert window._settings.last_preset == ""

        # (4) 关键：再次 seed（等价于「重启」）不会把它复活
        seed_builtin_presets(tmp_path)
        assert name not in list_presets(tmp_path), "删掉的内置预设不应被 seed 复活"

    def test_builtin_delete_does_not_hide_user_presets(self, window, recorder, tmp_path):
        # 删内置预设只影响该内置预设本身，用户预设和其它内置预设不受牵连
        save_preset(tmp_path, "keep-user", {"name": "keep-user", "schema": 1, "params": {}})
        window._refresh_preset_list()
        group = window._control.get_preset_group()
        name = sorted(builtin_preset_names())[0]
        group.select_preset_silently(name)
        group.deleteRequested.emit()

        remaining = list_presets(tmp_path)
        assert name not in remaining
        assert "keep-user" in remaining, "删内置预设不应波及用户预设"


class TestDeleteUserPreset:
    """用户另存的预设：删除后真正落盘、下拉框置空、重启不复活。"""

    def test_user_preset_deleted_falls_to_next(self, window, recorder, tmp_path):
        save_preset(tmp_path, "del-target", {"name": "del-target", "schema": 1, "params": {}})
        window._refresh_preset_list()
        group = window._control.get_preset_group()
        group.select_preset_silently("del-target")
        assert group.current_preset_name() == "del-target"

        group.deleteRequested.emit()

        assert not (tmp_path / "presets" / "del-target.json").exists()
        assert "del-target" not in window._store.list_presets()
        # 删掉后下拉框应落到「下一个可用预设」：即剩余列表的第一个（内置预设仍在）。
        # last_preset 也同步指向它，参数随之切过去，避免「下拉框指着 B、参数还是已删的 A」。
        remaining = window._store.list_presets()
        assert "del-target" not in remaining
        expected_next = remaining[0] if remaining else None
        assert group.combo.currentData() == expected_next, "删除后下拉框应落到下一个可用预设"
        assert window._settings.last_preset == (expected_next or "")
        # 用户预设删除**不需要**也不应写墓碑（它本就不在 resources/presets 里）
        assert "del-target" not in load_deleted_builtin(tmp_path)

    def test_user_preset_deleted_all_presets_combo_blanks(self, window, recorder, tmp_path):
        # 极端：删光所有预设后，下拉框才置空（index -1）。
        save_preset(tmp_path, "only-user", {"name": "only-user", "schema": 1, "params": {}})
        # 把内置预设文件删掉（并记墓碑），使列表里只剩这一个用户预设
        for b in sorted(builtin_preset_names()):
            record_builtin_deletion(tmp_path, b)
            window._store.delete_preset(b)
        window._refresh_preset_list()
        group = window._control.get_preset_group()
        group.select_preset_silently("only-user")
        assert group.current_preset_name() == "only-user"
        group.deleteRequested.emit()
        assert window._store.list_presets() == []
        assert group.combo.currentData() is None, "删光所有预设后下拉框应置空"
        assert window._settings.last_preset == ""

    def test_deleted_user_preset_not_resurrected_by_reseed(self, window, recorder, tmp_path):
        save_preset(tmp_path, "gone-on-restart", {"name": "gone-on-restart", "schema": 1, "params": {}})
        window._refresh_preset_list()
        group = window._control.get_preset_group()
        group.select_preset_silently("gone-on-restart")
        group.deleteRequested.emit()
        assert "gone-on-restart" not in list_presets(tmp_path)

        seed_builtin_presets(tmp_path)  # 重启等价于再 seed 一次
        assert "gone-on-restart" not in list_presets(tmp_path), "seed 只补内置预设，不该复活用户预设"


class TestBuiltinSurvivesReseedWhenNotDeleted:
    """对照：没被删的内置预设，「重启」播种后始终在（墓碑只影响被删的那个）。"""

    def test_untouched_builtin_still_seeded(self, tmp_path):
        builtins = sorted(builtin_preset_names())
        if not builtins:
            pytest.skip("开发态 resources/presets 无内置预设")
        keep = builtins[0]
        # 删掉其中另一个（若存在），确认 keep 不受影响
        if len(builtins) > 1:
            record_builtin_deletion(tmp_path, builtins[1])
        seed_builtin_presets(tmp_path)
        assert keep in list_presets(tmp_path), "未被删除的内置预设应继续被 seed"