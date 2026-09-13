"""core/weight_cache.py 单测：日志行解析（load_progress 的 100% 行 / weight H2D
摘要行）、未命中行返回 None、缓存更新的幂等与落盘判定。

纯函数，不需要真实 GPU、不需要真实配置目录。日志行格式对齐 C++ 侧
src/product/load_progress/load_progress.cpp 的 format_line 排版：
    load weights                     100.00%   16.95 GiB /   16.95 GiB 12.345 s
"""

from ninfer_launcher.core.weight_cache import (
    parse_weight_bytes_from_log,
    update_weight_cache,
)

GIB = 1024 ** 3
MIB = 1024 ** 2


def _line(done: str, unit: str, total: str) -> str:
    """拼出与 C++ format_line 同版的 load_progress 行。"""
    return f"load weights                     100.00% {done} {unit} / {total} {unit} 12.345 s"


class TestParseWeightBytes:
    def test_gib_done_line(self):
        assert parse_weight_bytes_from_log(_line("16.95", "GiB", "16.95 GiB")) == int(16.95 * GIB)

    def test_mib_done_line(self):
        assert parse_weight_bytes_from_log(_line("293.62", "MiB", "293.62 MiB")) == int(293.62 * MIB)

    def test_plain_bytes_line(self):
        assert parse_weight_bytes_from_log("load weights                     100.00% 12345 B / 12345 B 0.001 s") == 12345

    def test_weight_h2d_summary_line(self):
        """兼容文档中的「weight H2D <大小>」摘要格式。"""
        assert parse_weight_bytes_from_log("weight H2D 16.95 GiB") == int(16.95 * GIB)

    def test_h2d_case_insensitive(self):
        assert parse_weight_bytes_from_log("Weight h2d 16.95 GiB") == int(16.95 * GIB)

    def test_non_100_percent_progress_line_ignored(self):
        """未达 100% 的进度行不是权重总量，不解析。"""
        line = "load weights                      42.13%    7.13 GiB /   16.95 GiB  1.234 s"
        assert parse_weight_bytes_from_log(line) is None

    def test_other_log_lines_ignored(self):
        assert parse_weight_bytes_from_log("[2026-09-13] serve ready on :8080") is None
        assert parse_weight_bytes_from_log("") is None
        assert parse_weight_bytes_from_log(None) is None

    def test_garbage_numbers_ignored(self):
        # 数字后缺单位 → 不匹配（12 GiB 这种带单位的行是合法进度行，不应在此列）
        assert parse_weight_bytes_from_log("load weights                     100.00%  12") is None
        # 数字不是数字 → 无命中
        assert parse_weight_bytes_from_log("load weights                     100.00%  abc GiB") is None


class TestUpdateWeightCache:
    def test_new_entry_changes_cache(self):
        cache = {}
        assert update_weight_cache(cache, r"E:\m\a.ninfer", 5 * GIB) is True
        assert cache == {r"E:\m\a.ninfer": 5 * GIB}

    def test_same_value_is_noop(self):
        cache = {"a.ninfer": 5 * GIB}
        assert update_weight_cache(cache, "a.ninfer", 5 * GIB) is False

    def test_updated_value_changes_cache(self):
        cache = {"a.ninfer": 5 * GIB}
        assert update_weight_cache(cache, "a.ninfer", 6 * GIB) is True
        assert cache["a.ninfer"] == 6 * GIB

    def test_empty_path_never_written(self):
        cache = {}
        assert update_weight_cache(cache, "", 5 * GIB) is False
        assert cache == {}

    def test_nonpositive_bytes_never_written(self):
        cache = {}
        assert update_weight_cache(cache, "a.ninfer", 0) is False
        assert update_weight_cache(cache, "a.ninfer", -3) is False
        assert cache == {}

    def test_none_bytes_never_written(self):
        cache = {}
        assert update_weight_cache(cache, "a.ninfer", None) is False
        assert cache == {}


class TestSettingsRoundTrip:
    """weight_bytes_cache 随 settings.json 持久化（前后兼容：老文件无该字段）。"""

    def test_round_trip(self, tmp_path):
        from ninfer_launcher.core.config import Settings, load_settings, save_settings

        s = Settings(weight_bytes_cache={"E:\\models\\a.ninfer": 12345678})
        save_settings(tmp_path, s)
        reloaded = load_settings(tmp_path)
        assert reloaded.weight_bytes_cache == {"E:\\models\\a.ninfer": 12345678}

    def test_legacy_settings_without_field_loads_empty(self, tmp_path):
        import json

        legacy = {
            "last_preset": "",
            "window_width": 1100,
            "window_height": 750,
            "model_dir": "",
            "exe_path": "",
            "params": {},
            "theme": "dark",
        }
        (tmp_path / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")
        from ninfer_launcher.core.config import load_settings

        assert load_settings(tmp_path).weight_bytes_cache == {}

    def test_corrupted_cache_field_normalized(self, tmp_path):
        import json

        broken = {
            "last_preset": "",
            "window_width": 1100,
            "window_height": 750,
            "model_dir": "",
            "exe_path": "",
            "params": {},
            "theme": "dark",
            "weight_bytes_cache": {"a.ninfer": 42, "b.ninfer": "bad", "c.ninfer": -1, "d.ninfer": True},
        }
        (tmp_path / "settings.json").write_text(json.dumps(broken), encoding="utf-8")
        from ninfer_launcher.core.config import load_settings

        # 只保留「路径→正整数」的合法条目
        assert load_settings(tmp_path).weight_bytes_cache == {"a.ninfer": 42}
