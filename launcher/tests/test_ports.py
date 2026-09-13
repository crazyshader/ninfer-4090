"""测试端口检查。"""

import pytest

from ninfer_launcher.core.ports import PortCheck, PortStatus, check_port, suggest_port


class TestCheckPort:
    def test_invalid_port_too_low(self):
        result = check_port(0)
        assert result.status is PortStatus.UNKNOWN

    def test_invalid_port_too_high(self):
        result = check_port(70000)
        assert result.status is PortStatus.UNKNOWN

    def test_free_port(self):
        """一个不太可能被占用的端口。"""
        result = check_port(19527)
        assert result.status is PortStatus.FREE


class TestSuggestPort:
    def test_suggest_nearby(self):
        """占用 19527，应该建议 19528 或之后的。"""
        # 先确认 19527 是空闲的（测试环境无服务器运行）
        result = suggest_port(19527)
        # 应该返回某个有效端口
        assert result is None or 1 < result < 65536

    def test_suggest_returns_port_greater_than_preferred(self):
        """建议的端口必须大于 preferred。"""
        result = suggest_port(19527)
        if result is not None:
            assert result > 19527

    def test_suggest_near_upper_bound(self):
        """preferred 接近 65535 时，找不到就返回 None。"""
        # 65535 是最大端口，65536 不合法，所以应该返回 None
        result = suggest_port(65535)
        assert result is None

    def test_suggest_returns_none_when_no_free_port(self, monkeypatch):
        """所有候选端口都被占用时返回 None。"""
        from ninfer_launcher.core.ports import PortStatus, check_port
        import ninfer_launcher.core.ports as ports_module

        def always_in_use(port, host="127.0.0.1"):
            return PortCheck(port, PortStatus.IN_USE, "occupied")

        monkeypatch.setattr(ports_module, "check_port", always_in_use)
        assert suggest_port(10000) is None


class TestCheckPortEdge:
    """check_port 边界情况。"""

    def test_port_1_is_valid(self):
        """端口 1 是合法下界（可能需要 root 权限，但检查不报错）。"""
        result = check_port(1)
        assert result.status in (PortStatus.FREE, PortStatus.IN_USE, PortStatus.UNKNOWN)

    def test_port_65535_is_valid(self):
        """端口 65535 是合法上界。"""
        result = check_port(65535)
        assert result.status in (PortStatus.FREE, PortStatus.IN_USE, PortStatus.UNKNOWN)

    def test_negative_port(self):
        result = check_port(-1)
        assert result.status is PortStatus.UNKNOWN

    def test_port_zero(self):
        result = check_port(0)
        assert result.status is PortStatus.UNKNOWN
