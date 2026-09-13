"""CLI（ninfer_launcher/cli）测试：输出契约、状态判定、陈旧文件、锁互斥、启动校验、等待期崩溃、停止链。

测试纪律（docs/01-ninfer-launcher-cli.md 第 10 节）：

- 不许真的 spawn ninfer-serve.exe：spawn 全部注入替身（Spawn）；
- 不许碰真实配置根：动作函数直接接收 root=tmp 目录；端到端测试
  （test_main_run_status）monkeypatch resolve_config_root 指向 tmp；
- 不许碰真实 GPU：stop 的显存读数注入假 reader，MonitorService 注入假工厂；
- 唯一例外：test_main_run_status 与 test_cli_package_is_qt_free 之外的
  真实 /health 探测只出现在端到端测试里（只读，无副作用）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ninfer_launcher.cli import main as cli_main
from ninfer_launcher.cli import resolve, runtime
from ninfer_launcher.cli.actions import (
    action_ensure,
    action_start,
    action_status,
    action_stop,
)
from ninfer_launcher.cli.result import CONTRACT_KEYS
from ninfer_launcher.core import config as config_mod
from ninfer_launcher.core.health_probe import HealthResult, HealthState
from ninfer_launcher.core.ports import PortCheck, PortStatus
from ninfer_launcher.core.process_control import VramSettle

FAKE_PID = 987654
OTHER_PID = 987655
DEAD_PID = 424242
PORT = 18080

QUIET = lambda s: None  # noqa: E731


# ---------------------------------------------------------------------------
# 假件（fakes）
# ---------------------------------------------------------------------------

class Probe:
    """按次序返回预设 HealthResult；用完后重复最后一个。"""

    def __init__(self, *results: HealthResult) -> None:
        assert results
        self.results = list(results)
        self.calls = 0

    def __call__(self, host: str, port: int) -> HealthResult:
        self.calls += 1
        index = self.calls - 1
        return self.results[min(index, len(self.results) - 1)]


READY = HealthResult(HealthState.READY, "ready")
LOADING = HealthResult(HealthState.NOT_READY, "loading")
UNREACHABLE = HealthResult(HealthState.NOT_READY, "连接失败: 测试")


class Clock:
    """假时钟：sleep 直接推进，测试不真正等待。"""

    def __init__(self, step: float = 0.5) -> None:
        self.t = 0.0
        self.step = step

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Spawn:
    """Popen 替身：记录调用，把日志内容写进服务日志文件。"""

    def __init__(self, pid: int = FAKE_PID, log_write: bytes = b"") -> None:
        self.pid = pid
        self.log_write = log_write
        self.calls: list[dict] = []

    def __call__(self, exe: str, argv, cwd, out) -> int:
        self.calls.append({"exe": exe, "argv": list(argv), "cwd": cwd})
        if out is not None and self.log_write:
            out.write(self.log_write)
            out.flush()
        return self.pid

    def spawned(self) -> int:
        return len(self.calls)


class FakeMonitor:
    def __init__(self) -> None:
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1


class VramReaderSeq:
    """按次序返回显存读数；用尽后重复最后一个。"""

    def __init__(self, values: list) -> None:
        self.values = list(values)
        self.i = 0

    def __call__(self) -> int | None:
        if not self.values:
            return None
        v = self.values[min(self.i, len(self.values) - 1)]
        self.i += 1
        return v


def alive_terminated(*args):
    """terminated 语义：True = 进程已消失。参数是 pid（int）或 pid 可迭代。"""
    alive = set()
    for item in args:
        if isinstance(item, int):
            alive.add(item)
        else:
            alive.update(item)

    def term(pid) -> bool:
        return pid is None or pid not in alive
    return term


# ---------------------------------------------------------------------------
# 场景构造
# ---------------------------------------------------------------------------

def make_root(tmp_path: Path, *, preset: bool = True, model: bool = True, settings: dict | None = None) -> Path:
    root = tmp_path / "cfg"
    root.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (root / "settings.json").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    if preset:
        m = root / "model.ninfer"
        if model:
            m.write_bytes(b"fake model")
        (root / "presets").mkdir(parents=True, exist_ok=True)
        data = {"name": "P", "model": str(m), "port": PORT}
        (root / "presets" / "P.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return root


def make_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "serve.exe"
    exe.write_bytes(b"MZ")
    return exe


def write_entry(root: Path, *, pid=FAKE_PID, port=PORT, owner="cli", model="M", preset="P", log=None, started=1000.0) -> None:
    runtime.write_pid_entry(
        root,
        runtime.PidEntry(
            schema=runtime.PID_SCHEMA,
            pid=pid,
            port=port,
            exe="C:/fake/serve.exe",
            args=("--port", str(port)),
            model=model,
            preset=preset,
            owner=owner,
            started_at=started,
            log_path=log,
        ),
    )


def assert_contract(result) -> dict:
    """输出契约：单行 JSON、键齐全（缺键 = 契约破坏）。"""
    line = result.to_json()
    data = json.loads(line)
    assert set(data.keys()) == set(CONTRACT_KEYS), "JSON 键集与契约不一致：缺/多键"
    assert data["action"] in ("status", "start", "stop", "ensure")
    assert data["ok"] is result.ok
    return data


# ---------------------------------------------------------------------------
# status：状态判定表（docs 第 2 节 / 7.1 节）
# ---------------------------------------------------------------------------

def test_status_running_external(tmp_path):
    root = make_root(tmp_path, preset=False)
    r = action_status(root=root, probe=Probe(READY), terminated_check=alive_terminated())
    assert r.ok and r.state == "running" and r.health == "ready"
    assert r.owner == "external" and r.pid is None
    assert_contract(r)


def test_status_running_cli_owner(tmp_path):
    root = make_root(tmp_path, preset=False)
    write_entry(root)
    r = action_status(root=root, probe=Probe(READY), terminated_check=alive_terminated(FAKE_PID))
    assert r.state == "running" and r.owner == "cli"
    assert r.pid == FAKE_PID and r.port == PORT and r.model == "M" and r.preset == "P"
    assert r.started_at == 1000.0
    assert_contract(r)


def test_status_starting_loading(tmp_path):
    root = make_root(tmp_path, preset=False)
    write_entry(root)
    r = action_status(root=root, probe=Probe(LOADING), terminated_check=alive_terminated(FAKE_PID))
    assert r.state == "starting" and r.health == "loading"
    assert_contract(r)


def test_status_starting_unreachable_alive(tmp_path):
    root = make_root(tmp_path, preset=False)
    write_entry(root)
    r = action_status(root=root, probe=Probe(UNREACHABLE), terminated_check=alive_terminated(FAKE_PID))
    assert r.state == "starting" and r.health == "unreachable"
    assert_contract(r)


def test_status_stopped_dead_entry(tmp_path):
    root = make_root(tmp_path, preset=False)
    write_entry(root)
    # FAKE_PID 不在存活集合里 → 进程已死
    r = action_status(root=root, probe=Probe(UNREACHABLE), terminated_check=alive_terminated())
    assert r.state == "stopped" and r.pid is None and r.owner is None
    assert_contract(r)


def test_status_stale_pid_file_deleted(tmp_path):
    root = make_root(tmp_path, preset=False)
    write_entry(root)
    assert runtime.pid_path(root).is_file()
    action_status(root=root, probe=Probe(UNREACHABLE), terminated_check=alive_terminated())
    assert not runtime.pid_path(root).exists(), "陈旧 PID 文件必须删除（docs 陷阱 3）"


def test_status_preset_field_reflects_instance_not_argument(tmp_path):
    """契约：preset 字段是「实例」的预设名。没有实例时必须为 null，
    不能把调用方传的 --preset 回显出去（否则调用方会误以为实例用了该预设）；
    --preset 只用于定位端口。"""
    root = make_root(tmp_path)  # 有预设 P，端口 18080；但没有任何实例
    r = action_status(root=root, preset_name="P", probe=Probe(READY), terminated_check=alive_terminated())
    assert r.state == "running" and r.owner == "external"
    assert r.preset is None, "无实例时 preset 必须为 null"
    assert r.port == PORT, "--preset 仍然参与端口定位"
    assert_contract(r)


def test_status_tail_clamped_to_200(tmp_path):
    root = make_root(tmp_path, preset=False)
    log = root / "svc.log"
    log.write_text("".join("line%d" % i + chr(10) for i in range(300)), encoding="utf-8")
    write_entry(root, log=str(log))
    r = action_status(root=root, probe=Probe(UNREACHABLE), terminated_check=alive_terminated(FAKE_PID), tail=500)
    assert r.log_tail is not None and len(r.log_tail) == 200, "tail 上限必须是 200"


def test_status_tail_gbk_log_decodable(tmp_path):
    root = make_root(tmp_path, preset=False)
    log = root / "svc.log"
    # GBK 字节：走 decode_output 的编码链（utf-8 失败 → OEM 代码页），绝不抛异常
    raw = "加载失败：显存不足".encode("gbk") + bytes([10]) + b"OK" + bytes([10])
    log.write_bytes(raw)
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # 守卫：确认测试数据确实走不了 utf-8，确实在验证回退链
    write_entry(root, log=str(log))
    r = action_status(root=root, probe=Probe(UNREACHABLE), terminated_check=alive_terminated(FAKE_PID), tail=10)
    assert r.log_tail is not None
    assert len(r.log_tail) == 2
    assert r.log_tail[-1] == "OK"
    assert r.log_tail[0] != ""


# ---------------------------------------------------------------------------
# runtime：PID 文件 / 锁 / 日志轮转
# ---------------------------------------------------------------------------

def test_pid_file_roundtrip(tmp_path):
    root = make_root(tmp_path, preset=False)
    entry = runtime.PidEntry(
        schema=runtime.PID_SCHEMA, pid=FAKE_PID, port=PORT, exe="C:/x/y.exe",
        args=("--port", str(PORT)), model="M", preset="P", owner="cli",
        started_at=1234.5, log_path="C:/log",
    )
    runtime.write_pid_entry(root, entry)
    raw = runtime.read_pid_entry_raw(root)
    assert raw is not None and raw.pid == FAKE_PID and raw.owner == "cli" and raw.args == ("--port", str(PORT))


def test_pid_file_corrupt_treated_as_stale(tmp_path):
    root = make_root(tmp_path, preset=False)
    p = runtime.pid_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 半个 JSON", encoding="utf-8")
    assert runtime.load_pid_entry(root, terminated_check=lambda pid: True) is None
    assert not p.exists(), "损坏的登记表必须删除"


def test_lock_acquired_and_released(tmp_path):
    root = make_root(tmp_path, preset=False)
    result = runtime.acquire_lock(root, owner_pid=1234)
    assert result.state is runtime.LockState.ACQUIRED
    data = json.loads(runtime.lock_path(root).read_text(encoding="utf-8"))
    assert data["pid"] == 1234
    runtime.release_lock(result)
    assert not runtime.lock_path(root).exists()


def test_lock_stale_reclaimed(tmp_path):
    root = make_root(tmp_path, preset=False)
    lock = runtime.lock_path(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": DEAD_PID, "acquiredAt": 0.0}), encoding="utf-8")
    # DEAD_PID 已死（terminated=True）→ 陈旧锁被夺
    result = runtime.acquire_lock(root, owner_pid=1234, terminated_check=alive_terminated())
    assert result.state is runtime.LockState.ACQUIRED
    assert not runtime.lock_path(root).is_file() or json.loads(runtime.lock_path(root).read_text(encoding="utf-8"))["pid"] == 1234


def test_lock_held_by_live_holder(tmp_path):
    root = make_root(tmp_path, preset=False)
    lock = runtime.lock_path(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": OTHER_PID, "acquiredAt": 0.0}), encoding="utf-8")
    # OTHER_PID 活着 → 不报错，转等待分支
    result = runtime.acquire_lock(root, owner_pid=1234, terminated_check=alive_terminated(OTHER_PID))
    assert result.state is runtime.LockState.HELD_BY_OTHER
    assert result.holder_pid == OTHER_PID
    assert lock.read_text(encoding="utf-8").startswith("{")


def test_rotate_logs_keeps_ten(tmp_path):
    root = make_root(tmp_path, preset=False)
    d = runtime.logs_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    for i in range(12):
        (d / ("serve-20260101-1015%02d.log" % i)).touch()
    runtime.rotate_logs(root)
    remain = sorted(f.name for f in d.iterdir())
    assert len(remain) == 10
    assert remain[0] == "serve-20260101-101502.log"  # 最旧两份被删


def test_read_log_tail_missing_file(tmp_path):
    assert runtime.read_log_tail(tmp_path / "nope.log", 5) == []


def test_read_log_tail_large_file_reads_from_tail(tmp_path):
    """几百 KB 的日志：只读尾部一块就够，结果必须与整读一致（docs 12.4）。"""
    root = make_root(tmp_path, preset=False)
    log = root / "big.log"
    n = 3000
    with open(log, "wb") as fh:
        for i in range(n):
            fh.write(b"line-%04d " % i + b"x" * 90 + b"\n")
    assert log.stat().st_size > 64 * 1024, "测试数据必须大到走尾部块读路径"
    tail = runtime.read_log_tail(log, 20)
    expected = log.read_bytes().decode("utf-8").splitlines()[-20:]
    assert tail == expected
    assert len(tail) == 20
    assert tail[-1] == "line-%04d " % (n - 1) + "x" * 90


def test_read_log_tail_grows_block_until_enough_lines(tmp_path):
    """单行很长（远大于 64 KiB 块）时：块必须加倍向前扩，直到凑够行数。"""
    root = make_root(tmp_path, preset=False)
    log = root / "huge.log"
    n = 30
    with open(log, "wb") as fh:
        for i in range(n):
            fh.write(b"line-%03d " % i + b"Z" * 40000 + b"\n")
    assert log.stat().st_size > runtime.TAIL_BLOCK_SIZE * 2
    tail = runtime.read_log_tail(log, 5)
    assert [line[:8] for line in tail] == ["line-%03d" % i for i in range(n - 5, n)]


def test_read_log_tail_large_file_gbk_decodes_via_chain(tmp_path):
    """几百 KB 的 GBK 日志：按行边界对齐的块仍要走 decode_output 的探测链解码；
    只断言稳定的 ASCII 部分（中文部分依赖机器 OEM 代码页，与既有 GBK 用例同一纪律）。"""
    root = make_root(tmp_path, preset=False)
    log = root / "big_gbk.log"
    n = 6000
    with open(log, "wb") as fh:
        for i in range(n):
            fh.write(("日志行%04d" % i).encode("gbk") + b" " + b"y" * 8 + b"\n")
    assert log.stat().st_size > 64 * 1024
    tail = runtime.read_log_tail(log, 5)
    assert len(tail) == 5
    # ASCII 部分（行号与填充）在任何编码链下都稳定可辨；中文部分的解码结果
    # 依赖机器 OEM 代码页，只断言「不为替换字符满行」这类结构
    assert "5999" in tail[-1] and "yyyy" in tail[-1]
    assert "5995" in tail[0] and "yyyy" in tail[0]


# ---------------------------------------------------------------------------
# start：前置校验（失败时不许 spawn）
# ---------------------------------------------------------------------------

def test_start_no_preset(tmp_path):
    root = make_root(tmp_path, preset=False)
    sp = Spawn()
    r = action_start(root=root, probe=Probe(UNREACHABLE), spawn=sp, clock=Clock().now, sleep=Clock().sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE), progress=QUIET)
    assert r.ok is False and r.error == "no-preset"
    assert sp.spawned() == 0
    assert_contract(r)


def test_start_invalid_params(tmp_path):
    root = make_root(tmp_path, preset=False)
    (root / "presets").mkdir()
    (root / "presets" / "P.json").write_text(
        json.dumps({"name": "P", "model": str(root / "m.ninfer"), "port": PORT, "params": {"kv_dtype": "bogus"}}),
        encoding="utf-8",
    )
    (root / "m.ninfer").write_bytes(b"x")
    exe = make_exe(tmp_path)
    sp = Spawn()
    r = action_start(root=root, preset_name="P", exe_path=str(exe), probe=Probe(UNREACHABLE), spawn=sp,
                     clock=Clock().now, sleep=Clock().sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE), progress=QUIET)
    assert r.ok is False and r.error == "invalid-params"
    assert "bogus" in r.message
    assert sp.spawned() == 0
    assert_contract(r)


def test_start_model_not_found(tmp_path):
    root = make_root(tmp_path, model=False)
    exe = make_exe(tmp_path)
    sp = Spawn()
    r = action_start(root=root, preset_name="P", exe_path=str(exe), probe=Probe(UNREACHABLE), spawn=sp,
                     clock=Clock().now, sleep=Clock().sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE), progress=QUIET)
    assert r.ok is False and r.error == "model-not-found"
    assert sp.spawned() == 0
    assert_contract(r)


def test_start_exe_not_found(tmp_path):
    root = make_root(tmp_path)
    sp = Spawn()
    r = action_start(root=root, preset_name="P", exe_path=str(tmp_path / "no-such.exe"),
                     probe=Probe(UNREACHABLE), spawn=sp, clock=Clock().now, sleep=Clock().sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE), progress=QUIET)
    assert r.ok is False and r.error == "exe-not-found"
    assert sp.spawned() == 0
    assert_contract(r)


def test_start_port_in_use_by_other(tmp_path):
    root = make_root(tmp_path)
    exe = make_exe(tmp_path)
    sp = Spawn()

    def in_use(p, h):
        return PortCheck(p, PortStatus.IN_USE, "端口 %d 已被占用" % p)

    r = action_start(root=root, preset_name="P", exe_path=str(exe), probe=Probe(UNREACHABLE), spawn=sp,
                     clock=Clock().now, sleep=Clock().sleep, check_port_fn=in_use, progress=QUIET)
    assert r.ok is False and r.error == "port-in-use"
    assert sp.spawned() == 0
    assert_contract(r)


# ---------------------------------------------------------------------------
# start / ensure：完整流程
# ---------------------------------------------------------------------------

def test_start_port_mismatch_blocks_second_instance(tmp_path):
    """已登记的存活实例在别的端口：再拉起新实例会把两份权重压上同一张卡，
    必须报 port-mismatch 且不 spawn，让用户显式先 stop（docs 12.3）。"""
    root = make_root(tmp_path)
    exe = make_exe(tmp_path)
    write_entry(root, port=PORT + 1, owner="cli")  # 旧实例：本 CLI 拉起、端口不同
    sp = Spawn()
    r = action_start(root=root, preset_name="P", exe_path=str(exe),
                     probe=Probe(UNREACHABLE), spawn=sp, clock=Clock().now, sleep=Clock().sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE),
                     terminated_check=alive_terminated(FAKE_PID), progress=QUIET)
    assert r.ok is False and r.error == "port-mismatch"
    assert r.owner == "cli" and r.pid == FAKE_PID and r.port == PORT
    assert sp.spawned() == 0, "已有实例在不同端口时必须拦截，不得再拉一个"
    assert runtime.pid_path(root).exists(), "旧实例还在跑，登记表绝不能动"
    assert_contract(r)


def test_start_port_mismatch_blocks_external_owner_instance(tmp_path):
    """归属不明（登记表损坏回落 external）的存活实例在别的端口：同样拦截——
    不该为一个我们不了解的活实例静默再拉一份权重（docs 12.3）。"""
    root = make_root(tmp_path)
    exe = make_exe(tmp_path)
    write_entry(root, port=PORT + 1, owner="external")
    sp = Spawn()
    r = action_start(root=root, preset_name="P", exe_path=str(exe),
                     probe=Probe(UNREACHABLE), spawn=sp, clock=Clock().now, sleep=Clock().sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE),
                     terminated_check=alive_terminated(FAKE_PID), progress=QUIET)
    assert r.ok is False and r.error == "port-mismatch"
    assert sp.spawned() == 0
    assert_contract(r)


def test_start_success(tmp_path):
    root = make_root(tmp_path)
    exe = make_exe(tmp_path)
    clock = Clock()
    sp = Spawn(log_write=b"loading weights" + bytes([10]))
    probe = Probe(UNREACHABLE, UNREACHABLE, READY)
    r = action_start(root=root, preset_name="P", exe_path=str(exe), probe=probe, spawn=sp,
                     clock=clock.now, sleep=clock.sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE),
                     terminated_check=alive_terminated(FAKE_PID), progress=QUIET)
    assert r.ok and r.state == "running" and r.health == "ready"
    assert r.pid == FAKE_PID and r.port == PORT and r.owner == "cli"
    assert r.preset == "P" and r.model == str(root / "model.ninfer")
    assert r.waited_ms is not None and r.waited_ms >= 0
    assert r.log_path and Path(r.log_path).is_file()
    assert r.error is None
    assert_contract(r)
    # 进程登记表
    entry = runtime.load_pid_entry(root, terminated_check=alive_terminated(FAKE_PID))
    assert entry is not None and entry.pid == FAKE_PID and entry.owner == "cli" and entry.port == PORT
    # spawn 细节：模型位置参数在前，端口在内，cwd = exe 目录
    assert sp.spawned() == 1
    call = sp.calls[0]
    assert call["argv"][0] == str(root / "model.ninfer")
    assert "--port" in call["argv"] and str(PORT) in call["argv"]
    assert call["cwd"] == str(exe.parent)


def test_start_idempotent_already_ready(tmp_path):
    root = make_root(tmp_path)
    exe = make_exe(tmp_path)
    sp = Spawn()
    r = action_start(root=root, preset_name="P", exe_path=str(exe), probe=Probe(READY), spawn=sp,
                     clock=Clock().now, sleep=Clock().sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE), progress=QUIET)
    assert r.ok and r.state == "running" and r.owner == "external"
    assert sp.spawned() == 0, "已就绪时不得重复 spawn"
    assert_contract(r)


def test_start_crash_during_wait_returns_immediately(tmp_path):
    root = make_root(tmp_path)
    exe = make_exe(tmp_path)
    clock = Clock(step=0.5)
    state = {"alive": True}

    def term(pid):
        return pid is None or not state["alive"]

    probe = Probe(UNREACHABLE)
    real_probe = probe

    def flaky_probe(host, port):
        result = real_probe(host, port)
        if real_probe.calls >= 3:
            state["alive"] = False  # 第二次轮询后进程崩溃
        return result

    sp = Spawn(log_write=b"OOM: out of memory" + bytes([10]))
    r = action_start(root=root, preset_name="P", exe_path=str(exe), probe=flaky_probe, spawn=sp,
                     timeout=600.0, clock=clock.now, sleep=clock.sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE),
                     terminated_check=term, progress=QUIET)
    assert r.ok is False and r.error == "crashed" and r.state == "stopped"
    assert r.log_tail and any("OOM" in line for line in r.log_tail)
    assert clock.t < 5.0, "进程死了必须立即返回，不能傻等满超时（docs 陷阱 6）"
    assert_contract(r)


def test_start_lock_held_by_live_other_waits_then_times_out(tmp_path):
    root = make_root(tmp_path)
    exe = make_exe(tmp_path)
    lock = runtime.lock_path(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": OTHER_PID, "acquiredAt": 0.0}), encoding="utf-8")
    clock = Clock(step=1.0)
    sp = Spawn()
    r = action_start(root=root, preset_name="P", exe_path=str(exe), timeout=1.0, probe=Probe(LOADING),
                     spawn=sp, clock=clock.now, sleep=clock.sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE),
                     terminated_check=alive_terminated(FAKE_PID, OTHER_PID), progress=QUIET)
    assert r.ok is False and r.error == "lock-timeout" and r.state == "starting"
    assert sp.spawned() == 0, "持锁者活着时本实例不得 spawn"
    assert lock.is_file(), "活持锁者的锁不得被删除"
    assert_contract(r)


def test_start_stale_lock_reclaimed_then_spawns(tmp_path):
    root = make_root(tmp_path)
    exe = make_exe(tmp_path)
    lock = runtime.lock_path(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": DEAD_PID, "acquiredAt": 0.0}), encoding="utf-8")
    clock = Clock()
    sp = Spawn()
    r = action_start(root=root, preset_name="P", exe_path=str(exe), probe=Probe(UNREACHABLE, READY),
                     spawn=sp, clock=clock.now, sleep=clock.sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE),
                     terminated_check=alive_terminated(FAKE_PID), progress=QUIET)
    assert r.ok and r.state == "running", "陈旧锁必须被夺回，而不是报错"
    assert sp.spawned() == 1
    assert not lock.exists(), "释放后锁文件必须删除（try/finally）"
    assert_contract(r)


def test_ensure_fast_path_ready_no_lock(tmp_path):
    root = make_root(tmp_path)
    clock = Clock()
    sp = Spawn()
    r = action_ensure(root=root, probe=Probe(READY), spawn=sp, clock=clock.now, sleep=clock.sleep,
                      check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE),
                      terminated_check=alive_terminated(FAKE_PID), progress=QUIET)
    assert r.ok and r.state == "running" and r.health == "ready"
    # 快速路径没读登记表，无法断言归属：未知就是 null，不能谎称 external（docs 12.2）
    assert r.owner is None, "ensure 快速路径的 owner 必须是 null（不查证不断言）"
    assert sp.spawned() == 0
    assert not runtime.lock_path(root).exists(), "快速路径不得取锁"
    assert clock.t == 0.0, "快速路径不得轮询等待"
    assert_contract(r)


def test_ensure_waits_for_loading_own_instance_without_respawn(tmp_path):
    root = make_root(tmp_path)
    exe = make_exe(tmp_path)
    write_entry(root)  # 本 CLI 之前拉起的实例正在加载
    clock = Clock()
    sp = Spawn()
    r = action_ensure(root=root, preset_name="P", exe_path=str(exe),
                      probe=Probe(LOADING, LOADING, READY), spawn=sp,
                      clock=clock.now, sleep=clock.sleep,
                      check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE),
                      terminated_check=alive_terminated(FAKE_PID), progress=QUIET)
    assert r.ok and r.state == "running" and r.pid == FAKE_PID and r.owner == "cli"
    assert sp.spawned() == 0, "已有实例在加载时不得重复拉起（ensure 与 start 的关键区别）"
    assert_contract(r)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def _stop_common(**over):
    base = dict(
        probe=Probe(UNREACHABLE),
        monitor_factory=lambda: FakeMonitor(),
        make_reader=lambda svc: VramReaderSeq([]),
        progress=QUIET,
    )
    base.update(over)
    return base


def test_stop_idempotent_not_running(tmp_path):
    root = make_root(tmp_path, preset=False)
    mon = FakeMonitor()
    r = action_stop(root=root, probe=Probe(UNREACHABLE),
                    terminated_check=alive_terminated(), monitor_factory=lambda: mon,
                    make_reader=lambda s: VramReaderSeq([]), progress=QUIET)
    assert r.ok is True and r.error == "not-running" and r.state == "stopped"
    assert_contract(r)


def test_stop_external_instance_skipped_without_force(tmp_path):
    root = make_root(tmp_path, preset=False)
    killer_calls = []
    r = action_stop(root=root, probe=Probe(READY),
                    killer=lambda pid: (killer_calls.append(pid) or (True, "x")),
                    progress=QUIET)
    assert r.ok is True and r.error == "not-owned" and r.owner == "external"
    assert killer_calls == []
    assert_contract(r)


def test_stop_external_instance_force_reports_no_pid(tmp_path):
    """外部实例活着但没有可定位的 PID：--force 也停不掉，必须报 ok:false +
    no-pid-to-stop——报成功会让调用方把「没动」误判成「已停」（docs 12.5）。"""
    root = make_root(tmp_path, preset=False)
    r = action_stop(root=root, probe=Probe(READY), force=True,
                    monitor_factory=lambda: FakeMonitor(),
                    make_reader=lambda s: VramReaderSeq([]), progress=QUIET)
    assert r.ok is False and r.error == "no-pid-to-stop"
    assert r.state == "running" and r.owner == "external"
    assert "手动结束" in r.message, "必须明确指引用户手动结束进程"
    assert_contract(r)


def test_stop_owner_gui_skipped_without_force(tmp_path):
    root = make_root(tmp_path, preset=False)
    write_entry(root, owner="gui")
    killer_calls = []
    mon = FakeMonitor()
    r = action_stop(root=root, probe=Probe(UNREACHABLE),
                    terminated_check=alive_terminated(FAKE_PID),
                    killer=lambda pid: (killer_calls.append(pid) or (True, "x")),
                    monitor_factory=lambda: mon, make_reader=lambda s: VramReaderSeq([]), progress=QUIET)
    assert r.ok is True and r.error == "not-owned" and r.owner == "gui"
    assert killer_calls == [], "非 cli 归属的实例（未 --force）绝不得被杀"
    assert runtime.pid_path(root).exists()
    assert_contract(r)


def test_stop_success_with_vram_settle(tmp_path):
    root = make_root(tmp_path, preset=False)
    write_entry(root)
    state = {"alive": True}

    def term(pid):
        return pid is None or not state["alive"]

    def killer(pid):
        state["alive"] = False
        return True, "已终止"

    mon = FakeMonitor()
    reader = VramReaderSeq([2 * 1024 ** 3, 100 * 1024 ** 2])
    clock = Clock(step=0.25)
    r = action_stop(root=root, probe=Probe(UNREACHABLE), terminated_check=term, killer=killer,
                    hard_terminator=lambda pid: (True, "hard"),
                    monitor_factory=lambda: mon, make_reader=lambda svc: reader,
                    sleep=clock.sleep, clock=clock.now, progress=QUIET)
    assert r.ok and r.state == "stopped" and r.error is None
    assert r.settle == "settled"
    assert r.vram_freed_bytes == 2 * 1024 ** 3 - 100 * 1024 ** 2
    assert r.health == "unreachable"
    assert mon.shutdowns == 1, "MonitorService 用完必须 shutdown 释放 NVML"
    assert not runtime.pid_path(root).exists(), "停止成功后 PID 文件必须删除"
    assert_contract(r)


def test_stop_kill_timeout_keeps_pid_file(tmp_path):
    root = make_root(tmp_path, preset=False)
    write_entry(root)
    killer_calls = []
    hard_calls = []
    clock = Clock(step=1.0)

    def killer(pid):
        killer_calls.append(pid)
        return True, "ok"

    def hard(pid):
        hard_calls.append(pid)
        return True, "hard"

    r = action_stop(root=root, probe=Probe(UNREACHABLE),
                    terminated_check=lambda pid: False,  # 永远杀不死
                    killer=killer, hard_terminator=hard,
                    monitor_factory=lambda: FakeMonitor(), make_reader=lambda s: VramReaderSeq([]),
                    sleep=clock.sleep, clock=clock.now, progress=QUIET)
    assert r.ok is False and r.error == "kill-timeout" and r.state == "unknown"
    assert "任务管理器" in r.message, "强杀超时必须明确指引用户手动结束进程"
    assert len(killer_calls) == 1
    assert len(hard_calls) >= 2, "升级链必须真的重发了强杀"
    assert r.settle is None and r.vram_freed_bytes is None
    assert runtime.pid_path(root).exists(), "进程还活着，登记表必须保留"
    assert_contract(r)


def test_stop_degraded_settle_when_no_readings(tmp_path):
    root = make_root(tmp_path, preset=False)
    write_entry(root)
    state = {"alive": True}

    def killer(pid):
        state["alive"] = False
        return True, "ok"

    clock = Clock(step=0.3)
    mon = FakeMonitor()
    r = action_stop(root=root, probe=Probe(UNREACHABLE),
                    terminated_check=lambda pid: pid is None or not state["alive"],
                    killer=killer,
                    monitor_factory=lambda: mon,
                    make_reader=lambda s: VramReaderSeq([]),  # 无任何显存读数 → 降级
                    sleep=clock.sleep, clock=clock.now, progress=QUIET)
    assert r.ok and r.settle == "degraded"
    assert r.vram_freed_bytes is None
    assert_contract(r)


# ---------------------------------------------------------------------------
# 输出契约（端到端）
# ---------------------------------------------------------------------------

def test_contract_all_actions(tmp_path):
    root_preset = make_root(tmp_path)
    root_bare = tmp_path / "bare"
    root_bare.mkdir()
    exe = make_exe(tmp_path)
    clock = Clock()
    results = [
        action_status(root=root_bare, probe=Probe(READY), terminated_check=alive_terminated()),
        action_start(root=root_bare, probe=Probe(UNREACHABLE), spawn=Spawn(), clock=Clock().now, sleep=Clock().sleep,
                     check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE), progress=QUIET),
        action_stop(root=root_bare, probe=Probe(UNREACHABLE),
                    terminated_check=alive_terminated(), monitor_factory=lambda: FakeMonitor(),
                    make_reader=lambda s: VramReaderSeq([]), progress=QUIET),
        action_ensure(root=root_bare, probe=Probe(READY), spawn=Spawn(), clock=Clock().now, sleep=Clock().sleep,
                      check_port_fn=lambda p, h: PortCheck(p, PortStatus.FREE),
                      terminated_check=alive_terminated(FAKE_PID), progress=QUIET),
    ]
    actions_seen = [result["action"] for result in map(assert_contract, results)]
    assert actions_seen == ["status", "start", "stop", "ensure"]


def test_main_run_status_contract(monkeypatch, capsys, tmp_path):
    root = tmp_path / "cfg"
    root.mkdir()
    monkeypatch.setattr(config_mod, "resolve_config_root", lambda probe=None: root)
    code = cli_main.run(["status"])  # 真实 probe（只读 HTTP），其余全真实
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, "stdout 有且只有一行 JSON"
    data = json.loads(lines[0])
    assert set(data.keys()) == set(CONTRACT_KEYS)
    assert data["action"] == "status"
    assert code == (0 if data["ok"] else 1)


def test_main_usage_error_exit_2(capsys):
    assert cli_main.run(["definitely-not-a-command"]) == 2


# ---------------------------------------------------------------------------
# resolve：参数来源优先级
# ---------------------------------------------------------------------------

def test_resolve_preset_priority_cli_over_last_preset(tmp_path):
    root = make_root(tmp_path, settings={"last_preset": "Old"})
    (root / "presets" / "Old.json").write_text(
        json.dumps({"name": "Old", "model": "OLD", "port": 1}), encoding="utf-8")
    name, data, source = resolve.resolve_preset(root, "P")
    assert name == "P" and source == "cli" and data["model"] == str(root / "model.ninfer")


def test_resolve_preset_last_preset(tmp_path):
    root = make_root(tmp_path, settings={"last_preset": "P"})
    name, data, source = resolve.resolve_preset(root)
    assert name == "P" and source == "last_preset"


def test_resolve_preset_falls_back_to_settings_params(tmp_path):
    root = make_root(tmp_path, preset=False,
                     settings={"last_preset": "Deleted", "params": {"port": 9999}})
    name, data, source = resolve.resolve_preset(root)
    assert name is None and source == "settings" and data["port"] == 9999


def test_resolve_preset_none(tmp_path):
    root = make_root(tmp_path, preset=False, settings={})
    with pytest.raises(resolve.ResolveError) as excinfo:
        resolve.resolve_preset(root)
    assert excinfo.value.code == "no-preset"


def test_resolve_exe_priority(monkeypatch, tmp_path):
    root = make_root(tmp_path, preset=False, settings={"exe_path": ""})
    candidate = tmp_path / "proj" / "build-ninja" / "apps" / "ninfer-serve.exe"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"MZ")
    monkeypatch.setattr(config_mod, "find_project_root", lambda: tmp_path / "proj")
    assert resolve.resolve_exe(root) == str(candidate)


def test_resolve_exe_explicit_arg_wins(tmp_path):
    root = make_root(tmp_path, preset=False)
    target = tmp_path / "custom.exe"
    target.write_bytes(b"MZ")
    assert resolve.resolve_exe(root, exe_arg=str(target)) == str(target)


def test_resolve_exe_missing_reports_code(tmp_path, monkeypatch):
    root = make_root(tmp_path, preset=False, settings={"exe_path": ""})
    monkeypatch.setattr(config_mod, "find_project_root", lambda: tmp_path / "empty")
    with pytest.raises(resolve.ResolveError) as excinfo:
        resolve.resolve_exe(root)
    assert excinfo.value.code == "exe-not-found"


# ---------------------------------------------------------------------------
# 零 Qt 约束（约束 1）：CLI 包导入不得拖进 PySide6
# ---------------------------------------------------------------------------

def test_cli_package_is_qt_free():
    code = chr(10).join(
        [
            "import sys",
            "import ninfer_launcher.cli.main",
            "import ninfer_launcher.cli.actions",
            "bad = [m for m in sys.modules if m == 'PySide6' or m.startswith('PySide6.')]",
            "assert not bad, bad",
            "print('CLEAN')",
        ]
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, "CLI 导入拖进了 PySide6：%s" % proc.stderr
    assert "CLEAN" in proc.stdout
