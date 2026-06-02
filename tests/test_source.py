from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from rtsp import Source
from rtsp._utils import _to_pil
from rtsp.source import Publisher


@pytest.fixture
def no_open(monkeypatch):
    """Prevent Publisher.open() from connecting to a relay."""
    monkeypatch.setattr(Publisher, 'open', lambda self: self)


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
            Source(0, serve=False)

    def test_numeric_string_raises(self):
        with pytest.raises(ValueError, match="network address"):
            Source('0', serve=False)

    def test_valid_rtsp_uri(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        assert src._uri == 'rtsp://localhost:8554/live'

    def test_bare_host_defaults_to_rtsp(self):
        src = Source('localhost:8554/live', serve=False)
        assert src._uri.startswith('rtsp://')


# --- Source size parameter ---

class TestSourceSize:

    def test_size_sets_resolution(self, no_open):
        src = Source('rtsp://0.0.0.0:8554/live', size=(640, 480), serve=False)
        assert src._size == (640, 480)

    def test_size_snaps_odd_dimensions_to_even(self, no_open):
        src = Source('rtsp://0.0.0.0:8554/live', size=(641, 479), serve=False)
        assert src._size == (640, 478)

    def test_size_calls_open_immediately(self, no_open):
        called = []
        with patch.object(Publisher, 'open', lambda self: called.append(1) or self):
            src = Source('rtsp://0.0.0.0:8554/live', size=(320, 240), serve=False)
        assert called

    def test_size_resizes_frames_on_put(self, no_open):
        src = Source('rtsp://0.0.0.0:8554/live', size=(320, 240), serve=False)
        src.put(Image.new('RGB', (1920, 1080)))
        assert src._buffer[0].size == (320, 240)


# --- Source serve mode ---

class TestSourceServe:

    def test_serve_true_returns_source_impl(self):
        import rtsp.source as _src
        src = Source('rtsp://0.0.0.0:8554/live')
        assert isinstance(src, _src.Source)

    def test_serve_true_explicit_returns_source_impl(self):
        import rtsp.source as _src
        src = Source('rtsp://0.0.0.0:8554/live', serve=True)
        assert isinstance(src, _src.Source)

    def test_serve_false_returns_publisher(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        assert isinstance(src, Publisher)

    def test_serve_false_not_source_impl(self):
        import rtsp.source as _src
        src = Source('rtsp://localhost:8554/live', serve=False)
        assert not isinstance(src, _src.Source)


# --- Source frame rate ---

class TestSourceFrameRate:

    def test_fps_stored(self):
        src = Source('rtsp://0.0.0.0:8554/live', fps=15, serve=False)
        assert src._fps == 15

    def test_low_fps_stored(self):
        src = Source('rtsp://0.0.0.0:8554/live', fps=0.5, serve=False)
        assert src._fps == 0.5


# --- Source.put() ---

class TestSourcePut:

    def _make_source(self):
        src = Source('rtsp://localhost:8554/live', fps=25, serve=False)
        src.open = lambda: src
        return src

    def test_put_sets_size_from_first_frame(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        img = Image.new('RGB', (320, 240))
        src.put(img)
        assert src._size == (320, 240)

    def test_put_resizes_subsequent_frames(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        src.put(Image.new('RGB', (320, 240)))
        large = Image.new('RGB', (1920, 1080))
        src.put(large)
        assert src._buffer[-1].size == (320, 240)

    def test_put_accepts_numpy(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        arr = np.zeros((240, 320, 3), dtype=np.uint8)
        src.put(arr)
        assert src._size == (320, 240)

    def test_put_accepts_file_path(self, tmp_path):
        img = Image.new('RGB', (64, 64))
        p = tmp_path / 'f.png'
        img.save(str(p))
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        src.put(str(p))
        assert src._size == (64, 64)

    def test_buffer_grows_with_each_put(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        for _ in range(5):
            src.put(Image.new('RGB', (64, 64)))
        assert len(src._buffer) == 5


# --- Source frame_buffer= constructor parameter ---

class TestSourceFramesParam:

    @pytest.fixture(autouse=True)
    def _no_open(self, monkeypatch):
        monkeypatch.setattr(Publisher, 'open', lambda self: self)

    def _make(self, frames):
        return Source('rtsp://0.0.0.0:8554/live', frame_buffer=frames, serve=False)

    def test_list_of_pil_images(self):
        imgs = [Image.new('RGB', (32, 32), color=(i * 80, 0, 0)) for i in range(3)]
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=imgs, serve=False)
        src.open = lambda: src
        # open is stubbed but put() already ran - check buffer directly
        src2 = Source('rtsp://0.0.0.0:8554/live', serve=False)
        src2.open = lambda: src2
        for img in imgs:
            src2.put(img)
        assert len(src._buffer) == len(src2._buffer) == 3

    def test_tuple_of_images(self):
        imgs = tuple(Image.new('RGB', (8, 8)) for _ in range(4))
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=imgs, serve=False)
        src.open = lambda: src
        assert len(src._buffer) == 4

    def test_generator_expression(self):
        src = Source('rtsp://0.0.0.0:8554/live', serve=False,
                     frame_buffer=(Image.new('RGB', (8, 8)) for _ in range(5)))
        src.open = lambda: src
        if src._loader:
            src._loader.join(timeout=5)
        assert len(src._buffer) == 5

    def test_pathlib_glob(self, tmp_path):
        for i in range(3):
            Image.new('RGB', (8, 8), color=(i * 80, 0, 0)).save(tmp_path / f'{i}.png')
        src = Source('rtsp://0.0.0.0:8554/live', serve=False,
                     frame_buffer=sorted(tmp_path.glob('*.png')))
        src.open = lambda: src
        if src._loader:
            src._loader.join(timeout=5)
        assert len(src._buffer) == 3

    def test_size_inferred_from_first_frame(self):
        imgs = [Image.new('RGB', (160, 120)) for _ in range(2)]
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=imgs, serve=False)
        src.open = lambda: src
        assert src._size == (160, 120)

    def test_none_leaves_buffer_empty(self):
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=None, serve=False)
        assert len(src._buffer) == 0

    def test_numpy_arrays_in_iterable(self):
        arrays = [np.zeros((16, 16, 3), dtype=np.uint8) for _ in range(2)]
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=arrays, serve=False)
        src.open = lambda: src
        assert len(src._buffer) == 2


# --- Source.serve_forever() ---

class TestServeForever:

    def test_raises_for_push_mode(self):
        """serve_forever() is not supported for serve=False (push to relay)."""
        src = Source('rtsp://localhost:8554/live', serve=False)
        with pytest.raises(RuntimeError, match='serve=True'):
            src.serve_forever()
