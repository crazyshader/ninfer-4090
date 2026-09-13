"""core/gpu_processes.py 单测：NVML 进程枚举、compute/graphics 合并、
自身 / 受保护标记、psutil 名字查询、各类缺失时的回落、永不抛异常。

不需要真实 GPU：nvml / psutil 模块都注入假件（与 test_monitor.py 的注入纪律一致）。
"""

import sys
from types import SimpleNamespace

import pytest

from ninfer_launcher.core.gpu_processes import (
    GpuProcess,
    PROTECTED_NAMES,
    SELF_NAMES,
    list_gpu_processes,
)

GIB = 1024 ** 3


class FakeProcessNvml:
    """假 NVML 进程接口：每个条目是 SimpleNamespace(pid, usedGpuMemory)。"""

    def __init__(
        self,
        compute=(),
        graphics=(),
        *,
        init_error: Exception | None = None,
        handle_error: Exception | None = None,
        compute_error: Exception | None = None,
        graphics_error: Exception | None = None,
    ) -> None:
        self._compute = list(compute)
        self._graphics = list(graphics)
        self.init_error = init_error
        self.handle_error = handle_error
        self.compute_error = compute_error
        self.graphics_error = graphics_error
        self.init_calls = 0

    def nvmlInit(self) -> None:
        self.init_calls += 1
        if self.init_error is not None:
            raise self.init_error

    def nvmlDeviceGetHandleByIndex(self, index: int) -> int:
        if self.handle_error is not None:
            raise self.handle_error
        return index

    def nvmlDeviceGetComputeRunningProcesses(self, handle):
        if self.compute_error is not None:
            raise self.compute_error
        return self._compute

    def nvmlDeviceGetGraphicsRunningProcesses(self, handle):
        if self.graphics_error is not None:
            raise self.graphics_error
        return self._graphics


def _entry(pid: int, used) -> SimpleNamespace:
    return SimpleNamespace(pid=pid, usedGpuMemory=used)


class FakePsutilProcess:
    def __init__(self, names: dict, exes: dict, failing: set, pid: int) -> None:
        self._names, self._exes, self._failing, self._pid = names, exes, failing, pid

    def name(self) -> str:
        if self._pid in self._failing:
            raise RuntimeError("access denied")
        return self._names.get(self._pid, "")

    def exe(self) -> str:
        if self._pid in self._failing:
            raise RuntimeError("access denied")
        return self._exes.get(self._pid, "")


class FakePsutil:
    """假 psutil：只提供进程名 / exe 名查询。"""

    def __init__(self, names=None, exes=None, failing=()) -> None:
        self.names = names or {}
        self.exes = exes or {}
        self.failing = set(failing)

    def Process(self, pid: int) -> FakePsutilProcess:
        return FakePsutilProcess(self.names, self.exes, self.failing, pid)


class TestEnumeration:
    def test_sorted_desc_none_last(self):
        nvml = FakeProcessNvml(
            compute=[_entry(101, 6 * GIB), _entry(102, 2 * GIB), _entry(103, None)],
        )
        psutil = FakePsutil(names={101: "chrome.exe", 102: "Code.exe", 103: "dwm.exe"})
        procs = list_gpu_processes(nvml_module=nvml, psutil_module=psutil)
        assert [p.pid for p in procs] == [101, 102, 103]
        assert procs[0].name == "chrome.exe"
        assert procs[0].used_bytes == 6 * GIB
        assert procs[2].used_bytes is None
        assert procs[2].is_protected is True  # dwm.exe 是系统关键进程

    def test_self_process_flagged(self):
        nvml = FakeProcessNvml(compute=[_entry(7, 8 * GIB)])
        psutil = FakePsutil(names={7: "ninfer-serve.exe"})
        (proc,) = list_gpu_processes(nvml_module=nvml, psutil_module=psutil)
        assert proc.is_self is True
        assert proc.is_protected is False
        assert "ninfer-serve.exe" in SELF_NAMES

    def test_merge_compute_and_graphics_takes_max(self):
        """同一 PID 在两个接口都出现时合并，占用取较大值。"""
        nvml = FakeProcessNvml(
            compute=[_entry(5, 1 * GIB), _entry(9, None)],
            graphics=[_entry(5, 3 * GIB), _entry(9, 2 * GIB)],
        )
        procs = {p.pid: p for p in list_gpu_processes(nvml_module=nvml, psutil_module=None)}
        assert procs[5].used_bytes == 3 * GIB
        assert procs[9].used_bytes == 2 * GIB  # None 让位给有读数的一方

    def test_equal_usage_tie_broken_by_pid(self):
        nvml = FakeProcessNvml(compute=[_entry(9, 1 * GIB), _entry(3, 1 * GIB)])
        procs = list_gpu_processes(nvml_module=nvml, psutil_module=None)
        assert [p.pid for p in procs] == [3, 9]

    def test_graphics_only_processes_listed(self):
        nvml = FakeProcessNvml(compute=[], graphics=[_entry(2, 2 * GIB)])
        procs = list_gpu_processes(nvml_module=nvml, psutil_module=None)
        assert [p.pid for p in procs] == [2]


class TestNameLookup:
    def test_name_from_psutil(self):
        nvml = FakeProcessNvml(compute=[_entry(11, 1 * GIB)])
        psutil = FakePsutil(names={11: "chrome.exe"})
        (proc,) = list_gpu_processes(nvml_module=nvml, psutil_module=psutil)
        assert proc.name == "chrome.exe"

    def test_exe_basename_when_name_missing(self):
        nvml = FakeProcessNvml(compute=[_entry(21, 1 * GIB)])
        psutil = FakePsutil(exes={21: "C:\\apps\\game.exe"})
        (proc,) = list_gpu_processes(nvml_module=nvml, psutil_module=psutil)
        assert proc.name == "game.exe"

    def test_pid_fallback_when_query_fails(self):
        nvml = FakeProcessNvml(compute=[_entry(12, 2 * GIB)])
        psutil = FakePsutil(failing=(12,))
        (proc,) = list_gpu_processes(nvml_module=nvml, psutil_module=psutil)
        assert proc.name == "PID 12"

    def test_pid_fallback_when_psutil_absent(self, monkeypatch):
        """psutil 未安装（import 失败）：名字降级为 PID <pid>，枚举本身不受影响。"""
        monkeypatch.setitem(sys.modules, "psutil", None)
        nvml = FakeProcessNvml(compute=[_entry(4, 1 * GIB)])
        (proc,) = list_gpu_processes(nvml_module=nvml)
        assert proc.name == "PID 4"


class TestFailureModes:
    def test_no_processes_returns_empty(self):
        nvml = FakeProcessNvml(compute=[], graphics=[])
        assert list_gpu_processes(nvml_module=nvml, psutil_module=None) == ()

    def test_nvml_init_failure_returns_empty(self):
        nvml = FakeProcessNvml(compute=[_entry(1, 1 * GIB)], init_error=RuntimeError("nvml init"))
        assert list_gpu_processes(nvml_module=nvml, psutil_module=None) == ()
        assert nvml.init_calls == 1

    def test_handle_failure_returns_empty(self):
        nvml = FakeProcessNvml(compute=[_entry(1, 1 * GIB)], handle_error=RuntimeError("handle"))
        assert list_gpu_processes(nvml_module=nvml, psutil_module=None) == ()

    def test_one_getter_failing_still_lists_others(self):
        """compute 接口挂掉不牵连 graphics 的枚举（单项失败不炸整体）。"""
        nvml = FakeProcessNvml(
            compute=[_entry(1, 1 * GIB)],
            graphics=[_entry(2, 2 * GIB)],
            compute_error=RuntimeError("compute"),
        )
        procs = list_gpu_processes(nvml_module=nvml, psutil_module=None)
        assert [p.pid for p in procs] == [2]

    def test_bad_entry_fields_skipped(self):
        nvml = FakeProcessNvml(compute=[SimpleNamespace(pid="?"), _entry(3, 1 * GIB)])
        procs = list_gpu_processes(nvml_module=nvml, psutil_module=None)
        assert [p.pid for p in procs] == [3]


class TestGpuProcessDataclass:
    def test_equality_and_repr(self):
        a = GpuProcess(pid=1, name="x", used_bytes=5)
        b = GpuProcess(pid=1, name="x", used_bytes=5)
        c = GpuProcess(pid=1, name="x", used_bytes=6)
        assert a == b
        assert a != c
        assert "pid=1" in repr(a)

    def test_protected_names_cover_system_critical(self):
        assert {"dwm.exe", "csrss.exe", "winlogon.exe"} <= PROTECTED_NAMES
