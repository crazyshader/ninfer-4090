"""测试配置与预设的读写。"""

import json
import pytest
from pathlib import Path

from ninfer_launcher.core.config import (
    check_preset_name,
    ConfigStore,
    Settings,
    load_preset,
    load_settings,
    list_presets,
    project_root,
    resolve_config_root,
    save_preset,
    save_settings,
    delete_preset,
)


class TestSettings:
    def test_roundtrip(self, tmp_path):
        settings = Settings(last_preset="test", window_width=1200, window_height=800)
        save_settings(tmp_path, settings)

        loaded = load_settings(tmp_path)
        assert loaded.last_preset == "test"
        assert loaded.window_width == 1200
        assert loaded.window_height == 800

    def test_missing_file(self, tmp_path):
        settings = load_settings(tmp_path)
        assert settings.last_preset == ""
        assert settings.window_width == 1100

    def test_corrupt_file(self, tmp_path):
        (tmp_path / "settings.json").write_text("{invalid json", encoding="utf-8")
        settings = load_settings(tmp_path)
        assert settings.last_preset == ""


class TestPresets:
    def test_save_and_load(self, tmp_path):
        data = {"name": "test", "params": {"port": 8081, "kv_dtype": "bf16"}}
        save_preset(tmp_path, "test-preset", data)

        loaded = load_preset(tmp_path, "test-preset")
        assert loaded is not None
        assert loaded["params"]["port"] == 8081
        assert loaded["params"]["kv_dtype"] == "bf16"

    def test_list_presets(self, tmp_path):
        save_preset(tmp_path, "alpha", {"params": {}})
        save_preset(tmp_path, "beta", {"params": {}})
        names = list_presets(tmp_path)
        assert "alpha" in names
        assert "beta" in names

    def test_delete_preset(self, tmp_path):
        save_preset(tmp_path, "to-delete", {"params": {}})
        assert delete_preset(tmp_path, "to-delete")
        assert load_preset(tmp_path, "to-delete") is None

    def test_load_missing(self, tmp_path):
        assert load_preset(tmp_path, "nonexistent") is None

    def test_unicode_preset_name(self, tmp_path):
        data = {"name": "中文预设", "params": {"model": "E:\\ai\\model.ninfer"}}
        save_preset(tmp_path, "中文预设", data)
        loaded = load_preset(tmp_path, "中文预设")
        assert loaded is not None
        assert loaded["params"]["model"] == "E:\\ai\\model.ninfer"


class TestConfigStore:
    def test_full_cycle(self, tmp_path):
        store = ConfigStore(tmp_path)
        settings = store.load_settings()
        settings.window_width = 1400
        store.save_settings(settings)

        store2 = ConfigStore(tmp_path)
        loaded = store2.load_settings()
        assert loaded.window_width == 1400



class TestCheckPresetName:
    """check_preset_name：预设名最终要落成 presets/<name>.json 的文件名 stem，
    覆盖它拦截的每一类非法输入（这正是「另存为后列表不显示」的守门逻辑）。
    """

    def test_valid_ascii(self):
        assert check_preset_name("good-name") is None
        assert check_preset_name("Qwen3.8-27B-MTP") is None

    def test_valid_unicode(self):
        assert check_preset_name("中文预设") is None

    def test_surrounding_whitespace_is_stripped(self):
        # 前后空格在落盘前被 strip，不应报错（Windows 本身也会忽略首尾空格）
        assert check_preset_name("  spaced  ") is None

    def test_empty_and_blank(self):
        assert check_preset_name("") is not None
        assert check_preset_name("   ") is not None

    def test_non_string(self):
        assert check_preset_name(123) is not None

    def test_illegal_forward_slash(self):
        assert check_preset_name("a/b") is not None

    def test_illegal_backslash(self):
        assert check_preset_name("a" + chr(92) + "b") is not None

    def test_illegal_colon(self):
        assert check_preset_name("a:b") is not None

    def test_illegal_star(self):
        assert check_preset_name("a*b") is not None

    def test_illegal_pipe(self):
        assert check_preset_name("a|b") is not None

    def test_reserved_device_names(self):
        assert check_preset_name("CON") is not None
        assert check_preset_name("com1") is not None
        assert check_preset_name("nul") is not None

    def test_trailing_dot(self):
        # Windows 文件名不允许以点结尾（会被静默截断，导致列表名与磁盘名对不上）
        assert check_preset_name("foo.") is not None

    def test_overlong(self):
        assert check_preset_name("a" * 60) is not None


class TestSettingsModelDir:
    """回归：源码里的 model_dir 默认值曾因单反斜杠被 Python 解析成 BEL+换行（
    'E:\\ai\\ninfer-4090' 误写成 'E:\\ai' + chr(92) + 'ainfer...' 之类的转义陷阱）。
    """

    def test_default_model_dir_has_no_control_chars(self):
        s = Settings()
        assert chr(7) not in s.model_dir, "model_dir 默认值里混进了 BEL（反斜杠转义错误）"
        assert chr(10) not in s.model_dir, "model_dir 默认值里混进了换行（反斜杠转义错误）"
        assert chr(13) not in s.model_dir
        assert s.model_dir.startswith("E:")


class TestPresetListAfterSave:
    """回归用户报告的「另存为后预设列表不显示」：
    只要 save_preset 成功，list_presets 必须能看到新名字，且文件可被 load_preset 读回。
    """

    def test_newly_saved_preset_appears_in_list(self, tmp_path):
        save_preset(tmp_path, "my-new-preset", {"name": "my-new-preset", "schema": 1, "params": {}})
        assert "my-new-preset" in list_presets(tmp_path)
        assert load_preset(tmp_path, "my-new-preset") is not None

    def test_saved_schema_roundtrip_keeps_top_level_model_port(self, tmp_path):
        # 与 MainWindow._build_preset_data 的新 schema 对齐：
        # model/port 提到顶层，params 里不再重复存放这两个键。
        data = {
            "name": "roundtrip",
            "schema": 1,
            "model": "E:" + chr(92) + "ai" + chr(92) + "model.ninfer",
            "port": 8090,
            "params": {"kv_dtype": "bf16", "mtp": 3},
        }
        save_preset(tmp_path, "roundtrip", data)
        loaded = load_preset(tmp_path, "roundtrip")
        assert loaded["model"] == "E:" + chr(92) + "ai" + chr(92) + "model.ninfer"
        assert loaded["port"] == 8090
        assert "model" not in loaded["params"]
        assert "port" not in loaded["params"]
        assert loaded["params"]["kv_dtype"] == "bf16"

    def test_old_format_compat_model_in_params(self, tmp_path):
        # 旧预设把 model 塞在 params 里（没有顶层 model 字段）。
        # MainWindow 的读取逻辑是 preset.get("model") or params.get("model")，这里固化这一兼容约定。
        data = {"name": "legacy", "params": {"model": "E:" + chr(92) + "old.ninfer", "port": 9000}}
        save_preset(tmp_path, "legacy", data)
        loaded = load_preset(tmp_path, "legacy")
        top_model = loaded.get("model") or loaded.get("params", {}).get("model")
        assert top_model == "E:" + chr(92) + "old.ninfer"


class TestFindProjectRoot:
    """find_project_root：从 project_root() 逐级向上找含 build-ninja/ 的那一层。

    回归（docs/01-ninfer-launcher-cli.md 12.1）：首版迁移后的实现丢了
    p = p.parent，6 次循环全在检查同一个目录、总落回退分支，开发模式下回退值
    （launcher/ 的上级）恰好就是项目根，454 个测试无一变红。这里 monkeypatch
    project_root 返回「比假项目根深两级」的起点（等价 onedir 打包布局：exe
    目录在项目根下两层），断言真的能向上找到含 build-ninja/ 的层——旧实现
    在此输入下会返回起点上一级而断言失败。
    """

    def test_walks_up_from_deeper_start(self, tmp_path, monkeypatch):
        import ninfer_launcher.core.config as cfg

        proj = tmp_path / "proj"
        (proj / "build-ninja" / "apps").mkdir(parents=True)
        deep = proj / "app" / "exe_dir"
        deep.mkdir(parents=True)
        monkeypatch.setattr(cfg, "project_root", lambda: deep)
        assert cfg.find_project_root() == proj

    def test_base_itself_is_project_root(self, tmp_path, monkeypatch):
        import ninfer_launcher.core.config as cfg

        proj = tmp_path / "proj"
        (proj / "build-ninja").mkdir(parents=True)
        monkeypatch.setattr(cfg, "project_root", lambda: proj)
        assert cfg.find_project_root() == proj

    def test_fallback_parent_when_no_build_ninja(self, tmp_path, monkeypatch):
        import ninfer_launcher.core.config as cfg

        base = tmp_path / "base"
        base.mkdir()
        monkeypatch.setattr(cfg, "project_root", lambda: base)
        assert cfg.find_project_root() == base.parent


class TestConfigRootResolution:
    """配置根 = 用户级持久目录 %LOCALAPPDATA%/ninfer-launcher。

    回归「另存预设落到会被 build.ps1 -Clean 清空的 dist/」的架构缺陷：
    用户数据（预设 + settings.json）必须与程序安装/构建位置解耦，落在用户级目录，
    重新打包/重装/多机分发都不丢，且开发模式与打包版行为一致。
    """

    def test_root_is_user_persistent_dir(self, tmp_path, monkeypatch):
        # LOCALAPPDATA 存在 → root = %LOCALAPPDATA%/ninfer-launcher，且已 mkdir
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        root = resolve_config_root()
        assert root == tmp_path / "ninfer-launcher"
        assert root.is_dir()

    def test_root_is_not_program_dir(self, tmp_path, monkeypatch):
        # 即使程序目录可写，也不再优先落程序目录（新策略核心）
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        root = resolve_config_root()
        assert root != project_root()

    def test_falls_back_to_home_when_localappdata_missing(self, tmp_path, monkeypatch):
        # LOCALAPPDATA 缺失 → 回落到用户主目录
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        root = resolve_config_root()
        assert root == tmp_path / "ninfer-launcher"
        assert root.is_dir()