"""``core/gpu_power.py``：NVAPI 结构体布局、结果类型语义、失败路径（需求 7）。

**不碰真实显卡驱动**（除了显式标 ``realsystem`` 的那两条）。本文件的价值集中在那些
「做错了验证不会变红」的地方：

1. :class:`~core.gpu_power.NVDRS_SETTING` 的 ``sizeof`` 与版本号。字段被人「顺手简化」
   之后 sizeof 会变，NVAPI 会拿一个它不认的版本号去解读那块内存
   （``core/gpu_power.py`` 契约 1、2）。这一条不需要驱动就能测，且是本模块唯一一处
   「布局对不对」的自动化护栏。
2. 顶层三个函数**不抛异常**（契约 7）。它们直接接在界面开关上，一个 ctypes 异常穿到
   Qt 事件循环里就是一次静默失效。
3. :attr:`~core.gpu_power.PowerModeResult.performance_enabled` 在失败时必须为假：
   「读不到」不等于「已开启」，判错方向会让界面显示一个假的「已开启」。
"""

import ctypes
import sys

import pytest

from ninfer_launcher.core import gpu_power
#: ``tests/conftest.py`` 的 autouse ``_block_real_gpu_power`` 把这三个顶层函数换成了
#: 假实现（默认套件一律不碰真实驱动）。**本文件测的就是这三个函数本身**，被替换掉之后
#: 用例会一路绿着什么也没验证，所以整个模块显式要求还原成真实现。
#: 还原不等于会碰驱动：下面各处对 ``gpu_power._Nvapi`` 另外打了桩，真实现里的 NVAPI
#: 调用一个都走不到。
pytestmark = pytest.mark.usefixtures("real_gpu_power_functions")

from ninfer_launcher.core.gpu_power import (
    DISABLE_MODE,
    ENABLE_MODE,
    NVDRS_SETTING,
    PREFERRED_PSTATE_ID,
    PREFERRED_PSTATE_SETTING_NAME,
    GpuPowerError,
    NvapiCallError,
    NvapiUnavailableError,
    PowerMode,
    PowerModeResult,
    apply_power_mode,
    describe_availability,
    read_power_mode,
)


class TestStructLayout:
    """契约 1、2：结构体布局与版本号。数字取自 NVAPI 头文件的字段定义换算。"""

    @pytest.mark.smoke
    def test_sizeof_matches_nvapi_header(self) -> None:
        # 4（version）+ 4096（settingName = NvU16[2048]）+ 4×4（四个 NvU32 字段）
        # + 4100 × 2（两个 union，各取 NVDRS_BINARY_SETTING 的 4 + 4096）= 12320。
        # 这个数字在本机 2026-08-30 与真实驱动交互验证过（NvAPI_DRS_GetSetting 返回 0）。
        assert ctypes.sizeof(NVDRS_SETTING) == 12320

    def test_version_is_computed_not_hardcoded(self) -> None:
        # 契约 1：版本号必须等于 sizeof | (1 << 16)，即随结构体一起变。
        assert gpu_power._make_version(NVDRS_SETTING, 1) == ctypes.sizeof(NVDRS_SETTING) | (
            1 << 16
        )

    def test_version_equals_measured_value(self) -> None:
        assert gpu_power._make_version(NVDRS_SETTING, 1) == 0x00013020

    def test_setting_id_matches_nvapi_header(self) -> None:
        assert PREFERRED_PSTATE_ID == 0x1057EB71

    @pytest.mark.parametrize(
        "field,expected_size",
        [("settingName", 4096), ("predefined", 4100), ("current", 4100)],
    )
    def test_field_sizes(self, field: str, expected_size: int) -> None:
        # 逐个字段钉住大小：只断 sizeof 的话，两个字段一增一减刚好抵消就抓不到。
        assert ctypes.sizeof(dict(NVDRS_SETTING._fields_)[field]) == expected_size


class TestPowerModeValues:
    """档位数值必须与 NVAPI 头文件一致——填错了驱动会接受一个别的档位，不会报错。"""

    @pytest.mark.parametrize(
        "mode,value",
        [
            (PowerMode.ADAPTIVE, 0),
            (PowerMode.PREFER_MAX, 1),
            (PowerMode.DRIVER_CONTROLLED, 2),
            (PowerMode.PREFER_CONSISTENT_PERFORMANCE, 3),
            (PowerMode.PREFER_MIN, 4),
            (PowerMode.OPTIMAL_POWER, 5),
        ],
    )
    def test_enum_value(self, mode: PowerMode, value: int) -> None:
        assert int(mode) == value

    def test_enable_and_disable_modes(self) -> None:
        assert ENABLE_MODE is PowerMode.PREFER_MAX
        # 契约 8：关闭写回出厂默认，不是恢复用户原先的档位。
        assert DISABLE_MODE is PowerMode.OPTIMAL_POWER

    def test_every_mode_has_chinese_label(self) -> None:
        for mode in PowerMode:
            assert mode.label
            assert "未知档位" not in mode.label


class TestPowerModeResult:
    def test_performance_enabled_true_only_when_ok_and_prefer_max(self) -> None:
        assert PowerModeResult(True, ENABLE_MODE, "").performance_enabled is True

    def test_performance_enabled_false_when_disabled(self) -> None:
        assert PowerModeResult(True, DISABLE_MODE, "").performance_enabled is False

    def test_performance_enabled_false_when_failed(self) -> None:
        """失败时必须为假：「读不到」不等于「已开启」。"""
        assert PowerModeResult(False, ENABLE_MODE, "读取失败").performance_enabled is False

    def test_format_lines_returns_message(self) -> None:
        assert PowerModeResult(True, ENABLE_MODE, "已开启").format_lines() == ("已开启",)


class TestErrorTypes:
    def test_unavailable_is_gpu_power_error(self) -> None:
        assert issubclass(NvapiUnavailableError, GpuPowerError)

    def test_call_error_is_gpu_power_error(self) -> None:
        assert issubclass(NvapiCallError, GpuPowerError)

    def test_call_error_message_includes_status_and_detail(self) -> None:
        exc = NvapiCallError("读取电源管理模式", -8, "INCOMPATIBLE_STRUCT_VERSION")
        text = str(exc)
        assert "读取电源管理模式" in text
        assert "-8" in text
        assert "INCOMPATIBLE_STRUCT_VERSION" in text

    def test_call_error_message_without_detail(self) -> None:
        assert "NVAPI 状态码 -1" in str(NvapiCallError("写入", -1))


class TestTopLevelNeverRaises:
    """契约 7：顶层三个函数在任何失败下都返回 ``ok=False`` 而不是抛异常。"""

    @pytest.fixture(autouse=True)
    def _make_nvapi_unavailable(self, monkeypatch):
        def boom():
            raise NvapiUnavailableError("（测试）NVAPI 不可用")

        monkeypatch.setattr(gpu_power, "_Nvapi", boom)

    def test_describe_availability_returns_failure(self) -> None:
        result = gpu_power.describe_availability()
        assert result.ok is False
        assert result.mode is None
        assert "NVAPI 不可用" in result.message

    def test_read_power_mode_returns_failure(self) -> None:
        result = gpu_power.read_power_mode()
        assert result.ok is False
        assert result.performance_enabled is False

    @pytest.mark.parametrize("enable", [True, False])
    def test_apply_power_mode_returns_failure(self, enable: bool) -> None:
        result = gpu_power.apply_power_mode(enable)
        assert result.ok is False
        assert result.mode is None


class TestApplyVerifiesByReadback:
    """契约 3 的护栏：写入返回成功但回读不符时必须报失败，不能报「已开启」。

    这是本模块最重要的一条行为：漏掉 ``SaveSettings`` 的典型表现就是
    「SetSetting 返回 0、界面显示已开启、重启后什么都没变」。
    """

    def _patch_session(self, monkeypatch, readback_value: int):
        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def set_dword(self, setting_id, value, what):
                assert setting_id == PREFERRED_PSTATE_ID

            def get_dword(self, setting_id, what):
                return readback_value, PREFERRED_PSTATE_SETTING_NAME

        class FakeNvapi:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def session(self):
                return FakeSession()

        monkeypatch.setattr(gpu_power, "_Nvapi", FakeNvapi)

    def test_readback_matches_reports_success(self, monkeypatch) -> None:
        self._patch_session(monkeypatch, int(ENABLE_MODE))
        result = gpu_power.apply_power_mode(True)
        assert result.ok is True
        assert result.mode is ENABLE_MODE

    @pytest.mark.smoke
    def test_readback_mismatch_reports_failure(self, monkeypatch) -> None:
        self._patch_session(monkeypatch, int(DISABLE_MODE))
        result = gpu_power.apply_power_mode(True)
        assert result.ok is False
        assert "未生效" in result.message

    def test_disable_message_states_it_writes_factory_default(self, monkeypatch) -> None:
        # 契约 8 的语义必须出现在消息里，否则使用者会以为「关闭」恢复了他原先的档位。
        self._patch_session(monkeypatch, int(DISABLE_MODE))
        result = gpu_power.apply_power_mode(False)
        assert result.ok is True
        assert "出厂默认" in result.message

    def test_unknown_readback_value_reports_failure_with_raw_number(self, monkeypatch) -> None:
        self._patch_session(monkeypatch, 99)
        result = gpu_power.apply_power_mode(True)
        assert result.ok is False
        assert "99" in result.message


class TestReadHandlesUnknownMode:
    def test_unknown_mode_is_ok_with_raw_value(self, monkeypatch) -> None:
        """驱动新增档位时不该让开关整个不可用，只是标不出中文名。"""

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def get_dword(self, setting_id, what):
                return 42, PREFERRED_PSTATE_SETTING_NAME

        class FakeNvapi:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def session(self):
                return FakeSession()

        monkeypatch.setattr(gpu_power, "_Nvapi", FakeNvapi)
        result = gpu_power.read_power_mode()
        assert result.ok is True
        assert result.mode is None
        assert "42" in result.message


class TestNonWindowsIsUnavailable:
    def test_non_win32_raises_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(NvapiUnavailableError, match="Windows"):
            gpu_power._Nvapi()


# ---------------------------------------------------------------------------
# 真实驱动核对（默认不跑，见 pytest.ini 的 realsystem mark）
# ---------------------------------------------------------------------------


@pytest.mark.realsystem
class TestRealDriver:
    """对着本机真实 NVIDIA 驱动核对一次。默认套件不跑（会读写显卡全局设置）。

    ★ :meth:`test_apply_roundtrip_restores_original` 会**真的改动本机驱动设置**，
    但它在 ``finally`` 里写回进入时读到的档位，因此正常结束后没有残留。
    进入时读不到档位就直接 skip，不做「先猜一个值再写回去」这种事。
    """

    def test_read_returns_expected_setting_name(self) -> None:
        result = read_power_mode()
        if not result.ok:
            pytest.skip(f"本机 NVAPI 不可用：{result.message}")
        # 设置名对得上就说明设置 ID、结构体布局、版本号三者全对，
        # 而不只是「调用返回了 0」。
        assert result.setting_name == PREFERRED_PSTATE_SETTING_NAME

    def test_describe_availability_ok(self) -> None:
        result = describe_availability()
        if not result.ok:
            pytest.skip(f"本机 NVAPI 不可用：{result.message}")
        assert result.ok is True

    def test_apply_roundtrip_restores_original(self) -> None:
        before = read_power_mode()
        if not before.ok or before.mode is None:
            pytest.skip(f"读不到当前档位，不做写入往返：{before.message}")
        original = before.mode
        try:
            enabled = apply_power_mode(True)
            assert enabled.ok is True
            assert enabled.mode is ENABLE_MODE
            assert read_power_mode().performance_enabled is True
        finally:
            restored = apply_power_mode(original is ENABLE_MODE)
            assert restored.ok is True
        assert read_power_mode().mode is original
