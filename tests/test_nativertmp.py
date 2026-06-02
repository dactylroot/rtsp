"""Tests for rtsp.RTMPClient and rtsp.RTMPPublisher.

Unit tests verify routing and lifecycle without a running RTMP server.
Integration tests require mediamtx (which supports both RTMP publish
and RTSP playback) and PyAV.
"""

import time
from unittest.mock import patch

import pytest
from PIL import Image

import rtsp
from rtsp.nativertmp import RTMPClient, RTMPPublisher

from conftest import requires_mediamtx

requires_av = pytest.mark.skipif(
    __import__('importlib').util.find_spec('av') is None,
    reason='PyAV (av) not installed',
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def no_open_client(monkeypatch):
    monkeypatch.setattr(RTMPClient, 'open', lambda self: self)


@pytest.fixture
def no_open_publisher(monkeypatch):
    monkeypatch.setattr(RTMPPublisher, 'open', lambda self: self)


# ---------------------------------------------------------------------------
# URI / routing
# ---------------------------------------------------------------------------

class TestRouting:

    def test_client_rtmp_routes_to_rtmpclient(self, no_open_client):
        c = rtsp.Client('rtmp://127.0.0.1:1935/live/stream')
        assert isinstance(c, RTMPClient)

    def test_client_rtmps_routes_to_rtmpclient(self, no_open_client):
        c = rtsp.Client('rtmps://127.0.0.1:443/live/stream')
        assert isinstance(c, RTMPClient)

    def test_source_rtmp_routes_to_rtmppublisher(self, no_open_publisher):
        s = rtsp.Source('rtmp://127.0.0.1:1935/live/stream', serve=False)
        assert isinstance(s, RTMPPublisher)

    def test_source_rtmps_routes_to_rtmppublisher(self, no_open_publisher):
        s = rtsp.Source('rtmps://127.0.0.1:443/live/stream', serve=False)
        assert isinstance(s, RTMPPublisher)

    def test_rtsp_still_routes_to_native_client(self):
        from rtsp.client import _RtspClient
        with patch.object(_RtspClient, 'open', lambda self: self):
            c = rtsp.Client('rtsp://127.0.0.1:554/live')
        assert isinstance(c, _RtspClient)

    def test_rtsp_serve_false_still_routes_to_native_publisher(self):
        from rtsp.source import Publisher
        with patch.object(Publisher, 'open', lambda self: self):
            s = rtsp.Source('rtsp://127.0.0.1:8554/live', serve=False)
        assert isinstance(s, Publisher)


# ---------------------------------------------------------------------------
# RTMPClient lifecycle (no server)
# ---------------------------------------------------------------------------

class TestRTMPClientLifecycle:

    def test_not_opened_before_connect(self, no_open_client):
        c = RTMPClient('rtmp://127.0.0.1:1935/live')
        assert not c.isOpened()

    def test_read_returns_none_when_not_opened(self, no_open_client):
        c = RTMPClient('rtmp://127.0.0.1:1935/live')
        assert c.read() is None

    def test_close_is_safe_without_connect(self, no_open_client):
        c = RTMPClient('rtmp://127.0.0.1:1935/live')
        c.close()  # must not raise

    def test_context_manager_calls_close(self, no_open_client):
        with RTMPClient('rtmp://127.0.0.1:1935/live') as c:
            pass
        assert not c.isOpened()

    def test_connection_refused_does_not_crash(self):
        """Failed connect sets isOpened() to False without raising."""
        c = RTMPClient('rtmp://127.0.0.1:1/live')
        # recv_loop runs in background; give it a moment to fail
        deadline = time.monotonic() + 5
        while c.isOpened() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not c.isOpened()


# ---------------------------------------------------------------------------
# RTMPPublisher lifecycle (no server)
# ---------------------------------------------------------------------------

class TestRTMPPublisherLifecycle:

    def test_uri_stored(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1:1935/live/stream')
        assert 'rtmp' in p._uri

    def test_fps_stored(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1:1935/live', fps=15)
        assert p._fps == 15

    def test_not_opened_before_put(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1:1935/live')
        assert not p.isOpened()

    def test_put_sets_size(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1:1935/live')
        p.put(Image.new('RGB', (320, 240)))
        assert p._size == (320, 240)

    def test_put_resizes_to_match_first_frame(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1:1935/live')
        p.put(Image.new('RGB', (320, 240)))
        p.put(Image.new('RGB', (1920, 1080)))
        assert p._buffer[-1].size == (320, 240)

    def test_size_snaps_to_even(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1:1935/live', size=(641, 479))
        assert p._size == (640, 478)

    def test_close_is_idempotent(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1:1935/live')
        p.close()
        p.close()

    def test_serve_forever_raises(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1:1935/live')
        with pytest.raises(RuntimeError, match='serve=True'):
            p.serve_forever()

    def test_device_uri_raises(self):
        with pytest.raises(ValueError, match='network address'):
            RTMPPublisher(0)

    @requires_av
    def test_encode_loop_stops_on_failed_connection(self):
        """Encode loop sets isOpened() False when the relay is unreachable."""
        p = RTMPPublisher('rtmp://127.0.0.1:1/live', size=(64, 64))
        p.put(Image.new('RGB', (64, 64)))
        deadline = time.monotonic() + 10
        while p.isOpened() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not p.isOpened()


# ---------------------------------------------------------------------------
# Integration: RTMPPublisher → mediamtx → RTMPClient round-trip
# ---------------------------------------------------------------------------

@requires_mediamtx
@requires_av
class TestMediamtxRoundTrip:
    """Push via RTMP to mediamtx, read back via RTMP."""

    def test_publisher_connects(self, mediamtx_server, nouveau_frames):
        path = '/rtmp_test'
        pub_uri = mediamtx_server.replace('rtsp://', 'rtmp://') + path
        frames = [Image.open(p).convert('RGB') for p in nouveau_frames[:3]]
        with RTMPPublisher(pub_uri, fps=5, frame_buffer=frames) as pub:
            assert pub.isOpened()

    def test_client_receives_frame(self, mediamtx_server, nouveau_frames):
        path = '/rtmp_roundtrip'
        pub_uri = mediamtx_server.replace('rtsp://', 'rtmp://') + path
        client_uri = pub_uri

        frames = [Image.open(p).convert('RGB') for p in nouveau_frames[:3]]
        with RTMPPublisher(pub_uri, fps=5, frame_buffer=frames) as pub:
            assert pub.isOpened()
            time.sleep(1.0)
            with RTMPClient(client_uri) as client:
                deadline = time.monotonic() + 15
                received = None
                while time.monotonic() < deadline:
                    received = client.read()
                    if received is not None:
                        break
                    time.sleep(0.1)

        assert received is not None, 'RTMPClient received no frame'
        assert isinstance(received, Image.Image)
