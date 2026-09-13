"""测试进程管理纯逻辑：状态机、LineAssembler、ExitInfo。"""

import pytest

from ninfer_launcher.core.process import (
    ALLOWED_TRANSITIONS,
    ExitInfo,
    LineAssembler,
    ServerState,
    SettleOutcome,
    VramSettle,
    VramSettleWatcher,
    can_transition,
    decode_output,
    settle_vram,
    stop_external_sync,
    taskkill_command,
)


class TestStateTransitions:
    """状态机迁移规则。"""

    def test_stopped_to_starting(self):
        assert can_transition(ServerState.STOPPED, ServerState.STARTING)

    def test_starting_to_running(self):
        assert can_transition(ServerState.STARTING, ServerState.RUNNING)

    def test_starting_to_stopped(self):
        """探通前退出。"""
        assert can_transition(ServerState.STARTING, ServerState.STOPPED)

    def test_running_to_stopped(self):
        """运行中崩掉。"""
        assert can_transition(ServerState.RUNNING, ServerState.STOPPED)

    def test_running_to_stopping(self):
        assert can_transition(ServerState.RUNNING, ServerState.STOPPING)

    def test_stopping_to_stopped(self):
        assert can_transition(ServerState.STOPPING, ServerState.STOPPED)

    def test_invalid_stopped_to_running(self):
        assert not can_transition(ServerState.STOPPED, ServerState.RUNNING)

    def test_invalid_stopped_to_stopping(self):
        assert not can_transition(ServerState.STOPPED, ServerState.STOPPING)

    def test_same_state(self):
        assert not can_transition(ServerState.STOPPED, ServerState.STOPPED)


class TestLineAssembler:
    """行装配器。"""

    def test_simple_line(self):
        a = LineAssembler()
        lines = a.feed(b"hello\n")
        assert lines == ["hello"]

    def test_multi_lines(self):
        a = LineAssembler()
        lines = a.feed(b"one\ntwo\nthree\n")
        assert lines == ["one", "two", "three"]

    def test_partial_line(self):
        a = LineAssembler()
        lines = a.feed(b"partial")
        assert lines == []
        lines = a.feed(b" line\n")
        assert lines == ["partial line"]

    def test_crlf(self):
        a = LineAssembler()
        lines = a.feed(b"line1\r\nline2\r\n")
        assert lines == ["line1", "line2"]

    def test_trailing_cr(self):
        a = LineAssembler()
        lines = a.feed(b"line1\r")
        assert lines == []
        lines = a.feed(b"\nline2\n")
        assert lines == ["line1", "line2"]

    def test_flush(self):
        a = LineAssembler()
        a.feed(b"incomplete")
        lines = a.flush()
        assert lines == ["incomplete"]

    def test_flush_empty(self):
        a = LineAssembler()
        assert a.flush() == []

    def test_unicode_utf8(self):
        a = LineAssembler()
        text = "中文测试".encode("utf-8")
        lines = a.feed(text + b"\n")
        assert lines == ["中文测试"]

    def test_split_multibyte(self):
        """多字节字符被切成两半。"""
        a = LineAssembler()
        data = "你".encode("utf-8")  # b'\xe4\xbd\xa0'
        lines = a.feed(data[:2])
        assert lines == []
        lines = a.feed(data[2:] + b"\n")
        assert lines == ["你"]

    def test_buffer_limit(self):
        a = LineAssembler(limit=64)
        # 超过 64 字节无换行
        lines = a.feed(b"\x00" * 100)
        assert len(lines) == 1
        assert a.pending == b""


class TestDecodeOutput:
    """输出解码。"""

    def test_ascii(self):
        text, enc = decode_output(b"hello")
        assert text == "hello"
        assert enc == "utf-8"

    def test_utf8(self):
        text, enc = decode_output("你好".encode("utf-8"))
        assert text == "你好"

    def test_empty(self):
        text, enc = decode_output(b"")
        assert text == ""


class TestTaskkill:
    def test_command(self):
        cmd = taskkill_command(1234)
        assert cmd == ["taskkill", "/PID", "1234", "/T", "/F"]


class TestExitInfo:
    def test_code_text_normal(self):
        info = ExitInfo(exit_code=0)
        assert info.code_text == "0"

    def test_code_text_none(self):
        info = ExitInfo(exit_code=None)
        assert info.code_text == "未知"

    def test_clean(self):
        info = ExitInfo(exit_code=0, crashed=False)
        assert info.clean

    def test_not_clean_crashed(self):
        info = ExitInfo(exit_code=0, crashed=True)
        assert not info.clean

    def test_headline_failed_to_start(self):
        info = ExitInfo(failed_to_start=True)
        assert "未能启动" in info.headline()


class TestVramSettle:
    def test_degraded_no_reader(self):
        """无读数来源 -> 降级。"""
        import time
        watcher = VramSettleWatcher(None, fallback_wait=0.01, clock=lambda: 0.0)
        watcher.begin()
        # 模拟时间过了 0.01 秒
        watcher._started_at = 0.0
        watcher._clock = lambda: 0.02
        result = watcher.poll()
        assert result.outcome is SettleOutcome.DEGRADED

    def test_settled(self):
        """显存回落。"""
        readings = [20 * 1024**3, 20 * 1024**3, 18 * 1024**3]
        it = iter(readings)
        clock_val = [0.0]

        def fake_reader():
            return next(it, None)

        def fake_clock():
            return clock_val[0]

        watcher = VramSettleWatcher(
            fake_reader, timeout=3.0, interval=0.25,
            fallback_wait=0.5, drop_bytes=128 * 1024 * 1024,
            clock=fake_clock,
        )
        watcher.begin()
        # 第一次 poll：baseline=20GB, reading=20GB -> PENDING
        r = watcher.poll()
        assert r.outcome is SettleOutcome.PENDING
        # 第二次：reading=18GB, drop=2GB > 128MB -> SETTLED
        clock_val[0] = 0.3
        r = watcher.poll()
        assert r.outcome is SettleOutcome.SETTLED

    def test_timeout(self):
        """超时。"""
        def fake_reader():
            return 1024**3  # 1 GB，不变

        watcher = VramSettleWatcher(
            fake_reader, timeout=0.1, interval=0.05,
            fallback_wait=0.5, drop_bytes=128 * 1024 * 1024,
        )
        watcher.begin()
        result = watcher.poll()
        assert result.outcome is SettleOutcome.PENDING
        # 等待超过 timeout
        import time
        time.sleep(0.15)
        result = watcher.poll()
        assert result.outcome is SettleOutcome.TIMEOUT


class TestStopExternalSync:
    """stop_external_sync：关闭路径的外部实例同步停止升级链（纯函数，全假件注入）。"""

    PID = 987654

    def _fakes(self, *, alive_after_kill, die_at_round=None):
        """构造一组可编排的假 I/O。

        alive_after_kill: taskkill 之后进程是否仍存活（True=仍活着/杀不死）。
        die_at_round: 第 N 轮强杀后进程才真死（None=强杀也无效；0=一开始就已死）。
        taskkill 之前进程一律按存活处理（除非 die_at_round=0 已覆盖）。
        """
        killer_calls: list[int] = []
        hammer_calls: list[int] = []
        check_calls: list[int | None] = []
        messages: list[str] = []
        sleeps: list[float] = []
        clock_val = [0.0]

        def fake_clock():
            return clock_val[0]

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock_val[0] += seconds

        def fake_terminated(pid):
            check_calls.append(pid)
            if die_at_round is not None and len(hammer_calls) >= die_at_round:
                return True
            if not killer_calls:
                return False  # taskkill 之前进程仍存活
            return not alive_after_kill

        fakes = dict(
            killer=lambda pid: (killer_calls.append(pid), (True, f"假 taskkill {pid}"))[1],
            hard_terminator=lambda pid: (hammer_calls.append(pid), (True, f"假强杀 {pid}"))[1],
            terminated_check=fake_terminated,
            kill_escalation=1.0,
            kill_check_interval_ms=100,
            clock=fake_clock,
            sleep=fake_sleep,
            on_message=messages.append,
        )
        state = {
            "killer_calls": killer_calls,
            "hammer_calls": hammer_calls,
            "check_calls": check_calls,
            "messages": messages,
            "sleeps": sleeps,
            "clock": clock_val,
        }
        return fakes, state

    def test_already_dead_returns_true_without_killing(self):
        fakes, state = self._fakes(alive_after_kill=True, die_at_round=0)  # terminated_check 恒 True=已死
        ok = stop_external_sync(self.PID, **fakes)
        assert ok is True
        assert not state["killer_calls"], "进程已死：不得再发任何杀命令"
        assert not state["hammer_calls"]
        assert any("已不在运行" in m for m in state["messages"])

    def test_missing_pid_is_false(self):
        fakes, state = self._fakes(alive_after_kill=False)
        assert stop_external_sync(0, **fakes) is False
        assert not state["killer_calls"]

    def test_taskkill_suffices(self):
        """taskkill 后即死：返回 True，不进入强杀升级。"""
        fakes, state = self._fakes(alive_after_kill=False)
        ok = stop_external_sync(self.PID, **fakes)
        assert ok is True
        assert state["killer_calls"] == [self.PID]
        assert not state["hammer_calls"], "taskkill 已生效：无需强杀"
        assert not state["sleeps"], "确认死亡后立即返回，不阻塞等待"

    def test_hard_kill_rounds_until_os_confirms_death(self):
        """taskkill 失效、第 2 轮强杀后 OS 确认死亡：返回 True，且校验被多次调用。"""
        fakes, state = self._fakes(alive_after_kill=True, die_at_round=2)
        ok = stop_external_sync(self.PID, **fakes)
        assert ok is True
        assert state["killer_calls"] == [self.PID]
        assert len(state["hammer_calls"]) == 2, "每轮强杀直到 OS 确认死亡"
        assert len(state["check_calls"]) >= 3, "OS 真值校验必须贯穿全程（多轮）"
        assert state["sleeps"], "强杀轮次之间必须按间隔阻塞等待"
        assert all(s == 0.1 for s in state["sleeps"]), "间隔应取 kill_check_interval_ms"
        assert not any("仍未退出" in m for m in state["messages"]), "进程已真死：不应出现时限告警"

    def test_deadline_exhausted_returns_false_with_warning(self):
        """始终杀不死：时限用尽返回 False，on_message 收到显著警告。"""
        fakes, state = self._fakes(alive_after_kill=True)
        ok = stop_external_sync(self.PID, **fakes)
        assert ok is False
        assert state["killer_calls"] == [self.PID]
        assert state["hammer_calls"], "时限内必须持续重试强杀"
        assert state["clock"][0] >= 1.0, "必须推进到升级时限用尽"
        assert any("仍未退出" in m and "任务管理器" in m for m in state["messages"]), (
            "时限用尽仍存活必须给出显著警告（含手动处置指引），不能静默残留"
        )
