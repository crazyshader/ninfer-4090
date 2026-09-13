"""测试资源监视：纯逻辑解析、两级回落（NVML → nvidia-smi → 不可用）、make_vram_reader。

不需要真实 GPU，也不需要装 pynvml：MonitorService 的 nvml_module / smi_runner /
psutil_module 三个构造参数把「与真实系统打交道」那一段整段替换掉。
"""

from types import SimpleNamespace

import pytest

from ninfer_launcher.core.monitor import (
    FALLBACK_INTERVAL_MS,
    NORMAL_INTERVAL_MS,
    NVIDIA_SMI_FIELDS,
    GpuSnapshot,
    MonitorService,
    MonitorSource,
    SystemSnapshot,
    make_vram_reader,
    nvidia_smi_command,
    parse_nvidia_smi_csv,
)

GIB = 1024**3

#: 单卡 nvidia-smi 输出。20480 MiB / 24576 MiB 刻意与 FakeNvml 的字节数一致，
#: 用来交叉验证两条来源给出同一读数。
SMI_ONE_GPU = b"0, NVIDIA GeForce RTX 4090, 20480, 24576, 2520, 55, 180.00, 450.00\n"


class _MemInfo:
    def __init__(self, used: int, total: int, reserved: int = 0) -> None:
        self.used = used
        self.total = total
        self.reserved = reserved


class FakeNvml:
    """假 NVML 模块，只实现 monitor.py 真正会调的那几个函数。

    handle 直接就是 index，好让每张卡的读数可区分。
    """

    NVML_CLOCK_SM = 1
    NVML_TEMPERATURE_GPU = 0

    #: v2 内存查询的版本常量（仅 support_v2=True 的实例才在 __init__ 里挂上此属性，
    #: 与真实 pynvml「新版才有 nvmlMemory_v2」的行为一致）。
    _V2_SENTINEL = object()

    def __init__(
        self,
        *,
        count: int = 1,
        init_error: Exception | None = None,
        support_v2: bool = False,
        reserved_bytes: int = 0,
        v2_error: Exception | None = None,
    ) -> None:
        self.count = count
        self.init_error = init_error
        #: 置上后 nvmlDeviceGetCount 抛异常，用来模拟 NVML 运行期中途失效
        self.count_error: Exception | None = None
        #: 放进来的字段名读取时抛异常，用来验证单项失败互不牵连
        self.missing: set[str] = set()
        #: 型号名返回 str 而不是 bytes（新版 nvidia-ml-py 的行为）
        self.name_as_str = False
        #: 驱动 / 硬件保留显存字节；v1 计进 used，v2 单列 reserved 且从 used 剔除
        self.reserved_bytes = reserved_bytes
        #: 置上后 v2 查询抛异常，用来验证「v2 失败 → 回退 v1」
        self.v2_error = v2_error
        #: nvmlDeviceGetMemoryInfo 每次拿到的 version 参数（None 表示 v1 调用）
        self.memory_versions: list[object] = []
        self.init_calls = 0
        self.shutdown_calls = 0
        self.count_calls = 0
        #: 新版 NVML 才暴露 nvmlMemory_v2；老版本没有此属性 → 代码回退 v1
        if support_v2:
            self.nvmlMemory_v2 = self._V2_SENTINEL

    def nvmlInit(self) -> None:
        self.init_calls += 1
        if self.init_error is not None:
            raise self.init_error

    def nvmlDeviceGetCount(self) -> int:
        self.count_calls += 1
        if self.count_error is not None:
            raise self.count_error
        return self.count

    def nvmlDeviceGetHandleByIndex(self, index: int) -> int:
        return index

    def nvmlDeviceGetName(self, handle: int):
        self._guard("name")
        return "NVIDIA GeForce RTX 4090" if self.name_as_str else b"NVIDIA GeForce RTX 4090"

    def nvmlDeviceGetMemoryInfo(self, handle: int, version: object = None) -> _MemInfo:
        self._guard("memory")
        self.memory_versions.append(version)
        v1_used = (20 - handle) * GIB  # v1 口径：含驱动 / 硬件保留显存
        if version is not None:  # v2 查询
            if self.v2_error is not None:
                raise self.v2_error
            # v2 把保留显存单列成 reserved 并从 used 中剔除（与任务管理器一致）
            return _MemInfo(
                used=v1_used - self.reserved_bytes,
                total=24 * GIB,
                reserved=self.reserved_bytes,
            )
        return _MemInfo(used=v1_used, total=24 * GIB, reserved=self.reserved_bytes)

    def nvmlDeviceGetClockInfo(self, handle: int, clock_type: int) -> int:
        self._guard("clock")
        assert clock_type == self.NVML_CLOCK_SM
        return 2520

    def nvmlDeviceGetTemperature(self, handle: int, sensor: int) -> int:
        self._guard("temperature")
        assert sensor == self.NVML_TEMPERATURE_GPU
        return 55

    def nvmlDeviceGetPowerUsage(self, handle: int) -> int:
        self._guard("power_draw")
        return 180_000  # 毫瓦

    def nvmlDeviceGetEnforcedPowerLimit(self, handle: int) -> int:
        self._guard("power_limit")
        return 450_000  # 毫瓦

    def nvmlShutdown(self) -> None:
        self.shutdown_calls += 1

    def _guard(self, field: str) -> None:
        if field in self.missing:
            raise RuntimeError(f"NVML {field} 读不到")


class FakeSmi:
    """假 nvidia-smi 执行器：返回预置字节，或抛预置异常。"""

    def __init__(self, output: bytes = SMI_ONE_GPU, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls = 0

    def __call__(self) -> bytes:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.output


class FakePsutil:
    def __init__(self, *, cpu: float = 12.5, total: int = 64 * GIB, available: int = 40 * GIB) -> None:
        self.cpu = cpu
        self.total = total
        self.available = available
        #: 每次 cpu_percent 调用传进来的 interval，用来验证「首次打底」
        self.cpu_calls: list[object] = []

    def cpu_percent(self, interval=None) -> float:
        self.cpu_calls.append(interval)
        return self.cpu

    def virtual_memory(self):
        return SimpleNamespace(total=self.total, available=self.available)


def make_service(**kwargs) -> MonitorService:
    """默认不碰真实系统：三个来源全部显式注入。"""
    kwargs.setdefault("nvml_module", None)
    kwargs.setdefault("psutil_module", None)
    return MonitorService(**kwargs)


# ---------------------------------------------------------------------------
# 纯逻辑
# ---------------------------------------------------------------------------


class TestNvidiaSmiCommand:
    def test_fields_order(self):
        cmd = nvidia_smi_command()
        assert cmd[0] == "nvidia-smi"
        assert cmd[1] == "--query-gpu=" + ",".join(NVIDIA_SMI_FIELDS)
        assert cmd[2] == "--format=csv,noheader,nounits"

    def test_field_count_matches_parser(self):
        """字段顺序契约：解析器按下标取到 tokens[7]，字段表必须正好 8 个。"""
        assert len(NVIDIA_SMI_FIELDS) == 8


class TestParseNvidiaSmiCsv:
    def test_single_gpu(self):
        gpus = parse_nvidia_smi_csv(SMI_ONE_GPU.decode())
        assert len(gpus) == 1
        gpu = gpus[0]
        assert gpu.index == 0
        assert gpu.name == "NVIDIA GeForce RTX 4090"
        assert gpu.mem_used_bytes == 20 * GIB
        assert gpu.mem_total_bytes == 24 * GIB
        assert gpu.sm_clock_mhz == 2520
        assert gpu.temperature_c == 55
        assert gpu.power_draw_w == 180.0
        assert gpu.power_limit_w == 450.0

    def test_multi_gpu(self):
        text = (
            "0, GPU A, 1024, 24576, 2520, 55, 180.00, 450.00\n"
            "1, GPU B, 2048, 24576, 1800, 48, 90.00, 450.00\n"
        )
        gpus = parse_nvidia_smi_csv(text)
        assert [g.index for g in gpus] == [0, 1]
        assert [g.name for g in gpus] == ["GPU A", "GPU B"]

    def test_not_available_fields_become_none(self):
        """[N/A] / Not Supported 记为 None，不是 0，也不中断整行。"""
        text = "0, RTX 4090, [N/A], 24576, [Not Supported], 55, [N/A], 450.00"
        gpu = parse_nvidia_smi_csv(text)[0]
        assert gpu.mem_used_bytes is None
        assert gpu.sm_clock_mhz is None
        assert gpu.power_draw_w is None
        # 同一行里正常的字段照样解出来
        assert gpu.mem_total_bytes == 24 * GIB
        assert gpu.temperature_c == 55
        assert gpu.power_limit_w == 450.0

    def test_zero_is_not_none(self):
        """隐性契约 1：读到的 0 落在数值里，与「读不到」严格区分。"""
        text = "0, RTX 4090, 0, 24576, 0, 0, 0.00, 450.00"
        gpu = parse_nvidia_smi_csv(text)[0]
        assert gpu.mem_used_bytes == 0
        assert gpu.sm_clock_mhz == 0
        assert gpu.temperature_c == 0
        assert gpu.power_draw_w == 0.0
        assert gpu.mem_percent == 0.0

    def test_blank_lines_skipped(self):
        text = "\n  \n0, RTX 4090, 1024, 24576, 2520, 55, 180.00, 450.00\n\n"
        assert len(parse_nvidia_smi_csv(text)) == 1

    def test_short_line_skipped(self):
        assert parse_nvidia_smi_csv("0, RTX 4090, 1024") == ()

    def test_bad_index_line_skipped(self):
        text = (
            "oops, RTX 4090, 1024, 24576, 2520, 55, 180.00, 450.00\n"
            "1, RTX 4090, 1024, 24576, 2520, 55, 180.00, 450.00\n"
        )
        gpus = parse_nvidia_smi_csv(text)
        assert [g.index for g in gpus] == [1]

    def test_empty_text(self):
        assert parse_nvidia_smi_csv("") == ()

    def test_na_name_becomes_none(self):
        gpu = parse_nvidia_smi_csv("0, N/A, 1024, 24576, 2520, 55, 180.00, 450.00")[0]
        assert gpu.name is None
        assert gpu.display_name == "GPU 0"

    def test_float_memory_truncated(self):
        """nvidia-smi 偶尔给小数，整数字段按 int(float()) 收下。"""
        gpu = parse_nvidia_smi_csv("0, RTX 4090, 1024.7, 24576, 2520, 55, 180.00, 450.00")[0]
        assert gpu.mem_used_bytes == 1024 * 1024 * 1024


class TestGpuSnapshot:
    def test_mem_percent(self):
        gpu = GpuSnapshot(index=0, mem_used_bytes=12 * GIB, mem_total_bytes=24 * GIB)
        assert gpu.mem_percent == pytest.approx(50.0)

    def test_mem_percent_none_when_used_missing(self):
        assert GpuSnapshot(index=0, mem_total_bytes=24 * GIB).mem_percent is None

    def test_mem_percent_none_when_total_missing(self):
        assert GpuSnapshot(index=0, mem_used_bytes=1).mem_percent is None

    def test_mem_percent_none_when_total_zero(self):
        """总量为 0 时不能除，也不能谎报 0%。"""
        gpu = GpuSnapshot(index=0, mem_used_bytes=0, mem_total_bytes=0)
        assert gpu.mem_percent is None

    def test_display_name_with_name(self):
        assert GpuSnapshot(index=1, name="RTX 4090").display_name == "GPU 1：RTX 4090"

    def test_display_name_without_name(self):
        assert GpuSnapshot(index=1).display_name == "GPU 1"

    def test_frozen(self):
        gpu = GpuSnapshot(index=0)
        with pytest.raises(Exception):
            gpu.index = 1  # type: ignore[misc]


class TestMonitorSource:
    def test_nvml_not_degraded(self):
        assert MonitorSource.NVML.degraded is False
        assert MonitorSource.NVML.refresh_interval_ms == NORMAL_INTERVAL_MS
        assert MonitorSource.NVML.label == "NVML"

    def test_smi_degraded_and_slower(self):
        assert MonitorSource.NVIDIA_SMI.degraded is True
        assert MonitorSource.NVIDIA_SMI.refresh_interval_ms == FALLBACK_INTERVAL_MS

    def test_unavailable_degraded(self):
        assert MonitorSource.UNAVAILABLE.degraded is True
        assert MonitorSource.UNAVAILABLE.refresh_interval_ms == FALLBACK_INTERVAL_MS


class TestSystemSnapshot:
    def test_mem_percent(self):
        snap = SystemSnapshot(
            source=MonitorSource.NVML, mem_used_bytes=24 * GIB, mem_total_bytes=64 * GIB
        )
        assert snap.mem_percent == pytest.approx(37.5)

    def test_mem_percent_none(self):
        assert SystemSnapshot(source=MonitorSource.NVML).mem_percent is None

    def test_gpu_available_false_when_unavailable(self):
        assert SystemSnapshot(source=MonitorSource.UNAVAILABLE).gpu_available is False

    def test_gpu_available_false_when_no_gpus(self):
        """来源说可用但列表是空的，仍然算不可用。"""
        assert SystemSnapshot(source=MonitorSource.NVML).gpu_available is False

    def test_gpu_by_index(self):
        snap = SystemSnapshot(
            source=MonitorSource.NVML,
            gpus=(GpuSnapshot(index=0), GpuSnapshot(index=1)),
        )
        assert snap.gpu_by_index(1) is not None
        assert snap.gpu_by_index(1).index == 1
        assert snap.gpu_by_index(7) is None


# ---------------------------------------------------------------------------
# 第一级：NVML
# ---------------------------------------------------------------------------


class TestNvmlBackend:
    def test_backend_none_before_first_poll(self):
        svc = make_service(nvml_module=FakeNvml())
        assert svc.backend is None

    def test_readings(self):
        nvml = FakeNvml()
        svc = make_service(nvml_module=nvml)
        snap = svc.poll()
        assert svc.backend is MonitorSource.NVML
        assert snap.source is MonitorSource.NVML
        assert snap.messages == ()
        assert snap.gpu_available is True
        gpu = snap.gpus[0]
        assert gpu.index == 0
        assert gpu.name == "NVIDIA GeForce RTX 4090"
        assert gpu.mem_used_bytes == 20 * GIB
        assert gpu.mem_total_bytes == 24 * GIB
        assert gpu.mem_percent == pytest.approx(20 / 24 * 100)
        assert gpu.sm_clock_mhz == 2520
        assert gpu.temperature_c == 55
        # 毫瓦 → 瓦特
        assert gpu.power_draw_w == 180.0
        assert gpu.power_limit_w == 450.0

    def test_name_as_str(self):
        svc = make_service(nvml_module=FakeNvml())
        svc._nvml_override.name_as_str = True  # type: ignore[union-attr]
        assert svc.poll().gpus[0].name == "NVIDIA GeForce RTX 4090"

    def test_multi_gpu_enumerated_in_order(self):
        svc = make_service(nvml_module=FakeNvml(count=2))
        gpus = svc.poll().gpus
        assert [g.index for g in gpus] == [0, 1]
        assert gpus[0].mem_used_bytes == 20 * GIB
        assert gpus[1].mem_used_bytes == 19 * GIB

    def test_single_field_failure_does_not_taint_others(self):
        """隐性契约：某一项读不到只让那一项为 None。"""
        nvml = FakeNvml()
        nvml.missing = {"temperature", "power_draw"}
        gpu = make_service(nvml_module=nvml).poll().gpus[0]
        assert gpu.temperature_c is None
        assert gpu.power_draw_w is None
        assert gpu.mem_used_bytes == 20 * GIB
        assert gpu.sm_clock_mhz == 2520

    def test_backend_detected_once(self):
        """判定只做一次：第二轮不再重新 init。"""
        nvml = FakeNvml()
        svc = make_service(nvml_module=nvml)
        svc.poll()
        svc.poll()
        svc.poll()
        assert nvml.init_calls == 1

    def test_smi_not_touched(self):
        smi = FakeSmi()
        make_service(nvml_module=FakeNvml(), smi_runner=smi).poll()
        assert smi.calls == 0

    def test_matches_smi_readings(self):
        """两条来源对同一张卡给出同一组数值（单位换算无偏差）。"""
        from_nvml = make_service(nvml_module=FakeNvml()).poll().gpus[0]
        from_smi = make_service(smi_runner=FakeSmi()).poll().gpus[0]
        assert from_nvml == from_smi

    def test_shutdown_releases_nvml(self):
        nvml = FakeNvml()
        svc = make_service(nvml_module=nvml)
        svc.poll()
        svc.shutdown()
        assert nvml.shutdown_calls == 1

    def test_shutdown_before_poll_is_noop(self):
        nvml = FakeNvml()
        make_service(nvml_module=nvml).shutdown()
        assert nvml.shutdown_calls == 0

    def test_shutdown_swallows_errors(self):
        nvml = FakeNvml()
        svc = make_service(nvml_module=nvml)
        svc.poll()
        nvml.nvmlShutdown = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
        svc.shutdown()  # 不抛


# ---------------------------------------------------------------------------
# 第二级：回落到 nvidia-smi
# ---------------------------------------------------------------------------


class TestFallbackToSmi:
    def test_pynvml_not_installed(self):
        smi = FakeSmi()
        snap = make_service(nvml_module=None, smi_runner=smi).poll()
        assert snap.source is MonitorSource.NVIDIA_SMI
        assert snap.source.degraded is True
        assert snap.source.refresh_interval_ms == FALLBACK_INTERVAL_MS
        assert smi.calls == 1
        assert len(snap.gpus) == 1
        assert snap.gpus[0].mem_used_bytes == 20 * GIB
        assert any("未安装 nvidia-ml-py" in m for m in snap.messages)
        assert any("已回落到 nvidia-smi" in m for m in snap.messages)

    def test_nvml_init_error(self):
        nvml = FakeNvml(init_error=RuntimeError("driver/library version mismatch"))
        snap = make_service(nvml_module=nvml, smi_runner=FakeSmi()).poll()
        assert snap.source is MonitorSource.NVIDIA_SMI
        assert any("NVML 初始化失败" in m for m in snap.messages)
        assert any("driver/library version mismatch" in m for m in snap.messages)

    def test_nvml_reports_zero_gpus(self):
        snap = make_service(nvml_module=FakeNvml(count=0), smi_runner=FakeSmi()).poll()
        assert snap.source is MonitorSource.NVIDIA_SMI
        assert any("未检测到任何 GPU" in m for m in snap.messages)

    def test_downgrade_announced_once(self):
        """降级只告知一次，后续轮次消息为空。"""
        svc = make_service(nvml_module=None, smi_runner=FakeSmi())
        first = svc.poll()
        assert first.messages
        assert svc.poll().messages == ()
        assert svc.poll().messages == ()

    def test_nvml_not_retried_after_fallback(self):
        nvml = FakeNvml(init_error=RuntimeError("x"))
        svc = make_service(nvml_module=nvml, smi_runner=FakeSmi())
        svc.poll()
        svc.poll()
        assert nvml.init_calls == 1

    def test_smi_called_every_round(self):
        smi = FakeSmi()
        svc = make_service(nvml_module=None, smi_runner=smi)
        svc.poll()
        svc.poll()
        assert smi.calls == 2

    def test_smi_accepts_str_output(self):
        """注入的执行器返回 str 也能吃下（真实的 run_nvidia_smi 返回 bytes）。"""
        svc = make_service(smi_runner=lambda: SMI_ONE_GPU.decode())
        assert svc.poll().source is MonitorSource.NVIDIA_SMI

    def test_smi_output_undecodable_bytes_do_not_crash(self):
        svc = make_service(smi_runner=lambda: b"\xff\xfe not csv")
        snap = svc.poll()
        assert snap.source is MonitorSource.UNAVAILABLE


# ---------------------------------------------------------------------------
# 两级都不通
# ---------------------------------------------------------------------------


class TestUnavailable:
    def test_both_sources_dead(self):
        smi = FakeSmi(error=FileNotFoundError("nvidia-smi 不存在"))
        snap = make_service(nvml_module=None, smi_runner=smi).poll()
        assert snap.source is MonitorSource.UNAVAILABLE
        # 隐性契约 3：空元组，不是填满 None 的假 GPU
        assert snap.gpus == ()
        assert snap.gpu_available is False
        assert any("nvidia-smi 调用失败" in m for m in snap.messages)
        assert any("GPU 监视数据不可用" in m for m in snap.messages)

    def test_smi_output_unparsable(self):
        snap = make_service(smi_runner=FakeSmi(output=b"garbage\n")).poll()
        assert snap.source is MonitorSource.UNAVAILABLE
        assert snap.gpus == ()

    def test_later_rounds_stay_quiet(self):
        svc = make_service(smi_runner=FakeSmi(error=OSError("no")))
        svc.poll()
        second = svc.poll()
        assert second.source is MonitorSource.UNAVAILABLE
        assert second.gpus == ()
        assert second.messages == ()

    def test_smi_not_called_again_once_unavailable(self):
        smi = FakeSmi(error=OSError("no"))
        svc = make_service(smi_runner=smi)
        svc.poll()
        svc.poll()
        # 判定缓存住了，不会每轮都白起一次进程
        assert smi.calls == 1

    def test_poll_never_raises_when_everything_explodes(self):
        def boom() -> bytes:
            raise RuntimeError("炸了")

        nvml = FakeNvml(init_error=RuntimeError("也炸了"))
        snap = make_service(nvml_module=nvml, smi_runner=boom).poll()
        assert snap.source is MonitorSource.UNAVAILABLE


# ---------------------------------------------------------------------------
# NVML 运行期中途失效：当轮临时回落，不永久放弃
# ---------------------------------------------------------------------------


class TestNvmlRuntimeFailure:
    def test_temporary_fallback_then_recover(self):
        nvml = FakeNvml()
        smi = FakeSmi()
        svc = make_service(nvml_module=nvml, smi_runner=smi)

        assert svc.poll().source is MonitorSource.NVML
        assert smi.calls == 0

        # 第二轮 NVML 中途失效
        nvml.count_error = RuntimeError("GPU is lost")
        second = svc.poll()
        assert any("NVML 读取失败" in m for m in second.messages)
        assert any("下一轮仍会先试 NVML" in m for m in second.messages)
        assert smi.calls == 1
        assert second.gpus[0].mem_used_bytes == 20 * GIB
        # 隐性契约 2：不永久放弃 NVML
        assert svc.backend is MonitorSource.NVML

        # 第三轮 NVML 恢复
        nvml.count_error = None
        third = svc.poll()
        assert third.messages == ()
        assert smi.calls == 1
        assert third.gpus[0].name == "NVIDIA GeForce RTX 4090"

    def test_reports_every_round_while_failing(self):
        """中途失效的告知不受「只说一次」的降级去重影响。"""
        nvml = FakeNvml()
        svc = make_service(nvml_module=nvml, smi_runner=FakeSmi())
        svc.poll()
        nvml.count_error = RuntimeError("GPU is lost")
        assert svc.poll().messages
        assert svc.poll().messages

    def test_smi_also_dead_during_runtime_failure(self):
        nvml = FakeNvml()
        svc = make_service(nvml_module=nvml, smi_runner=FakeSmi(error=OSError("no")))
        svc.poll()
        nvml.count_error = RuntimeError("GPU is lost")
        snap = svc.poll()
        assert snap.gpus == ()
        assert any("nvidia-smi 调用失败" in m for m in snap.messages)


# ---------------------------------------------------------------------------
# CPU / 内存（psutil）
# ---------------------------------------------------------------------------


class TestPsutilCollection:
    def test_readings(self):
        ps = FakePsutil()
        snap = make_service(nvml_module=FakeNvml(), psutil_module=ps).poll()
        assert snap.cpu_percent == pytest.approx(12.5)
        assert snap.mem_total_bytes == 64 * GIB
        assert snap.mem_used_bytes == 24 * GIB
        assert snap.mem_percent == pytest.approx(37.5)

    def test_first_call_primed(self):
        """隐性契约 5：首次必须先打底一次，否则 cpu_percent 语义不成立。"""
        ps = FakePsutil()
        svc = make_service(psutil_module=ps)
        svc.poll()
        assert len(ps.cpu_calls) == 2  # 打底 + 真读
        svc.poll()
        assert len(ps.cpu_calls) == 3  # 后续每轮只读一次
        assert all(interval is None for interval in ps.cpu_calls)

    def test_not_installed(self):
        snap = make_service(psutil_module=None).poll()
        assert snap.cpu_percent is None
        assert snap.mem_used_bytes is None
        assert snap.mem_total_bytes is None

    def test_cpu_failure_isolated_from_memory(self):
        ps = FakePsutil()
        ps.cpu_percent = lambda interval=None: (_ for _ in ()).throw(RuntimeError("x"))  # type: ignore[method-assign]
        snap = make_service(psutil_module=ps).poll()
        assert snap.cpu_percent is None
        assert snap.mem_total_bytes == 64 * GIB

    def test_memory_failure_isolated_from_cpu(self):
        ps = FakePsutil()
        ps.virtual_memory = lambda: (_ for _ in ()).throw(RuntimeError("x"))  # type: ignore[method-assign]
        snap = make_service(psutil_module=ps).poll()
        assert snap.cpu_percent == pytest.approx(12.5)
        assert snap.mem_used_bytes is None

    def test_gpu_unavailable_still_reports_cpu(self):
        """GPU 整体不可用不该拖累 CPU / 内存。"""
        snap = make_service(smi_runner=FakeSmi(error=OSError("no")), psutil_module=FakePsutil()).poll()
        assert snap.source is MonitorSource.UNAVAILABLE
        assert snap.cpu_percent == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# make_vram_reader：与 core/process.py 的 VramReader 对接
# ---------------------------------------------------------------------------


class TestNvmlMemoryV2:
    """NVML 显存查询优先 v2 口径（保留显存不计入 used，与任务管理器 / nvidia-smi 一致）。

    背景（本机实测）：v1 nvmlDeviceGetMemoryInfo 把约 428 MiB 驱动 / 硬件保留显存计进
    used，导致「已用」比任务管理器虚高约 0.4 GiB、空余同量低估，把临界配置误判为不足。
    v2 单列 reserved 并从 used 剔除。缺 v2 支持时回退 v1。
    """

    MIB = 1024 * 1024

    def test_v2_excludes_reserved_from_used(self):
        """支持 v2 时：used 不含保留显存，读数与任务管理器一致。"""
        reserved = 428 * self.MIB
        nvml = FakeNvml(support_v2=True, reserved_bytes=reserved)
        gpu = make_service(nvml_module=nvml).poll().gpus[0]
        # v1 会读到 20 GiB；v2 剔除 428 MiB 保留区
        assert gpu.mem_used_bytes == 20 * GIB - reserved
        assert gpu.mem_total_bytes == 24 * GIB
        # 确实走了 v2（每次查询都带 v2 版本常量）
        assert nvml.memory_versions and all(
            v is FakeNvml._V2_SENTINEL for v in nvml.memory_versions
        )

    def test_v1_fallback_when_no_v2_attr(self):
        """老 NVML 无 nvmlMemory_v2 属性 → 回退 v1，used 含保留显存（现状不变）。"""
        nvml = FakeNvml(reserved_bytes=428 * self.MIB)  # support_v2=False
        gpu = make_service(nvml_module=nvml).poll().gpus[0]
        assert gpu.mem_used_bytes == 20 * GIB  # v1 口径，含保留
        # 全部走 v1 调用：每次 version 参数都是 None（used / total 各查一次）
        assert nvml.memory_versions and all(v is None for v in nvml.memory_versions)

    def test_v2_error_falls_back_to_v1(self):
        """v2 查询抛异常（老驱动不认版本号）→ 回退 v1，不崩、不返回 None。"""
        nvml = FakeNvml(
            support_v2=True,
            reserved_bytes=428 * self.MIB,
            v2_error=RuntimeError("Function Not Found"),
        )
        gpu = make_service(nvml_module=nvml).poll().gpus[0]
        assert gpu.mem_used_bytes == 20 * GIB  # 回退到 v1 口径
        assert gpu.mem_total_bytes == 24 * GIB

    def test_v2_frees_up_reserved_in_derived_free(self):
        """预检的空余按 (total-used) 反推：v1 把保留区当占用压低空余，v2 剔除后空余
        多出正好一个 reserved——这正是临界误判被修正的那部分显存。"""
        reserved = 428 * self.MIB
        v1 = make_service(nvml_module=FakeNvml(reserved_bytes=reserved)).poll().gpus[0]
        v2 = make_service(
            nvml_module=FakeNvml(support_v2=True, reserved_bytes=reserved)
        ).poll().gpus[0]
        v1_free = v1.mem_total_bytes - v1.mem_used_bytes
        v2_free = v2.mem_total_bytes - v2.mem_used_bytes
        assert v2_free - v1_free == reserved


class TestNvmlModuleAccessor:
    """MonitorService.nvml_module 访问器：供 gpu_processes 复用同一 NVML 句柄（不二次 nvmlInit）。"""

    def test_none_before_first_poll(self):
        svc = make_service(nvml_module=FakeNvml())
        assert svc.nvml_module is None

    def test_returns_module_after_nvml_poll(self):
        nvml = FakeNvml()
        svc = make_service(nvml_module=nvml)
        svc.poll()
        assert svc.nvml_module is nvml

    def test_none_when_fell_back_to_smi(self):
        svc = make_service(smi_runner=FakeSmi())
        svc.poll()
        assert svc.backend is MonitorSource.NVIDIA_SMI
        assert svc.nvml_module is None

    def test_none_when_unavailable(self):
        svc = make_service(smi_runner=FakeSmi(error=OSError("no")))
        svc.poll()
        assert svc.nvml_module is None


class TestMakeVramReader:
    def test_signature_matches_process_vram_reader(self):
        """隐性契约 4：零参、返回 int | None，可直接传给 ServerProcess。"""
        import inspect

        from ninfer_launcher.core.process import ServerProcess

        reader = make_vram_reader(make_service(nvml_module=FakeNvml()))
        assert inspect.signature(reader).parameters == {}
        # ServerProcess 真的收得下
        proc = ServerProcess(vram_reader=reader)
        assert proc is not None

    def test_reads_nvml(self):
        reader = make_vram_reader(make_service(nvml_module=FakeNvml()))
        assert reader() == 20 * GIB

    def test_reads_smi(self):
        reader = make_vram_reader(make_service(smi_runner=FakeSmi()))
        assert reader() == 20 * GIB

    def test_default_index_is_zero(self):
        svc = make_service(nvml_module=FakeNvml(count=2))
        assert make_vram_reader(svc)() == 20 * GIB

    def test_explicit_index(self):
        svc = make_service(nvml_module=FakeNvml(count=2))
        assert make_vram_reader(svc, index=1)() == 19 * GIB

    def test_missing_index_returns_none(self):
        svc = make_service(nvml_module=FakeNvml(count=1))
        assert make_vram_reader(svc, index=3)() is None

    def test_none_when_gpu_unavailable(self):
        svc = make_service(smi_runner=FakeSmi(error=OSError("no")))
        assert make_vram_reader(svc)() is None

    def test_none_when_memory_unreadable(self):
        """卡在、但显存这一项读不到 → None，不是 0。"""
        nvml = FakeNvml()
        nvml.missing = {"memory"}
        assert make_vram_reader(make_service(nvml_module=nvml))() is None

    def test_each_call_polls(self):
        smi = FakeSmi()
        reader = make_vram_reader(make_service(smi_runner=smi))
        reader()
        reader()
        reader()
        assert smi.calls == 3

    def test_never_raises_even_if_poll_explodes(self):
        svc = make_service(nvml_module=FakeNvml())
        svc.poll = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
        assert make_vram_reader(svc)() is None
