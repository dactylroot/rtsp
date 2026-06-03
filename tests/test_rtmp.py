"""Tests for rtsp.RTMPClient and rtsp.RTMPPublisher.

Unit tests verify routing and lifecycle without a running RTMP server.
Integration tests require mediamtx (which supports RTMP publish and playback)
and PyAV.
"""

import sys
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

import rtsp
from rtsp.rtmp import RTMPClient, RTMPPublisher

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

@requires_av
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

    def test_rtsp_still_routes_to_rtsp_client(self):
        from rtsp.client import _RtspClient
        with patch.object(_RtspClient, 'open', lambda self: self):
            c = rtsp.Client('rtsp://127.0.0.1:554/live')
        assert isinstance(c, _RtspClient)

    def test_rtsp_serve_false_still_routes_to_publisher(self):
        from rtsp.source import Publisher
        with patch.object(Publisher, 'open', lambda self: self):
            s = rtsp.Source('rtsp://127.0.0.1:8554/live', serve=False)
        assert isinstance(s, Publisher)


# ---------------------------------------------------------------------------
# RTMPClient lifecycle (no server)
# ---------------------------------------------------------------------------

@requires_av
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
        """Client retries on connection refused and stops cleanly when closed."""
        c = RTMPClient('rtmp://127.0.0.1:1/live')
        time.sleep(1.0)  # let it attempt a couple of retries
        c.close()        # must not raise or hang
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
# RTMPClient reconnection backoff (_recv_loop)
# ---------------------------------------------------------------------------

class TestRTMPClientBackoff:
    """Verify _recv_loop backoff behaviour without real I/O or real time.

    Each test patches _av, time.sleep, and time.monotonic so _recv_loop
    runs synchronously in the test thread with deterministic timing.
    The no_open_client fixture suppresses the background thread; _bg_run
    is set manually before calling _recv_loop() directly.
    """

    def _run(self, no_open_client, mock_av_cfg, monotonic_vals, sleeps):
        """Create a client, set _bg_run, and run _recv_loop() synchronously."""
        mono = iter(monotonic_vals)
        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep', side_effect=sleeps.append), \
             patch('rtsp.rtmp.time.monotonic', side_effect=mono):
            mock_av_cfg(mock_av)
            c = RTMPClient('rtmp://127.0.0.1:1935/live')
            c._bg_run = True
            c._recv_loop()
        return c

    def test_initial_sleep_is_half_second(self, no_open_client):
        sleeps = []
        # monotonic: [deadline setup=0, loop check 1=0, loop check 2=200 (past deadline)]
        self._run(
            no_open_client,
            lambda av: setattr(av, 'open', MagicMock(side_effect=ConnectionError('refused'))),
            [0, 0, 200],
            sleeps,
        )
        assert sleeps == [0.5]

    def test_delay_doubles_on_each_failure(self, no_open_client):
        sleeps = []
        # 1 setup call + 4 True loop checks + 1 False
        self._run(
            no_open_client,
            lambda av: setattr(av, 'open', MagicMock(side_effect=ConnectionError('refused'))),
            [0, 0, 0, 0, 0, 200],
            sleeps,
        )
        assert sleeps == [0.5, 1.0, 2.0, 4.0]

    def test_delay_caps_at_30_seconds(self, no_open_client):
        sleeps = []
        # 7 failures: 0.5→1→2→4→8→16→30 (min(32,30)=30 on 7th)
        # 1 setup + 7 True + 1 False = 9 monotonic calls
        self._run(
            no_open_client,
            lambda av: setattr(av, 'open', MagicMock(side_effect=ConnectionError('refused'))),
            [0] + [0] * 7 + [200],
            sleeps,
        )
        assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0]

    def test_delay_resets_after_successful_connect(self, no_open_client):
        # 2 failures raise delay to 2.0; then a stream that ends immediately
        # resets delay to 0.5; the stream-end sleep proves the reset.
        sleeps = []
        mock_stream = MagicMock()
        mock_stream.type = 'video'
        mock_container = MagicMock()
        mock_container.streams = [mock_stream]
        mock_container.decode.return_value = iter([])  # stream ends at once

        def _cfg(av):
            av.open.side_effect = [
                ConnectionError('refused'),
                ConnectionError('refused'),
                mock_container,
            ]

        # 1 setup + 2 fail checks + 1 success check + 1 past-deadline check
        self._run(no_open_client, _cfg, [0, 0, 0, 0, 200], sleeps)

        # sleeps: fail→0.5, fail→1.0, stream-end→0.5 (reset confirmed)
        assert sleeps == [0.5, 1.0, 0.5]

    def test_deadline_stops_loop_immediately(self, no_open_client):
        sleeps = []
        # deadline = 0+120=120; first loop check returns 200 → never enters body
        c = self._run(
            no_open_client,
            lambda av: setattr(av, 'open', MagicMock(side_effect=ConnectionError('refused'))),
            [0, 200],
            sleeps,
        )
        assert sleeps == []
        assert not c._bg_run

    def test_bg_run_false_after_deadline_expires(self, no_open_client):
        sleeps = []
        # After deadline, _recv_loop sets _bg_run = False (line 188)
        c = self._run(
            no_open_client,
            lambda av: setattr(av, 'open', MagicMock(side_effect=ConnectionError('refused'))),
            [0, 0, 200],
            sleeps,
        )
        assert not c._bg_run

    def test_no_video_stream_breaks_without_retry(self, no_open_client):
        sleeps = []
        mock_container = MagicMock()
        mock_container.streams = []  # no video streams

        c = self._run(
            no_open_client,
            lambda av: setattr(av.open, 'return_value', mock_container),
            [0, 0],
            sleeps,
        )
        assert sleeps == []   # break with no retry sleep
        assert not c._bg_run  # loop exited cleanly


# ---------------------------------------------------------------------------
# Integration: RTMPPublisher → mediamtx → RTMPClient round-trip
# ---------------------------------------------------------------------------

@requires_mediamtx
@requires_av
class TestMediamtxRoundTrip:
    """Push via RTMP to mediamtx, read back via RTMP."""

    def test_publisher_connects(self, mediamtx_rtmp_server, synthetic_frames):
        pub_uri = mediamtx_rtmp_server['rtmp'] + '/live/pub_test'
        with RTMPPublisher(pub_uri, fps=5, frame_buffer=synthetic_frames[:3]) as pub:
            time.sleep(2.0)  # let the encode loop attempt the connection
            assert pub.isOpened(), 'RTMPPublisher failed to connect'

    def test_client_receives_frame(self, mediamtx_rtmp_server, synthetic_frames):
        pub_uri = mediamtx_rtmp_server['rtmp'] + '/live/roundtrip'

        received = None
        with RTMPPublisher(pub_uri, fps=5, frame_buffer=synthetic_frames[:3]) as pub:
            assert pub.isOpened(), 'RTMPPublisher failed to connect'
            # RTMPClient retries internally with exponential backoff, so a
            # single client with a generous outer deadline is sufficient.
            with RTMPClient(pub_uri) as client:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    received = client.read()
                    if received is not None:
                        break
                    time.sleep(0.1)

        assert received is not None, 'RTMPClient received no frame'
        assert isinstance(received, Image.Image)


# ---------------------------------------------------------------------------
# ImportError when PyAV is absent
# ---------------------------------------------------------------------------

class TestNoAV:

    def test_rtmpclient_raises_without_av(self):
        with patch('rtsp.rtmp._av', None):
            with pytest.raises(ImportError, match='PyAV'):
                RTMPClient('rtmp://127.0.0.1/live')

    def test_rtmppublisher_raises_without_av(self):
        with patch('rtsp.rtmp._av', None):
            with pytest.raises(ImportError, match='PyAV'):
                RTMPPublisher('rtmp://127.0.0.1/live', size=(64, 64))


# ---------------------------------------------------------------------------
# RTMPClient.read() with actual frame data
# ---------------------------------------------------------------------------

class TestRTMPClientRead:

    def test_read_returns_pil_image(self, no_open_client):
        c = RTMPClient('rtmp://127.0.0.1/live')
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        with c._lock:
            c._queue = arr
        frame = c.read()
        assert isinstance(frame, Image.Image)

    def test_read_raw_returns_ndarray(self, no_open_client):
        c = RTMPClient('rtmp://127.0.0.1/live')
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        with c._lock:
            c._queue = arr
        raw = c.read(raw=True)
        assert isinstance(raw, np.ndarray)


# ---------------------------------------------------------------------------
# RTMPClient._recv_loop internals (frame decode paths)
# ---------------------------------------------------------------------------

class TestRTMPClientRecvLoop:
    """Call _recv_loop() directly in the test thread with all I/O mocked."""

    def _make_client(self, no_open_client):
        return RTMPClient('rtmp://127.0.0.1/live')

    def _video_container(self, frames=()):
        mock_stream = MagicMock()
        mock_stream.type = 'video'
        container = MagicMock()
        container.streams = [mock_stream]
        container.decode.return_value = iter(frames)
        return container, mock_stream

    def test_frame_stored_in_queue(self, no_open_client):
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        mock_frame = MagicMock()
        mock_frame.to_ndarray.return_value = arr
        container, _ = self._video_container([mock_frame])

        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep'), \
             patch('rtsp.rtmp.time.monotonic', side_effect=iter([0, 0, 200])):
            mock_av.open.return_value = container
            c = self._make_client(no_open_client)
            c._bg_run = True
            c._recv_loop()

        assert c._queue is arr

    def test_verbose_sets_resolution(self, no_open_client):
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        mock_frame = MagicMock()
        mock_frame.to_ndarray.return_value = arr
        container, _ = self._video_container([mock_frame])

        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep'), \
             patch('rtsp.rtmp.time.monotonic', side_effect=iter([0, 0, 200])):
            mock_av.open.return_value = container
            c = RTMPClient('rtmp://127.0.0.1/live', verbose=True)
            c._bg_run = True
            c._recv_loop()

        assert c._width == 160
        assert c._height == 120

    def test_decode_exception_triggers_retry(self, no_open_client):
        container, _ = self._video_container()
        container.decode.side_effect = RuntimeError('decode error')
        sleeps = []

        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep', side_effect=sleeps.append), \
             patch('rtsp.rtmp.time.monotonic', side_effect=iter([0, 0, 200])):
            mock_av.open.return_value = container
            c = self._make_client(no_open_client)
            c._bg_run = True
            c._recv_loop()

        assert sleeps == [0.5]
        assert not c._bg_run

    def test_bg_run_false_skips_frame_processing(self, no_open_client):
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        mock_frame = MagicMock()
        mock_frame.to_ndarray.return_value = arr

        c = self._make_client(no_open_client)

        container, _ = self._video_container()

        def frames_then_stop(stream):
            c._bg_run = False
            yield mock_frame

        container.decode.side_effect = frames_then_stop

        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep'), \
             patch('rtsp.rtmp.time.monotonic', side_effect=iter([0, 0])):
            mock_av.open.return_value = container
            c._bg_run = True
            c._recv_loop()

        assert c._queue is None


# ---------------------------------------------------------------------------
# RTMPPublisher additional lifecycle tests
# ---------------------------------------------------------------------------

class TestRTMPPublisherExtra:

    def test_context_manager_calls_close(self, no_open_publisher):
        with RTMPPublisher('rtmp://127.0.0.1/live') as p:
            pass
        assert not p.isOpened()

    def test_put_numpy_array(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1/live')
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        p.put(arr)
        assert isinstance(p._buffer[0], Image.Image)

    def test_close_flushes_stream(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1/live')
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_pkt = MagicMock()
        mock_stream.encode.return_value = [mock_pkt]
        p._container = mock_container
        p._stream = mock_stream
        p.close()
        mock_stream.encode.assert_called_once_with()
        mock_container.mux.assert_called_once_with(mock_pkt)
        mock_container.close.assert_called_once()

    def test_close_handles_flush_exception(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1/live')
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.encode.return_value = []
        mock_container.close.side_effect = RuntimeError('close failed')
        p._container = mock_container
        p._stream = mock_stream
        p.close()  # must not raise

    def test_frame_buffer_loads_all_frames(self, no_open_publisher):
        frames = [Image.new('RGB', (64, 64), color=(i * 40, 100, 80)) for i in range(4)]
        p = RTMPPublisher('rtmp://127.0.0.1/live', frame_buffer=frames)
        if p._loader:
            p._loader.join(timeout=2)
        assert len(p._buffer) == 4

    def test_empty_frame_buffer_no_loader(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1/live', frame_buffer=iter([]))
        assert p._loader is None

    def test_open_noop_when_size_not_set(self):
        with patch('rtsp.rtmp.Thread') as mock_thread:
            p = RTMPPublisher('rtmp://127.0.0.1/live')
        p.open()  # _size is None → early return
        mock_thread.assert_not_called()

    def test_open_verbose_logs_uri(self, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger='rtsp.rtmp'), \
             patch('rtsp.rtmp.Thread'), \
             patch('rtsp.rtmp._av', MagicMock()):
            RTMPPublisher('rtmp://127.0.0.1/live', verbose=True, size=(64, 64))
        assert any('127.0.0.1' in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# RTMPPublisher._encode_loop internals
# ---------------------------------------------------------------------------

class TestRTMPEncodeLoop:
    """Run _encode_loop() directly with all I/O mocked."""

    def _setup(self, mock_av, mock_stream, size=(64, 64), fps=10):
        mock_container = MagicMock()
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container
        mock_frame = MagicMock()
        mock_av.VideoFrame.from_ndarray.return_value = mock_frame
        mock_frame.reformat.return_value = mock_frame
        return mock_container

    def test_encode_loop_sleeps_on_empty_buffer(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1/live', fps=10, size=(64, 64))
        sleep_calls = []

        def mock_sleep(s):
            sleep_calls.append(s)
            if len(sleep_calls) >= 2:
                p._bg_run = False

        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep', side_effect=mock_sleep), \
             patch('rtsp.rtmp.time.monotonic', return_value=0.0):
            self._setup(mock_av, MagicMock())
            p._bg_run = True
            p._encode_loop()

        assert 0.05 in sleep_calls
        assert not p._bg_run

    def test_encode_loop_encodes_frame(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1/live', fps=10, size=(64, 64))
        p.put(Image.new('RGB', (64, 64)))
        mock_stream = MagicMock()
        mock_pkt = MagicMock()

        def stop_after_encode(frame):
            p._bg_run = False
            return [mock_pkt]

        mock_stream.encode.side_effect = stop_after_encode

        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep'), \
             patch('rtsp.rtmp.time.monotonic', return_value=0.0):
            mock_container = self._setup(mock_av, mock_stream)
            p._bg_run = True
            p._encode_loop()

        mock_stream.encode.assert_called_once()
        mock_container.mux.assert_called_once_with(mock_pkt)

    def test_encode_loop_stops_on_mux_error(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1/live', fps=10, size=(64, 64))
        p.put(Image.new('RGB', (64, 64)))
        mock_stream = MagicMock()
        mock_stream.encode.side_effect = RuntimeError('mux error')

        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep'), \
             patch('rtsp.rtmp.time.monotonic', return_value=0.0):
            self._setup(mock_av, mock_stream)
            p._bg_run = True
            p._encode_loop()

        assert not p._bg_run

    def test_encode_loop_exits_when_container_cleared(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1/live', fps=10, size=(64, 64))
        p.put(Image.new('RGB', (64, 64)))
        mock_stream = MagicMock()

        def clear_container(frame):
            p._container = None
            return []

        mock_stream.encode.side_effect = clear_container

        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep'), \
             patch('rtsp.rtmp.time.monotonic', return_value=0.0):
            self._setup(mock_av, mock_stream)
            p._bg_run = True
            p._encode_loop()

        assert not p._bg_run

    def test_encode_loop_closes_container_on_setup_failure(self, no_open_publisher):
        p = RTMPPublisher('rtmp://127.0.0.1/live', fps=10, size=(64, 64))
        mock_container = MagicMock()
        mock_container.add_stream.side_effect = RuntimeError('stream setup failed')

        with patch('rtsp.rtmp._av') as mock_av, \
             patch('rtsp.rtmp.time.sleep'), \
             patch('rtsp.rtmp.time.monotonic', return_value=0.0):
            mock_av.open.return_value = mock_container
            p._bg_run = True
            p._encode_loop()

        mock_container.close.assert_called_once()
        assert not p._bg_run


# ---------------------------------------------------------------------------
# RTMPClient.preview() with mocked tkinter
# ---------------------------------------------------------------------------

class TestPreview:

    def _mock_tk(self):
        mock_root = MagicMock()
        mock_label = MagicMock()
        mock_tk = MagicMock()
        mock_tk.Tk.return_value = mock_root
        mock_tk.Label.return_value = mock_label
        return mock_tk, mock_root, mock_label

    def test_preview_exits_immediately_when_not_running(self, no_open_client):
        c = RTMPClient('rtmp://127.0.0.1/live')
        mock_tk, mock_root, _ = self._mock_tk()

        with patch.dict(sys.modules, {'tkinter': mock_tk, 'PIL.ImageTk': MagicMock()}):
            c.preview()

        mock_root.destroy.assert_called_once()
        mock_root.mainloop.assert_called_once()

    def test_preview_updates_label_when_frame_available(self, no_open_client):
        c = RTMPClient('rtmp://127.0.0.1/live')
        c._queue = np.zeros((120, 160, 3), dtype=np.uint8)
        c._bg_run = True
        mock_tk, mock_root, mock_label = self._mock_tk()

        with patch.dict(sys.modules, {'tkinter': mock_tk, 'PIL.ImageTk': MagicMock()}):
            c.preview()

        mock_label.config.assert_called()
        mock_root.after.assert_called()
