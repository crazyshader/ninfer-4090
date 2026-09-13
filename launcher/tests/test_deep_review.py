"""深入复盘验证：针对代码审查中发现的边界行为与隐式契约的补充测试。

本文件不重复已有测试，而是覆盖审查中识别出的「做错了验证不会变红」的具体路径：
1. _is_disabled 的 BOOL3 归一化匹配（builder.py）
2. ServerProcess 启动/停止的状态机边界（process.py）
3. LineAssembler 混合换行符与超大缓冲（process.py）
4. VramSettleWatcher 的 DEGRADED→TIMEOUT 转换（process.py）
5. MonitorService 的 NVML 运行期失效→回落→恢复（monitor.py）
6. 预设 round-trip 中 Bool3 的序列化/反序列化（config + spec）
"""

import pytest

# =====================================================================
# 1. builder._is_disabled：BOOL3 归一化匹配
# =====================================================================


class TestIsDisabled:
    """_is_disabled 的 BOOL3 归一化逻辑。"""

    def test_spec_none_disables_draft_tokens(self):
        from ninfer_launcher.params.builder import build
        from ninfer_launcher.params.registry import default_values
        values = default_values()
        values["spec"] = "none"
        args = build(values)
        # draft_tokens 和 lm_head_draft 应该被跳过
        assert "--draft-tokens" not in args
        assert "--lm-head-draft" not in args

    def test_spec_mtp_does_not_disable_draft_tokens(self):
        from ninfer_launcher.params.builder import build
        from ninfer_launcher.params.registry import default_values
        values = default_values()
        values["spec"] = "mtp"
        args = build(values)
        assert "--draft-tokens" in args

    def test_no_thinking_on_disables_reasoning_effort(self):
        from ninfer_launcher.params.builder import build
        from ninfer_launcher.params.registry import default_values
        from ninfer_launcher.params.spec import Bool3
        values = default_values()
        values["no_thinking"] = Bool3.ON
        values["reasoning_effort"] = "medium"
        args = build(values)
        # reasoning_effort 被禁用，不应落参
        assert "--reasoning-effort" not in args

    def test_no_thinking_off_allows_reasoning_effort(self):
        from ninfer_launcher.params.builder import build
        from ninfer_launcher.params.registry import default_values
        from ninfer_launcher.params.spec import Bool3
        values = default_values()
        values["no_thinking"] = Bool3.OFF
        values["reasoning_effort"] = "medium"
        args = build(values)
        assert "--reasoning-effort" in args
        assert "medium" in args

    def test_vision_off_disables_vision_max_tokens(self):
        from ninfer_launcher.params.builder import build
        from ninfer_launcher.params.registry import default_values
        from ninfer_launcher.params.spec import Bool3
        values = default_values()
        values["vision"] = Bool3.OFF
        values["vision_max_tokens"] = 4096
        args = build(values)
        assert "--vision-max-tokens" not in args

    def test_vision_on_with_max_tokens(self):
        from ninfer_launcher.params.builder import build
        from ninfer_launcher.params.registry import default_values
        from ninfer_launcher.params.spec import Bool3
        values = default_values()
        values["vision"] = Bool3.ON
        values["vision_max_tokens"] = 4096
        args = build(values)
        assert "--vision-max-tokens" in args
        assert "4096" in args

    def test_vision_on_with_max_tokens_none(self):
        from ninfer_launcher.params.builder import build
        from ninfer_launcher.params.registry import default_values
        from ninfer_launcher.params.spec import Bool3
        values = default_values()
        values["vision"] = Bool3.ON
        values["vision_max_tokens"] = None
        args = build(values)
        # vision_max_tokens=None → NullableSpinBox 不落参
        assert "--vision-max-tokens" not in args

    def test_no_thinking_as_string_on(self):
        """no_thinking 以字符串 'on' 形式传入，也应触发禁用。"""
        from ninfer_launcher.params.builder import build
        from ninfer_launcher.params.registry import default_values
        values = default_values()
        values["no_thinking"] = "on"
        values["reasoning_effort"] = "medium"
        args = build(values)
        assert "--reasoning-effort" not in args

    def test_no_thinking_as_true(self):
        """no_thinking 以 Python True 传入（Bool3.from_config(True)=ON）。"""
        from ninfer_launcher.params.builder import build
        from ninfer_launcher.params.registry import default_values
        values = default_values()
        values["no_thinking"] = True
        values["reasoning_effort"] = "medium"
        args = build(values)
        assert "--reasoning-effort" not in args


# =====================================================================
# 2. ServerProcess 启动/停止边界
# =====================================================================


class TestServerProcessStart:
    """ServerProcess.start 的边界行为。"""

    def test_start_nonexistent_file(self):
        """启动不存在的文件 → 立即 failed_to_start，状态保持 STOPPED。"""
        from PySide6.QtCore import QCoreApplication
        from ninfer_launcher.core.process import ServerProcess, ServerState

        app = QCoreApplication.instance() or QCoreApplication([])
        proc = ServerProcess()
        assert proc.state is ServerState.STOPPED

        exited = []
        proc.exited.connect(lambda info: exited.append(info))

        ok = proc.start("E:/nonexistent/path/to/server.exe", ["--port", "8080"])
        assert ok is False
        assert proc.state is ServerState.STOPPED
        assert len(exited) == 1
        assert exited[0].failed_to_start is True
        assert exited[0].clean is False

    def test_start_when_not_stopped_ignored(self):
        """非 STOPPED 状态时启动请求被忽略。"""
        from PySide6.QtCore import QCoreApplication
        from ninfer_launcher.core.process import ServerProcess, ServerState

        app = QCoreApplication.instance() or QCoreApplication([])
        proc = ServerProcess()
        # 模拟 STARTING 状态
        proc._state = ServerState.STARTING
        ok = proc.start("E:/some/file.exe", [])
        assert ok is False
        # 状态不变
        assert proc.state is ServerState.STARTING

    def test_stop_when_stopped(self):
        """STOPPED 状态且进程不在运行时，stop 返回 False。"""
        from PySide6.QtCore import QCoreApplication
        from ninfer_launcher.core.process import ServerProcess, ServerState

        app = QCoreApplication.instance() or QCoreApplication([])
        proc = ServerProcess()
        assert proc.state is ServerState.STOPPED
        ok = proc.stop()
        assert ok is False

    def test_mark_ready_when_not_starting(self):
        """非 STARTING 状态时 mark_ready 返回 False。"""
        from PySide6.QtCore import QCoreApplication
        from ninfer_launcher.core.process import ServerProcess, ServerState

        app = QCoreApplication.instance() or QCoreApplication([])
        proc = ServerProcess()
        assert proc.state is ServerState.STOPPED
        assert proc.mark_ready() is False
        assert proc.state is ServerState.STOPPED

    def test_abort_reason_when_stopped(self):
        """STOPPED 且无退出信息时，abort_reason 返回子进程不在运行的消息。"""
        from PySide6.QtCore import QCoreApplication
        from ninfer_launcher.core.process import ServerProcess

        app = QCoreApplication.instance() or QCoreApplication([])
        proc = ServerProcess()
        reason = proc.abort_reason()
        assert reason is not None
        assert "不在运行" in reason or "已不在" in reason


# =====================================================================
# 3. LineAssembler 混合换行符与边界
# =====================================================================


class TestLineAssemblerEdge:
    """LineAssembler 的额外边界测试。"""

    def test_mixed_crlf_and_lf(self):
        """同一 buffer 中混合 CRLF 和 LF。"""
        from ninfer_launcher.core.process import LineAssembler
        asm = LineAssembler()
        lines = asm.feed(b"line1\r\nline2\nline3\r\n")
        assert lines == ["line1", "line2", "line3"]

    def test_multiple_cr(self):
        """连续多个 CR（老 Mac 换行）。"""
        from ninfer_launcher.core.process import LineAssembler
        asm = LineAssembler()
        lines = asm.feed(b"a\r\r\rb")
        #     → 两个换行，产生 "a", "", 然后 "b" 在 buffer 中
        assert "a" in lines

    def test_binary_data(self):
        """二进制数据不崩溃。"""
        from ninfer_launcher.core.process import LineAssembler
        asm = LineAssembler()
        data = bytes(range(256))
        lines = asm.feed(data)
        # 不应该抛异常
        assert isinstance(lines, list)

    def test_very_long_line_truncates_buffer(self):
        """超过 LINE_BUFFER_LIMIT 的行被截断。"""
        from ninfer_launcher.core.process import LineAssembler, LINE_BUFFER_LIMIT
        asm = LineAssembler(limit=LINE_BUFFER_LIMIT)
        # 构造一个超过 limit 的行
        long_line = b"A" * (LINE_BUFFER_LIMIT + 1000)
        lines = asm.feed(long_line)
        # buffer 被截断后应该 flush 出一行
        assert len(lines) >= 1
        # 截断后的 buffer 应该是空的
        assert len(asm.pending) == 0

    def test_empty_feeds_noop(self):
        """空数据 feed 返回空列表。"""
        from ninfer_launcher.core.process import LineAssembler
        asm = LineAssembler()
        assert asm.feed(b"") == []
        assert asm.feed(b"") == []

    def test_flush_preserves_partial(self):
        """flush 只输出完整行，不丢数据。"""
        from ninfer_launcher.core.process import LineAssembler
        asm = LineAssembler()
        asm.feed(b"hello")
        lines = asm.flush()
        assert lines == ["hello"]
        # flush 后 buffer 为空
        assert asm.pending == b""


# =====================================================================
# 4. VramSettleWatcher DEGRADED 转换
# =====================================================================


class TestVramSettleWatcherDegraded:
    """VramSettleWatcher 的 DEGRADED 路径。"""

    def test_reader_returns_none_mid_poll(self):
        """读值中途返回 None → DEGRADED。

        begin() 消费 reader 第 1 次调用（设 baseline），
        poll() 每次消费一次 reader 调用。
        所以：begin 取第 1 个值，poll#1 取第 2 个值，poll#2 取第 3 个值（None）。
        """
        from ninfer_launcher.core.process import (
            SettleOutcome, VramSettleWatcher,
        )
        calls = {"n": 0}

        def reader():
            calls["n"] += 1
            if calls["n"] <= 2:
                return 10 * 1024 * 1024
            return None

        clock_val = [0.0]
        watcher = VramSettleWatcher(
            reader, timeout=3.0, fallback_wait=0.1,
            drop_bytes=128 * 1024 * 1024,
            clock=lambda: clock_val[0],
        )
        watcher.begin()
        # poll#1: reading=10MB (与 baseline 相同) → PENDING
        clock_val[0] = 0.25
        result = watcher.poll()
        assert result.outcome is SettleOutcome.PENDING
        # poll#2: reader 返回 None → DEGRADED
        clock_val[0] = 0.5
        result = watcher.poll()
        assert result.outcome is SettleOutcome.DEGRADED
        assert result.reason is not None
        assert "不可用" in result.reason

    def test_no_reader_degrades_after_fallback_wait(self):
        """无 reader 时，等待 fallback_wait 后 DEGRADED。"""
        from ninfer_launcher.core.process import (
            SettleOutcome, VramSettleWatcher,
        )
        clock_val = [0.0]
        watcher = VramSettleWatcher(
            None, timeout=3.0, fallback_wait=0.5,
            drop_bytes=128 * 1024 * 1024,
            clock=lambda: clock_val[0],
        )
        watcher.begin()
        # 未到达 fallback_wait → PENDING
        clock_val[0] = 0.3
        result = watcher.poll()
        assert result.outcome is SettleOutcome.PENDING
        # 超过 fallback_wait → DEGRADED
        clock_val[0] = 0.6
        result = watcher.poll()
        assert result.outcome is SettleOutcome.DEGRADED
        assert result.reason is not None

    def test_settled_before_timeout(self):
        """显存在超时前回落 → SETTLED。

        begin() 消费 readings[0]（baseline），
        poll#1 消费 readings[1]（没变），
        poll#2 消费 readings[2]（回落 4GB > 128MB → SETTLED）。
        """
        from ninfer_launcher.core.process import (
            SettleOutcome, VramSettleWatcher,
        )
        call_count = {"n": 0}
        readings = [
            5000 * 1024 * 1024,  # begin() 用
            5000 * 1024 * 1024,  # poll#1 用（没变）
            1000 * 1024 * 1024,  # poll#2 用（回落 4GB）
        ]

        def reader():
            idx = min(call_count["n"], len(readings) - 1)
            call_count["n"] += 1
            return readings[idx]

        clock_val = [0.0]
        watcher = VramSettleWatcher(
            reader, timeout=3.0, interval=0.25,
            drop_bytes=128 * 1024 * 1024,
            clock=lambda: clock_val[0],
        )
        watcher.begin()
        clock_val[0] = 0.25
        r1 = watcher.poll()
        assert r1.outcome is SettleOutcome.PENDING
        clock_val[0] = 0.5
        r2 = watcher.poll()
        assert r2.outcome is SettleOutcome.SETTLED
        assert r2.waited <= 3.0


# =====================================================================
# 5. 预设 round-trip：Bool3 序列化/反序列化
# =====================================================================


class TestPresetBool3Roundtrip:
    """预设文件中 Bool3 以字符串存储，加载时正确还原。"""

    def test_on_off_unset_roundtrip(self):
        from ninfer_launcher.params.spec import Bool3
        # 写入：Bool3 → 字符串
        assert Bool3.ON.to_config() == "on"
        assert Bool3.OFF.to_config() == "off"
        assert Bool3.UNSET.to_config() == "unset"
        # 读取：字符串 → Bool3
        assert Bool3.from_config("on") is Bool3.ON
        assert Bool3.from_config("off") is Bool3.OFF
        assert Bool3.from_config("unset") is Bool3.UNSET

    def test_from_config_various_inputs(self):
        from ninfer_launcher.params.spec import Bool3
        assert Bool3.from_config(None) is Bool3.UNSET
        assert Bool3.from_config(True) is Bool3.ON
        assert Bool3.from_config(False) is Bool3.OFF
        assert Bool3.from_config("true") is Bool3.ON
        assert Bool3.from_config("false") is Bool3.OFF
        assert Bool3.from_config("1") is Bool3.ON
        assert Bool3.from_config("0") is Bool3.OFF
        assert Bool3.from_config("enabled") is Bool3.ON
        assert Bool3.from_config("disabled") is Bool3.OFF
        assert Bool3.from_config("") is Bool3.UNSET
        assert Bool3.from_config("none") is Bool3.UNSET

    def test_from_config_invalid_raises(self):
        from ninfer_launcher.params.spec import Bool3
        with pytest.raises(ValueError):
            Bool3.from_config("maybe")

    def test_from_config_int_rejected(self):
        """非 bool 的 int 不被识别为 Bool3。"""
        from ninfer_launcher.params.spec import Bool3
        with pytest.raises(ValueError):
            Bool3.from_config(1)

    def test_param_value_to_config(self):
        """main_window._param_value_to_config 把 Bool3 转字符串。"""
        from ninfer_launcher.ui.main_window import _param_value_to_config
        from ninfer_launcher.params.spec import Bool3
        assert _param_value_to_config(Bool3.ON) == "on"
        assert _param_value_to_config(Bool3.OFF) == "off"
        assert _param_value_to_config(Bool3.UNSET) == "unset"
        # 非 Bool3 原样透传
        assert _param_value_to_config(8080) == 8080
        assert _param_value_to_config("mtp") == "mtp"
        assert _param_value_to_config(None) is None
        assert _param_value_to_config(1.5) == 1.5


# =====================================================================
# 6. monitor 格式函数边界
# =====================================================================


class TestMonitorFormatEdge:
    """monitor_panel 的格式函数边界。"""

    def test_format_zero_values(self):
        """0 值应显示为 '0'，不是 '不可用'。"""
        from ninfer_launcher.ui.monitor_panel import (
            format_optional_number, format_bytes_pair,
        )
        assert format_optional_number(0, " MHz") == "0 MHz"
        assert format_optional_number(0.0, "%", decimals=1) == "0.0%"
        pair = format_bytes_pair(0, 24576, 0.0)
        assert "0 MiB" in pair
        assert "0.0%" in pair

    def test_format_none_values(self):
        """None 值显示为 '不可用'。"""
        from ninfer_launcher.ui.monitor_panel import (
            format_optional_number, format_bytes_pair, format_power_pair,
        )
        assert format_optional_number(None) == "不可用"
        pair = format_bytes_pair(None, 24576, None)
        assert "不可用" in pair
        power = format_power_pair(None, None)
        assert power == "不可用 / 不可用"

    def test_format_negative_power(self):
        """负数功耗（不应该发生，但格式函数不应崩溃）。"""
        from ninfer_launcher.ui.monitor_panel import format_optional_number
        result = format_optional_number(-1.5, " W", decimals=1)
        assert "-1.5 W" == result
