"""core/vram_preflight.py 单测：三态判定、候选过滤（排除自身 / 受保护）、
「退出哪些进程补齐缺口」的最小集合、消息文案、字节格式化。

全部输入是纯数据（GpuSnapshot / GpuProcess），零系统接触面。
"""

from ninfer_launcher.core.gpu_processes import GpuProcess
from ninfer_launcher.core.monitor import GpuSnapshot
from ninfer_launcher.core.vram_estimate import GIB, VramConfig, estimate_requirement
from ninfer_launcher.core.vram_preflight import (
    PreflightStatus,
    evaluate,
    format_vram_bytes,
    processes_to_close,
)


def _gpu(free_gib: int, total_gib: int = 24) -> GpuSnapshot:
    total = int(total_gib * GIB)
    used = total - int(free_gib * GIB)
    return GpuSnapshot(
        index=0,
        name="NVIDIA GeForce RTX 4090",
        mem_used_bytes=used,
        mem_total_bytes=total,
    )


def _small_cfg(**overrides) -> VramConfig:
    """小需求配置：权重 8 GiB + 1K 上下文 → 总需求约 10.2 GiB，好造「够 / 不够」两态。"""
    base = dict(max_context=1024, kv_dtype="rk4v4-e8", spec="mtp", vision=True, weight_bytes=8 * GIB)
    base.update(overrides)
    return VramConfig(**base)


def _proc(pid: int, name: str, used, **flags) -> GpuProcess:
    return GpuProcess(pid=pid, name=name, used_bytes=used, **flags)


class TestOkVerdict:
    def test_ok_when_free_covers_requirement(self):
        verdict = evaluate(_small_cfg(), _gpu(free_gib=16), ())
        assert verdict.status is PreflightStatus.OK
        assert verdict.can_start is True
        assert verdict.shortfall_bytes == 0
        assert verdict.free_bytes == 16 * GIB
        assert verdict.total_bytes == 24 * GIB
        assert "显存充足" in verdict.messages[0]

    def test_boundary_free_equals_total_is_ok(self):
        cfg = _small_cfg()
        total = 24 * GIB
        used = total - estimate_requirement(cfg).total_bytes
        gpu = GpuSnapshot(index=0, mem_used_bytes=used, mem_total_bytes=total)
        assert evaluate(cfg, gpu, ()).status is PreflightStatus.OK


class TestInsufficientVerdict:
    def test_shortfall_and_filtered_candidates(self):
        processes = (
            _proc(1, "chrome.exe", 6 * GIB),
            _proc(2, "Code.exe", 4 * GIB),
            _proc(3, "dwm.exe", 1 * GIB, is_protected=True),
            _proc(4, "ninfer-serve.exe", 8 * GIB, is_self=True),
            _proc(5, "mystery.exe", None),
        )
        verdict = evaluate(_small_cfg(), _gpu(free_gib=4), processes)
        assert verdict.status is PreflightStatus.INSUFFICIENT
        assert verdict.can_start is True  # 预检不再硬门控：不足也放行，仅面板警告
        assert verdict.shortfall_bytes > 0
        # 自身 / 受保护 / 占用未知的进程不进候选清单
        assert [p.name for p in verdict.candidates] == ["chrome.exe", "Code.exe"]
        # 候选降序（6 GiB 在前）
        assert verdict.candidates[0].used_bytes >= verdict.candidates[1].used_bytes
        joined = " ".join(verdict.messages)
        assert "目标需要" in joined and "还差" in joined
        # chrome(6G) + Code(4G) = 10G 覆盖 ~6.2G 缺口 → 「可补齐」文案
        assert "即可满足" in joined

    def test_all_closed_still_short_lists_config_tips(self):
        cfg = _small_cfg(vision=True)
        verdict = evaluate(cfg, _gpu(free_gib=0), (_proc(1, "chrome.exe", 2 * GIB),))
        assert verdict.status is PreflightStatus.INSUFFICIENT
        joined = " ".join(verdict.messages)
        assert "仍差" in joined
        assert "max_context" in joined
        assert "rk2v4-e8" in joined
        assert "视觉" in joined  # vision=True → 附带「关闭视觉」建议

    def test_no_vision_config_omits_vision_tip(self):
        cfg = _small_cfg(vision=False)
        verdict = evaluate(cfg, _gpu(free_gib=0), (_proc(1, "chrome.exe", 2 * GIB),))
        joined = " ".join(verdict.messages)
        assert "仍差" in joined
        assert "视觉" not in joined

    def test_no_candidates_at_all(self):
        verdict = evaluate(_small_cfg(), _gpu(free_gib=4), ())
        assert verdict.status is PreflightStatus.INSUFFICIENT
        assert verdict.candidates == ()
        joined = " ".join(verdict.messages)
        assert "未发现可退出的进程" in joined

    def test_only_protected_and_self_present(self):
        """只有系统 / 自身进程时，候选清单为空，但仍给出「未发现可退出」指引。"""
        verdict = evaluate(
            _small_cfg(),
            _gpu(free_gib=4),
            (_proc(1, "dwm.exe", 1 * GIB, is_protected=True), _proc(2, "ninfer-serve.exe", 8 * GIB, is_self=True)),
        )
        assert verdict.status is PreflightStatus.INSUFFICIENT
        assert verdict.candidates == ()


class TestUnavailableVerdict:
    def test_gpu_none(self):
        verdict = evaluate(_small_cfg(), None, ())
        assert verdict.status is PreflightStatus.UNAVAILABLE
        assert verdict.can_start is True  # 降级不阻断
        assert verdict.free_bytes is None
        assert "风险自负" in verdict.messages[0]

    def test_unreadable_memory(self):
        gpu = GpuSnapshot(index=0, mem_used_bytes=None, mem_total_bytes=24 * GIB)
        assert evaluate(_small_cfg(), gpu, ()).status is PreflightStatus.UNAVAILABLE

    def test_unreadable_total(self):
        gpu = GpuSnapshot(index=0, mem_used_bytes=8 * GIB, mem_total_bytes=None)
        assert evaluate(_small_cfg(), gpu, ()).status is PreflightStatus.UNAVAILABLE


class TestRunningVerdict:
    """服务运行中：预检不做「冷启动能否装下」的减法，返回 RUNNING 中性态、不误报不足。"""

    def test_running_returns_running_not_insufficient(self):
        """服务在跑 + 空余极小（因本服务已占满）→ RUNNING，而非 INSUFFICIENT。"""
        # 复刻用户场景：模型已加载，空余仅 1.3 GiB ≪ 需求
        verdict = evaluate(_small_cfg(), _gpu(free_gib=1), (), server_running=True)
        assert verdict.status is PreflightStatus.RUNNING
        assert verdict.can_start is True
        assert verdict.shortfall_bytes == 0  # 不做减法，无缺口
        assert verdict.candidates == ()      # 不列可退出进程
        assert "服务运行中" in verdict.messages[0]

    def test_running_reports_current_usage(self):
        """RUNNING 仍带上当前显存现状（free / total），供面板显示已用量。"""
        verdict = evaluate(_small_cfg(), _gpu(free_gib=1), (), server_running=True)
        assert verdict.free_bytes == 1 * GIB
        assert verdict.total_bytes == 24 * GIB

    def test_running_takes_priority_over_unavailable(self):
        """服务在跑就是在跑：读不到显存也返回 RUNNING（优先级高于 UNAVAILABLE）。"""
        verdict = evaluate(_small_cfg(), None, (), server_running=True)
        assert verdict.status is PreflightStatus.RUNNING
        assert verdict.free_bytes is None
        assert verdict.total_bytes is None

    def test_not_running_still_evaluates_normally(self):
        """server_running=False（默认）时行为不变：空余足够 → OK。"""
        verdict = evaluate(_small_cfg(), _gpu(free_gib=16), (), server_running=False)
        assert verdict.status is PreflightStatus.OK


class TestProcessesToClose:
    def test_zero_shortfall(self):
        assert processes_to_close((_proc(1, "a", GIB),), 0) == ((), 0)

    def test_minimal_prefix_covers(self):
        picked, remaining = processes_to_close(
            (_proc(1, "a", 6 * GIB), _proc(2, "b", 4 * GIB), _proc(3, "c", GIB)),
            7 * GIB,
        )
        assert [p.name for p in picked] == ["a", "b"]
        assert remaining == 0

    def test_single_suffices(self):
        picked, remaining = processes_to_close((_proc(1, "a", 6 * GIB), _proc(2, "b", 4 * GIB)), 5 * GIB)
        assert [p.name for p in picked] == ["a"]
        assert remaining == 0

    def test_exhausted_returns_all_with_remaining(self):
        picked, remaining = processes_to_close(
            (_proc(1, "a", GIB), _proc(2, "b", GIB)), 5 * GIB
        )
        assert [p.name for p in picked] == ["a", "b"]
        assert remaining == 3 * GIB

    def test_none_usage_skipped_from_accumulation(self):
        picked, remaining = processes_to_close(
            (_proc(1, "a", None), _proc(2, "b", 2 * GIB)), 1 * GIB
        )
        assert [p.name for p in picked] == ["b"]
        assert remaining == 0

    def test_empty_candidates(self):
        picked, remaining = processes_to_close((), 3 * GIB)
        assert picked == ()
        assert remaining == 3 * GIB


class TestFormatVramBytes:
    def test_none_is_unknown(self):
        assert format_vram_bytes(None) == "未知"

    def test_raw_bytes(self):
        assert format_vram_bytes(0) == "0 B"
        assert format_vram_bytes(512) == "512 B"

    def test_mib(self):
        assert format_vram_bytes(512 * 1024 ** 2) == "512 MiB"

    def test_gib_one_decimal(self):
        assert format_vram_bytes(int(16.95 * GIB)) == "16.9 GiB"

    def test_gib_whole(self):
        assert format_vram_bytes(24 * GIB) == "24 GiB"

    def test_large_value_stays_positive(self):
        assert format_vram_bytes(-5) == "0 B"
