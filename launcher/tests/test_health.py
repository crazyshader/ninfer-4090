"""core/health.py：纯逻辑判据 + 周期探测。

classify_health 是纯函数（状态码 → 健康状态），probe_health 是 I/O 函数，
HealthPoller 是 QTimer 驱动的周期探测器。本文件覆盖全部三层。
"""

import pytest

from ninfer_launcher.core.health import (
    HEALTH_INTERVAL_MS,
    HealthPoller,
    HealthResult,
    HealthState,
    classify_health,
    probe_health,
)


class TestClassifyHealth:
    """classify_health 纯逻辑：HTTP 状态码 → 健康状态。"""

    @pytest.mark.smoke
    def test_200_ready(self):
        result = classify_health(200, "ok")
        assert result.state is HealthState.READY
        assert result.detail == "ready"

    @pytest.mark.smoke
    def test_503_not_ready(self):
        result = classify_health(503, "loading")
        assert result.state is HealthState.NOT_READY
        assert result.detail == "loading"

    def test_500_not_ready(self):
        result = classify_health(500)
        assert result.state is HealthState.NOT_READY
        assert "500" in result.detail

    def test_404_not_ready(self):
        result = classify_health(404)
        assert result.state is HealthState.NOT_READY
        assert "404" in result.detail

    def test_0_not_ready(self):
        result = classify_health(0)
        assert result.state is HealthState.NOT_READY


class TestProbeHealth:
    """probe_health：向真实 /health 端点发请求（测试中用 mock）。"""

    def test_never_raises_on_connection_refused(self, monkeypatch):
        """连接被拒绝时返回 NOT_READY，不抛异常。"""
        import urllib.error
        import urllib.request

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = probe_health("127.0.0.1", 9999, timeout=0.1)
        assert result.state is HealthState.NOT_READY
        assert "连接失败" in result.detail

    def test_never_raises_on_timeout(self, monkeypatch):
        """超时返回 NOT_READY，不抛异常。"""
        import socket
        import urllib.request

        def fake_urlopen(req, timeout=None):
            raise socket.timeout("timed out")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = probe_health("127.0.0.1", 9999, timeout=0.1)
        assert result.state is HealthState.NOT_READY

    def test_http_error_503(self, monkeypatch):
        """HTTPError 503 → NOT_READY。"""
        import urllib.error
        import urllib.request
        from http.client import HTTPResponse
        import io

        class FakeHTTPError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://test/health", 503, "Service Unavailable", {}, io.BytesIO(b""))

        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = probe_health("127.0.0.1", 9999)
        assert result.state is HealthState.NOT_READY
        assert result.detail == "loading"


class TestHealthPoller:
    """HealthPoller：QTimer 驱动的周期探测。"""

    def test_construction(self):
        poller = HealthPoller(host="127.0.0.1", port=8080)
        assert poller.host == "127.0.0.1"
        assert poller.port == 8080

    def test_host_port_setters(self):
        poller = HealthPoller()
        poller.host = "0.0.0.0"
        poller.port = 9090
        assert poller.host == "0.0.0.0"
        assert poller.port == 9090

    def test_default_interval(self):
        poller = HealthPoller()
        assert HEALTH_INTERVAL_MS > 0

    def test_start_stop_idempotent(self):
        poller = HealthPoller(port=19999)
        poller.start()
        poller.stop()
        poller.stop()  # 第二次 stop 不报错


class TestHealthState:
    """HealthState 枚举值。"""

    def test_states(self):
        assert HealthState.UNKNOWN.value == "unknown"
        assert HealthState.NOT_READY.value == "not_ready"
        assert HealthState.READY.value == "ready"
        assert HealthState.ABORTED.value == "aborted"

    def test_result_frozen(self):
        """HealthResult 是 frozen dataclass。"""
        import dataclasses
        with pytest.raises(dataclasses.FrozenInstanceError):
            r = HealthResult(HealthState.READY)
            r.state = HealthState.NOT_READY  # type: ignore
