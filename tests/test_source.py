from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from rtsp.ffmpegstream import Source, _to_pil


# --- _to_pil helper ---

class TestToPil:

    def test_pil_passthrough(self):
        img = Image.new('RGB', (64, 64), color=(255, 0, 0))
        assert _to_pil(img) is img

    def test_numpy_array(self):
        arr = np.zeros((32, 64, 3), dtype=np.uint8)
        result = _to_pil(arr)
        assert isinstance(result, Image.Image)
        assert result.size == (64, 32)  # PIL size is (width, height)

    def test_str_path(self, tmp_path):
        img = Image.new('RGB', (16, 16), color=(0, 128, 255))
        p = tmp_path / 'frame.png'
        img.save(str(p))
        result = _to_pil(str(p))
        assert isinstance(result, Image.Image)
        assert result.size == (16, 16)

    def test_pathlib_path(self, tmp_path):
        img = Image.new('RGB', (16, 16), color=(0, 128, 255))
        p = tmp_path / 'frame.png'
        img.save(p)
        result = _to_pil(p)
        assert isinstance(result, Image.Image)
        assert result.size == (16, 16)

    def test_invalid_type_raises(self):
        with pytest.raises(TypeError):
            _to_pil(42)


# --- Source URI validation ---

class TestSourceUri:

    def test_device_uri_raises(self):
        with pytest.raises(ValueError, match="network address"):
            Source(0)

    def test_numeric_string_raises(self):
        with pytest.raises(ValueError, match="network address"):
            Source('0')

    def test_picam_raises(self):
        with pytest.raises(ValueError, match="network address"):
            Source('picam')

    def test_valid_rtsp_uri(self):
        src = Source('rtsp://localhost:8554/live')
        assert src._uri == 'rtsp://localhost:8554/live'

    def test_bare_host_defaults_to_rtsp(self):
        src = Source('localhost:8554/live')
        assert src._uri.startswith('rtsp://')


# --- Source size parameter ---

class TestSourceSize:

    def test_size_sets_resolution(self):
        from unittest.mock import patch, MagicMock
        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=MagicMock()):
            src = Source('rtsp://0.0.0.0:8554/live', size=(640, 480))
        assert src._size == (640, 480)

    def test_size_snaps_odd_dimensions_to_even(self):
        from unittest.mock import patch, MagicMock
        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=MagicMock()):
            src = Source('rtsp://0.0.0.0:8554/live', size=(641, 479))
        assert src._size == (640, 478)

    def test_size_starts_ffmpeg_immediately(self):
        from unittest.mock import patch, MagicMock
        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=MagicMock()) as mock_popen:
            src = Source('rtsp://0.0.0.0:8554/live', size=(320, 240))
        assert mock_popen.called

    def test_size_resizes_frames_on_put(self):
        from unittest.mock import patch, MagicMock
        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=MagicMock()):
            src = Source('rtsp://0.0.0.0:8554/live', size=(320, 240))
        src.put(Image.new('RGB', (1920, 1080)))
        assert src._buffer[0].size == (320, 240)


# --- Source serve mode ---

class TestSourceServe:

    def test_serve_is_true_by_default(self):
        src = Source('rtsp://0.0.0.0:8554/live')
        assert src._serve is True

    def test_serve_false_stores_flag(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        assert src._serve is False

    def test_serve_true_uses_tcp_listen(self):
        from unittest.mock import patch, MagicMock
        src = Source('rtsp://0.0.0.0:8554/live', fps=5)
        src._size = (64, 64)
        with patch('rtsp.ffmpegstream.subprocess.Popen') as mock_popen:
            mock_popen.return_value = MagicMock()
            src.open()
        cmd = mock_popen.call_args[0][0]
        assert '-f' in cmd
        assert 'mpegts' in cmd
        assert any('tcp://' in arg and 'listen=1' in arg for arg in cmd)

    def test_serve_false_uses_rtsp_push(self):
        from unittest.mock import patch, MagicMock
        src = Source('rtsp://localhost:8554/live', fps=5, serve=False)
        src._size = (64, 64)
        with patch('rtsp.ffmpegstream.subprocess.Popen') as mock_popen:
            mock_popen.return_value = MagicMock()
            src.open()
        cmd = mock_popen.call_args[0][0]
        assert 'rtsp' in cmd
        assert not any('listen=1' in arg for arg in cmd)


# --- Source frame rate ---

class TestSourceFrameRate:

    def _open_cmd(self, fps):
        from unittest.mock import patch, MagicMock
        src = Source('rtsp://0.0.0.0:8554/live', fps=fps)
        src._size = (64, 64)
        with patch('rtsp.ffmpegstream.subprocess.Popen') as mock_popen:
            mock_popen.return_value = MagicMock()
            src.open()
        cmd = mock_popen.call_args[0][0]
        return cmd

    def test_normal_fps_passed_to_ffmpeg(self):
        cmd = self._open_cmd(fps=25)
        r_idx = cmd.index('-r')
        assert float(cmd[r_idx + 1]) == 25

    def test_low_fps_clamps_encode_rate_to_min(self):
        from rtsp.ffmpegstream import _MIN_ENCODE_FPS
        cmd = self._open_cmd(fps=0.05)
        r_idx = cmd.index('-r')
        assert float(cmd[r_idx + 1]) == _MIN_ENCODE_FPS

    def test_low_fps_repeats_same_frame(self):
        """At fps=0.1 a single buffer frame should be written multiple times."""
        import time
        from unittest.mock import patch, MagicMock

        src = Source('rtsp://0.0.0.0:8554/live', fps=0.1)   # advance every 10 s
        src._size = (4, 4)

        written = []
        mock_proc = MagicMock()
        mock_proc.stdin.write = lambda b: written.append(b)
        mock_proc.stdin.flush = lambda: None

        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=mock_proc):
            src.open()

        with src._lock:
            src._buffer.append(Image.new('RGB', (4, 4), color=(255, 0, 0)))

        time.sleep(2.5)   # encode_fps=1, so ~2 writes; advance not due until 10 s
        src.close()

        assert len(written) >= 2          # frame was repeated
        assert len(set(written)) == 1     # only one unique frame so far

    def test_feed_advances_index_at_display_rate(self):
        """At fps=0.5, buffer index should advance after ~2 s, not every encode tick."""
        import time
        from unittest.mock import patch, MagicMock

        src = Source('rtsp://0.0.0.0:8554/live', fps=0.5)   # advance every 2 s
        src._size = (4, 4)

        written = []
        mock_proc = MagicMock()
        mock_proc.stdin.write = lambda b: written.append(b)
        mock_proc.stdin.flush = lambda: None

        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=mock_proc):
            src.open()

        frame_a = Image.new('RGB', (4, 4), color=(255, 0, 0))
        frame_b = Image.new('RGB', (4, 4), color=(0, 255, 0))
        with src._lock:
            src._buffer.extend([frame_a, frame_b])

        # First encode tick at t≈1 s writes frame_a; advance not due until t≈2 s
        time.sleep(1.5)
        unique_before = len(set(written))

        # Past the advance interval — frame_b should now appear
        time.sleep(1.5)
        unique_after = len(set(written))

        src.close()

        assert unique_before == 1, "only frame_a expected before advance"
        assert unique_after == 2, "frame_b expected after advance"


# --- Source.put() ---

class TestSourcePut:

    def _make_source(self):
        src = Source('rtsp://localhost:8554/live', fps=25)
        src.open = lambda: src   # prevent actual FFmpeg launch
        return src

    def test_put_sets_size_from_first_frame(self):
        src = Source('rtsp://localhost:8554/live')
        src.open = lambda: src
        img = Image.new('RGB', (320, 240))
        src.put(img)
        assert src._size == (320, 240)

    def test_put_resizes_subsequent_frames(self):
        src = Source('rtsp://localhost:8554/live')
        src.open = lambda: src
        src.put(Image.new('RGB', (320, 240)))
        large = Image.new('RGB', (1920, 1080))
        src.put(large)
        assert src._buffer[-1].size == (320, 240)

    def test_put_accepts_numpy(self):
        src = Source('rtsp://localhost:8554/live')
        src.open = lambda: src
        arr = np.zeros((240, 320, 3), dtype=np.uint8)
        src.put(arr)
        assert src._size == (320, 240)

    def test_put_accepts_file_path(self, tmp_path):
        img = Image.new('RGB', (64, 64))
        p = tmp_path / 'f.png'
        img.save(str(p))
        src = Source('rtsp://localhost:8554/live')
        src.open = lambda: src
        src.put(str(p))
        assert src._size == (64, 64)

    def test_buffer_grows_with_each_put(self):
        src = Source('rtsp://localhost:8554/live')
        src.open = lambda: src
        for _ in range(5):
            src.put(Image.new('RGB', (64, 64)))
        assert len(src._buffer) == 5


# --- Source frame_buffer= constructor parameter ---

class TestSourceFramesParam:

    @pytest.fixture(autouse=True)
    def _patch_popen(self):
        from unittest.mock import patch, MagicMock
        with patch('rtsp.ffmpegstream.subprocess.Popen', return_value=MagicMock()):
            yield

    def _make(self, frames):
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=frames)
        src.open = lambda: src   # prevent FFmpeg launch
        return src

    def test_list_of_pil_images(self):
        imgs = [Image.new('RGB', (32, 32), color=(i * 80, 0, 0)) for i in range(3)]
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=imgs)
        src.open = lambda: src
        # open is stubbed but put() already ran — check buffer directly
        src2 = Source('rtsp://0.0.0.0:8554/live')
        src2.open = lambda: src2
        for img in imgs:
            src2.put(img)
        assert len(src._buffer) == len(src2._buffer) == 3

    def test_tuple_of_images(self):
        imgs = tuple(Image.new('RGB', (8, 8)) for _ in range(4))
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=imgs)
        src.open = lambda: src
        assert len(src._buffer) == 4

    def test_generator_expression(self):
        src = Source('rtsp://0.0.0.0:8554/live',
                     frame_buffer=(Image.new('RGB', (8, 8)) for _ in range(5)))
        src.open = lambda: src
        if src._loader:
            src._loader.join(timeout=5)
        assert len(src._buffer) == 5

    def test_pathlib_glob(self, tmp_path):
        for i in range(3):
            Image.new('RGB', (8, 8), color=(i * 80, 0, 0)).save(tmp_path / f'{i}.png')
        src = Source('rtsp://0.0.0.0:8554/live',
                     frame_buffer=sorted(tmp_path.glob('*.png')))
        src.open = lambda: src
        if src._loader:
            src._loader.join(timeout=5)
        assert len(src._buffer) == 3

    def test_size_inferred_from_first_frame(self):
        imgs = [Image.new('RGB', (160, 120)) for _ in range(2)]
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=imgs)
        src.open = lambda: src
        assert src._size == (160, 120)

    def test_none_leaves_buffer_empty(self):
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=None)
        assert len(src._buffer) == 0

    def test_numpy_arrays_in_iterable(self):
        arrays = [np.zeros((16, 16, 3), dtype=np.uint8) for _ in range(2)]
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=arrays)
        src.open = lambda: src
        assert len(src._buffer) == 2


# --- Source.serve_forever() ---

class TestServeForever:

    def test_raises_when_serve_false(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        with pytest.raises(RuntimeError, match='serve=True'):
            src.serve_forever()

    def test_raises_before_any_frame_added(self):
        src = Source('rtsp://0.0.0.0:8554/live', serve=True)
        with pytest.raises(RuntimeError, match='frame_buffer'):
            src.serve_forever()

    def test_restarts_after_disconnect(self):
        """serve_forever() calls close() + open() each time isOpened() goes False."""
        import threading

        src = Source('rtsp://0.0.0.0:8554/live', serve=True)
        src._size = (8, 8)
        with src._lock:
            src._buffer.append(Image.new('RGB', (8, 8)))

        open_calls = []
        close_calls = []

        def fake_open():
            open_calls.append(1)
            src._bg_run = True   # simulate "running"
            return src

        def fake_close():
            close_calls.append(1)
            src._bg_run = False

        src.open = fake_open
        src.close = fake_close
        src._bg_run = True   # start as "opened"

        cycle = 0

        original_isOpened = src.isOpened.__func__

        def toggling_isOpened():
            nonlocal cycle
            cycle += 1
            # appear closed on cycles 3, 4 (triggering one restart), then raise to exit
            if cycle > 6:
                raise KeyboardInterrupt
            return cycle not in (3, 4)

        src.isOpened = toggling_isOpened

        t = threading.Thread(target=src.serve_forever)
        t.start()
        t.join(timeout=5)

        assert not t.is_alive(), "serve_forever() did not exit on KeyboardInterrupt"
        assert len(open_calls) >= 1
        assert len(close_calls) >= 1
