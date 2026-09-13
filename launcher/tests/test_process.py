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
