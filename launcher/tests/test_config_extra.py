"""core/config.py 补充测试：seed_builtin_presets / record_builtin_deletion /
builtin_preset_names / load_deleted_builtin 的完整行为。
"""

import json
from pathlib import Path

import pytest

from ninfer_launcher.core.config import (
    builtin_preset_names,
    load_deleted_builtin,
    record_builtin_deletion,
    resolve_config_root,
    save_preset,
    seed_builtin_presets,
)


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    return tmp_path / "ninfer-launcher"


@pytest.fixture
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin-presets"
    d.mkdir()
    # 创建两个内置预设
    (d / "alpha.json").write_text(
        json.dumps({"name": "Alpha", "schema": 1, "params": {}}), encoding="utf-8"
    )
    (d / "beta.json").write_text(
        json.dumps({"name": "Beta", "schema": 1, "params": {}}), encoding="utf-8"
    )
    return d


class TestSeedBuiltinPresets:
    """seed_builtin_presets：只补缺失文件，不覆盖已有文件。"""

    def test_seeds_all_when_empty(self, config_root, builtin_dir):
        config_root.mkdir(parents=True)
        seeded = seed_builtin_presets(config_root, builtin_dir)
        assert sorted(seeded) == ["alpha", "beta"]
        assert (config_root / "presets" / "alpha.json").is_file()
        assert (config_root / "presets" / "beta.json").is_file()

    def test_does_not_overwrite_existing(self, config_root, builtin_dir):
        """已有同名文件不被覆盖。"""
        presets = config_root / "presets"
        presets.mkdir(parents=True)
        (presets / "alpha.json").write_text("user modified", encoding="utf-8")

        seeded = seed_builtin_presets(config_root, builtin_dir)
        assert "alpha" not in seeded
        assert "beta" in seeded
        # alpha 的内容没被覆盖
        assert (presets / "alpha.json").read_text(encoding="utf-8") == "user modified"

    def test_skips_deleted_builtin(self, config_root, builtin_dir):
        """已记录删除的内置预设不被播种。"""
        config_root.mkdir(parents=True)
        record_builtin_deletion(config_root, "alpha")

        seeded = seed_builtin_presets(config_root, builtin_dir)
        assert "alpha" not in seeded
        assert "beta" in seeded
        assert not (config_root / "presets" / "alpha.json").exists()

    def test_idempotent_second_call(self, config_root, builtin_dir):
        """第二次调用不再播种（文件已存在）。"""
        config_root.mkdir(parents=True)
        first = seed_builtin_presets(config_root, builtin_dir)
        second = seed_builtin_presets(config_root, builtin_dir)
        assert len(first) == 2
        assert second == []

    def test_none_builtin_dir(self, config_root, monkeypatch):
        """builtin_dir=None 且 builtin_presets_dir() 返回 None 时不播种。"""
        import ninfer_launcher.core.config as cfg
        config_root.mkdir(parents=True)
        monkeypatch.setattr(cfg, "builtin_presets_dir", lambda: None)
        assert seed_builtin_presets(config_root, None) == []

    def test_empty_builtin_dir(self, config_root, tmp_path):
        """内置目录存在但为空。"""
        empty = tmp_path / "empty-presets"
        empty.mkdir()
        config_root.mkdir(parents=True)
        assert seed_builtin_presets(config_root, empty) == []


class TestBuiltinPresetNames:
    """builtin_preset_names：返回内置预设名集合。"""

    def test_returns_names(self, builtin_dir):
        """通过 monkeypatch builtin_presets_dir 让它返回测试目录。"""
        from ninfer_launcher.core import config as cfg
        import unittest.mock

        with unittest.mock.patch.object(cfg, "builtin_presets_dir", return_value=builtin_dir):
            names = builtin_preset_names()
            assert "alpha" in names
            assert "beta" in names
            assert isinstance(names, frozenset)

    def test_none_dir_returns_empty(self):
        from ninfer_launcher.core import config as cfg
        import unittest.mock

        with unittest.mock.patch.object(cfg, "builtin_presets_dir", return_value=None):
            assert builtin_preset_names() == frozenset()


class TestDeletedBuiltin:
    """load_deleted_builtin / record_builtin_deletion：墓碑机制。"""

    def test_load_missing_file(self, config_root):
        config_root.mkdir(parents=True)
        assert load_deleted_builtin(config_root) == frozenset()

    def test_record_then_load(self, config_root):
        config_root.mkdir(parents=True)
        record_builtin_deletion(config_root, "alpha")
        deleted = load_deleted_builtin(config_root)
        assert "alpha" in deleted

    def test_record_multiple(self, config_root):
        config_root.mkdir(parents=True)
        record_builtin_deletion(config_root, "alpha")
        record_builtin_deletion(config_root, "beta")
        deleted = load_deleted_builtin(config_root)
        assert deleted == frozenset({"alpha", "beta"})

    def test_record_idempotent(self, config_root):
        """重复记录不产生重复。"""
        config_root.mkdir(parents=True)
        record_builtin_deletion(config_root, "alpha")
        record_builtin_deletion(config_root, "alpha")
        deleted = load_deleted_builtin(config_root)
        assert list(deleted) == ["alpha"]

    def test_corrupt_file_returns_empty(self, config_root):
        config_root.mkdir(parents=True)
        (config_root / ".deleted_builtin_presets").write_text("not json", encoding="utf-8")
        assert load_deleted_builtin(config_root) == frozenset()

    def test_non_list_json_returns_empty(self, config_root):
        config_root.mkdir(parents=True)
        (config_root / ".deleted_builtin_presets").write_text(
            '{"a": 1}', encoding="utf-8"
        )
        assert load_deleted_builtin(config_root) == frozenset()


class TestResolveConfigRoot:
    """resolve_config_root：用户级持久目录。"""

    def test_uses_localappdata(self, monkeypatch):
        import os
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
        root = resolve_config_root()
        assert root.name == "ninfer-launcher"
        assert "ninfer-launcher" in str(root)

    def test_probe_override(self, tmp_path):
        """probe 参数优先级最高。"""
        root = resolve_config_root(probe=lambda: tmp_path / "custom")
        assert root == tmp_path / "custom"

    def test_creates_directory(self, monkeypatch, tmp_path):
        import os
        target = tmp_path / "fake-local"
        monkeypatch.setenv("LOCALAPPDATA", str(target))
        root = resolve_config_root()
        assert root.is_dir()
