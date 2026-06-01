"""Client tests using a mocked FFmpeg process.

stdout is a real OS pipe so _update blocks naturally (no timing hacks).
stderr is a BytesIO preloaded with a fake stream-info line so dimension
parsing completes immediately.  No external RTSP server is required.
"""
import io
import os
import time
import threading
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

import rtsp


_W, _H = 320, 240
_FRAME = bytes([100, 150, 200] * (_W * _H))   # solid RGB colour, one frame

_STDERR = (
    b'ffmpeg version test\n'
    b'Input #0, rtsp, from rtsp://127.0.0.1:18554/test:\n'
    b'    Stream #0:0: Video: h264, yuv420p, 320x240 [SAR 1:1 DAR 4:3]\n'
)


@pytest.fixture
def mock_ffmpeg():
    """Mock subprocess.Popen result with a live stdout pipe and fake stderr."""
    r_fd, w_fd = os.pipe()

    def _writer():
        try:
            while True:
                os.write(w_fd, _FRAME)
                time.sleep(0.02)          # ~50 fps, keeps _update alive
        except OSError:
            pass                          # pipe closed by terminate()

    threading.Thread(target=_writer, daemon=True).start()

    proc = MagicMock()
    proc.stdout = os.fdopen(r_fd, 'rb')
    proc.stderr = io.BytesIO(_STDERR)

    def _terminate():
        try:
            os.close(w_fd)
        except OSError:
            pass

    proc.terminate = _terminate
    yield proc
    _terminate()    # cleanup if test didn't close


@pytest.fixture
def client(mock_ffmpeg):
    with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=mock_ffmpeg):
        c = rtsp.Client('rtsp://127.0.0.1:18554/test')
    time.sleep(0.1)     # let _update populate _queue with at least one frame
    yield c
    c.close()


class TestClientState:

    def test_opens(self, client):
        assert client.isOpened()

    def test_context_manager_closes_on_exit(self, mock_ffmpeg):
        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=mock_ffmpeg):
            with rtsp.Client('rtsp://127.0.0.1:18554/test') as c:
                assert c.isOpened()
        assert not c.isOpened()

    def test_close_stops_stream(self, client):
        client.close()
        assert not client.isOpened()

    def test_close_is_idempotent(self, client):
        client.close()
        client.close()      # must not raise
        assert not client.isOpened()


class TestClientRead:

    def test_read_returns_pil_image(self, client):
        frame = client.read()
        assert isinstance(frame, Image.Image)

    def test_read_frame_dimensions(self, client):
        assert client.read().size == (_W, _H)

    def test_read_raw_returns_numpy_array(self, client):
        frame = client.read(raw=True)
        assert isinstance(frame, np.ndarray)
        assert frame.dtype == np.uint8

    def test_read_raw_shape_is_height_width_channels(self, client):
        assert client.read(raw=True).shape == (_H, _W, 3)

    def test_read_raw_pixel_values_match_source(self, client):
        frame = client.read(raw=True)
        # _FRAME is solid [100, 150, 200] repeated — check a corner pixel
        np.testing.assert_array_equal(frame[0, 0], [100, 150, 200])

    def test_read_before_first_frame_returns_none(self, mock_ffmpeg):
        # Create client but don't sleep — _queue may still be empty
        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=mock_ffmpeg):
            c = rtsp.Client('rtsp://127.0.0.1:18554/test')
        result = c.read()
        c.close()
        assert result is None or isinstance(result, Image.Image)


class TestClientDimensions:

    def test_dimensions_parsed_from_stderr(self, client):
        assert client._width == _W
        assert client._height == _H

    def test_no_dims_in_stderr_raises(self):
        """Client must raise if FFmpeg never reports video dimensions."""
        r_fd, w_fd = os.pipe()
        proc = MagicMock()
        proc.stdout = os.fdopen(r_fd, 'rb')
        proc.stderr = io.BytesIO(b'some stderr with no video info\n')
        proc.terminate = lambda: os.close(w_fd)

        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=proc):
            with pytest.raises(RuntimeError, match='Timed out'):
                rtsp.Client('rtsp://127.0.0.1:18554/test')
        try:
            os.close(w_fd)
        except OSError:
            pass


class TestClientCommand:

    def test_rtsp_transport_flag_present(self, mock_ffmpeg):
        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=mock_ffmpeg) as mock_popen:
            c = rtsp.Client('rtsp://127.0.0.1:18554/test')
            c.close()
        cmd = mock_popen.call_args[0][0]
        assert '-rtsp_transport' in cmd
        assert 'tcp' in cmd

    def test_output_is_rgb24_rawvideo(self, mock_ffmpeg):
        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=mock_ffmpeg) as mock_popen:
            c = rtsp.Client('rtsp://127.0.0.1:18554/test')
            c.close()
        cmd = mock_popen.call_args[0][0]
        assert 'rgb24' in cmd
        assert 'rawvideo' in cmd
