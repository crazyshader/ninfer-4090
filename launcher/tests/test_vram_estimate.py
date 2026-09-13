"""core/vram_estimate.py 单测：标定常量、仿射需求模型、kv_dtype 分档、mtp 乘数、
权重字节覆盖、config_from_values 的归一化（含「永不抛异常」契约）。

不需要真实 GPU。所有期望值都用模块自身的常量表达式（int(16.95*GIB) 等）推导，
标定常量日后调整时测试只需随附一处同步，不会因手抄字面量漂移。
"""

from ninfer_launcher.core.vram_estimate import (
    DEFAULT_MAX_CONTEXT,
    DEFAULT_KV_DTYPE,
    DEFAULT_WEIGHT_BYTES,
    FIXED_RUNTIME_BYTES,
    GIB,
    MIB,
    SAFETY_FLOOR_BYTES,
    SAFETY_MIN_BYTES,
    SAFETY_MAX_BYTES,
    DEFAULT_SAFETY_BYTES,
    VramConfig,
    clamp_safety_bytes,
    config_from_values,
    estimate_requirement,
)
from ninfer_launcher.params.spec import Bool3


def _cfg(**overrides) -> VramConfig:
    base = dict(max_context=163840, kv_dtype="rk4v4-e8", spec="mtp", vision=True)
    base.update(overrides)
    return VramConfig(**base)


class TestCalibrationConstants:
    def test_units(self):
        assert MIB == 1024 ** 2
        assert GIB == 1024 ** 3

    def test_weight_default_within_16_17_gib(self):
        assert 16 * GIB < DEFAULT_WEIGHT_BYTES < 17 * GIB

    def test_fixed_runtime_within_1_1_13_gib(self):
        assert 1.0 * GIB < FIXED_RUNTIME_BYTES < 1.3 * GIB

    def test_safety_floor_is_200_mib(self):
        assert SAFETY_FLOOR_BYTES == 200 * MIB


class TestEstimateRequirement:
    def test_default_config_affine_decomposition(self):
        """需求 = 权重 + 固定 + max_context×每token KV(mtp×1.06) + 固定安全垫(200 MiB)。"""
        req = estimate_requirement(_cfg())
        weight = int(16.95 * GIB)
        kv = int(163840 * 17408 * 1.06)  # 17 KiB/token 的 rk4v4-e8 档
        assert req.weight_bytes == weight
        assert req.fixed_runtime_bytes == FIXED_RUNTIME_BYTES
        assert req.kv_bytes == kv
        assert req.safety_bytes == SAFETY_FLOOR_BYTES
        assert req.total_bytes == weight + FIXED_RUNTIME_BYTES + kv + SAFETY_FLOOR_BYTES

    def test_kv_grows_linearly_with_context(self):
        small = estimate_requirement(_cfg(max_context=32768, spec="none"))
        big = estimate_requirement(_cfg(max_context=65536, spec="none"))
        assert big.kv_bytes == small.kv_bytes * 2

    def test_mtp_multiplier_on_kv_term(self):
        none = estimate_requirement(_cfg(spec="none"))
        mtp = estimate_requirement(_cfg(spec="mtp"))
        # int() 截断允许 ±2 字节漂移
        assert none.kv_bytes * 1.06 - 2 <= mtp.kv_bytes <= none.kv_bytes * 1.06 + 2

    def test_kv_dtype_tiers_ordering(self):
        tiers = {
            name: estimate_requirement(_cfg(kv_dtype=name, spec="none")).kv_bytes
            for name in ("bf16", "int8", "rk8v4", "rk4v4", "rk4v4-e8", "rk2v4-e8")
        }
        assert tiers["bf16"] > tiers["int8"] > tiers["rk8v4"]
        assert tiers["rk4v4"] == tiers["rk4v4-e8"]
        assert tiers["rk4v4"] > tiers["rk2v4-e8"]

    def test_kv_scale_matches_appendix_a(self):
        """200K 上下文 rk4v4-e8+mtp 的 KV 应落在实测区间（2.83GiB@163840 线性外推）。"""
        kv = estimate_requirement(_cfg(max_context=204800, spec="mtp")).kv_bytes
        assert 3.4 * GIB < kv < 3.8 * GIB

    def test_safety_is_fixed_at_small_context(self):
        req = estimate_requirement(_cfg(max_context=1024, weight_bytes=2 * GIB))
        assert req.safety_bytes == SAFETY_FLOOR_BYTES

    def test_safety_is_fixed_at_large_context(self):
        """安全垫不随规模膨胀：大上下文下仍恒为 200 MiB（不再走 base×5%）。"""
        req = estimate_requirement(_cfg(max_context=262144))
        assert req.safety_bytes == SAFETY_FLOOR_BYTES

    def test_weight_bytes_override(self):
        assert estimate_requirement(_cfg(weight_bytes=5 * GIB)).weight_bytes == 5 * GIB

    def test_weight_zero_falls_back_to_default(self):
        assert estimate_requirement(_cfg(weight_bytes=0)).weight_bytes == DEFAULT_WEIGHT_BYTES
        assert estimate_requirement(_cfg(weight_bytes=None)).weight_bytes == DEFAULT_WEIGHT_BYTES

    def test_vision_off_keeps_estimate_conservative(self):
        """标定常量在 vision 开启态测得：关闭 vision 时估算不变（偏保守方向，
        只可能更严格，不会误放行）。"""
        assert estimate_requirement(_cfg(vision=True)) == estimate_requirement(_cfg(vision=False))

    def test_nonpositive_context_is_safe(self):
        assert estimate_requirement(_cfg(max_context=0)).kv_bytes == 0
        assert estimate_requirement(_cfg(max_context=-5)).kv_bytes == 0

    def test_unknown_kv_dtype_uses_most_conservative_tier(self):
        unknown = estimate_requirement(_cfg(kv_dtype="future-dtype", spec="none"))
        bf16 = estimate_requirement(_cfg(kv_dtype="bf16", spec="none"))
        assert unknown.kv_bytes == bf16.kv_bytes


class TestSafetyBytes:
    """用户可调安全垫：常量范围、clamp_safety_bytes 归一、estimate 用动态值。"""

    def test_range_constants(self):
        assert SAFETY_MIN_BYTES == 200 * MIB
        assert SAFETY_MAX_BYTES == 3 * GIB
        assert DEFAULT_SAFETY_BYTES == SAFETY_MIN_BYTES
        assert SAFETY_FLOOR_BYTES == SAFETY_MIN_BYTES  # 向后兼容别名

    def test_clamp_within_range(self):
        assert clamp_safety_bytes(1 * GIB) == 1 * GIB
        assert clamp_safety_bytes(SAFETY_MIN_BYTES) == SAFETY_MIN_BYTES
        assert clamp_safety_bytes(SAFETY_MAX_BYTES) == SAFETY_MAX_BYTES

    def test_clamp_below_min_and_above_max(self):
        assert clamp_safety_bytes(50 * MIB) == SAFETY_MIN_BYTES
        assert clamp_safety_bytes(10 * GIB) == SAFETY_MAX_BYTES
        assert clamp_safety_bytes(-1) == SAFETY_MIN_BYTES

    def test_clamp_bad_input_falls_back_to_default(self):
        assert clamp_safety_bytes(None) == DEFAULT_SAFETY_BYTES
        assert clamp_safety_bytes("garbage") == DEFAULT_SAFETY_BYTES

    def test_estimate_uses_config_safety(self):
        """config.safety_bytes 直接进 requirement.safety_bytes（在范围内原样）。"""
        req = estimate_requirement(_cfg(safety_bytes=1 * GIB))
        assert req.safety_bytes == 1 * GIB

    def test_estimate_clamps_out_of_range_safety(self):
        assert estimate_requirement(_cfg(safety_bytes=10 * GIB)).safety_bytes == SAFETY_MAX_BYTES
        assert estimate_requirement(_cfg(safety_bytes=1 * MIB)).safety_bytes == SAFETY_MIN_BYTES

    def test_estimate_none_safety_uses_default(self):
        assert estimate_requirement(_cfg(safety_bytes=None)).safety_bytes == DEFAULT_SAFETY_BYTES

    def test_larger_safety_raises_total(self):
        small = estimate_requirement(_cfg(safety_bytes=SAFETY_MIN_BYTES))
        large = estimate_requirement(_cfg(safety_bytes=SAFETY_MAX_BYTES))
        assert large.total_bytes - small.total_bytes == SAFETY_MAX_BYTES - SAFETY_MIN_BYTES


class TestConfigFromValues:
    def test_full_normalization(self):
        cfg = config_from_values(
            {
                "max_context": 32768,
                "kv_dtype": " RK4V4-E8 ",
                "spec": "MTP",
                "vision": Bool3.ON,
                "port": 8080,  # 无关键必须被忽略而不报错
            },
            weight_bytes=9 * GIB,
        )
        assert cfg.max_context == 32768
        assert cfg.kv_dtype == "rk4v4-e8"
        assert cfg.spec == "mtp"
        assert cfg.vision is True
        assert cfg.weight_bytes == 9 * GIB

    def test_vision_tristate_mapping(self):
        assert config_from_values({"vision": Bool3.ON}).vision is True
        assert config_from_values({"vision": Bool3.OFF}).vision is False
        assert config_from_values({"vision": Bool3.UNSET}).vision is False
        # 预设 JSON 的字符串形式（Bool3.to_config 的逆）
        assert config_from_values({"vision": "on"}).vision is True
        assert config_from_values({"vision": "off"}).vision is False

    def test_spec_normalization(self):
        assert config_from_values({"spec": "mtp"}).spec == "mtp"
        assert config_from_values({"spec": "none"}).spec == "none"
        assert config_from_values({"spec": "weird"}).spec == "none"
        assert config_from_values({"spec": ""}).spec == "none"
        # 键缺失按注册表默认（ParamsTab store 恒有该键，这里是纯防御兜底）
        assert config_from_values({}).spec == "mtp"

    def test_empty_values_never_raise(self):
        cfg = config_from_values({})
        assert cfg.max_context == DEFAULT_MAX_CONTEXT
        assert cfg.kv_dtype == DEFAULT_KV_DTYPE
        assert cfg.vision is False
        assert cfg.weight_bytes is None

    def test_bad_values_fall_back(self):
        cfg = config_from_values({"max_context": "garbage", "kv_dtype": ""})
        assert cfg.max_context == DEFAULT_MAX_CONTEXT
        assert cfg.kv_dtype == DEFAULT_KV_DTYPE
        cfg2 = config_from_values({"max_context": 0})
        assert cfg2.max_context == DEFAULT_MAX_CONTEXT
        cfg3 = config_from_values({"max_context": -100, "vision": 12345})  # 非法三态不抛异常
        assert cfg3.max_context == DEFAULT_MAX_CONTEXT
        assert cfg3.vision is False

    def test_safety_bytes_passthrough(self):
        """safety_bytes 参数透传进 VramConfig（估算侧再夹紧）；缺省为 None。"""
        assert config_from_values({}).safety_bytes is None
        cfg = config_from_values({}, safety_bytes=1 * GIB)
        assert cfg.safety_bytes == 1 * GIB
        # 估算用到时按范围夹紧
        assert estimate_requirement(cfg).safety_bytes == 1 * GIB
