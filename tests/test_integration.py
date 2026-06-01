"""Source + Client round-trip tests via mediamtx.

Skipped automatically when mediamtx or ffmpeg is not on PATH.
Run mediamtx before this suite or install it: https://github.com/bluenviron/mediamtx
"""
import time

import numpy as np
import pytest
from PIL import Image

import rtsp
from conftest import requires_ffmpeg, requires_mediamtx, wait_for_frame


@requires_ffmpeg
@requires_mediamtx
class TestSourceToClient:

    def test_source_opens_after_first_put(self, mediamtx_server):
        uri = mediamtx_server + '/open'
        with rtsp.Source(uri, serve=False, fps=5) as src:
            src.put(Image.new('RGB', (320, 240)))
            time.sleep(0.5)
            assert src.isOpened()

    def test_source_context_manager_closes(self, mediamtx_server):
        uri = mediamtx_server + '/ctxmgr'
        with rtsp.Source(uri, serve=False, fps=5) as src:
            src.put(Image.new('RGB', (320, 240)))
        assert not src.isOpened()

    def test_client_receives_frame(self, mediamtx_server):
        uri = mediamtx_server + '/recv'
        with rtsp.Source(uri, serve=False, fps=5) as src:
            src.put(Image.new('RGB', (320, 240), color=(200, 80, 40)))
            time.sleep(2.0)   # allow Source to connect and push before Client joins

            with rtsp.Client(uri) as client:
                frame = wait_for_frame(client)

        assert frame is not None

    def test_frame_dimensions_preserved(self, mediamtx_server):
        uri = mediamtx_server + '/dims'
        with rtsp.Source(uri, serve=False, fps=5) as src:
            src.put(Image.new('RGB', (640, 480)))
            time.sleep(2.0)

            with rtsp.Client(uri) as client:
                frame = wait_for_frame(client)

        assert frame is not None
        assert frame.size == (640, 480)

    def test_buffer_loops(self, mediamtx_server):
        """Client should keep receiving frames after the buffer has been cycled."""
        uri = mediamtx_server + '/loop'
        frames_in = [Image.new('RGB', (160, 120), color=(i * 80, 0, 0)) for i in range(3)]

        with rtsp.Source(uri, serve=False, fps=10) as src:
            for f in frames_in:
                src.put(f)
            time.sleep(2.0)

            with rtsp.Client(uri) as client:
                received = [wait_for_frame(client) for _ in range(3)]

        assert all(f is not None for f in received)

    def test_multiple_frame_sizes_resized_to_first(self, mediamtx_server):
        """Frames added after the first are resized to match."""
        uri = mediamtx_server + '/resize'
        with rtsp.Source(uri, serve=False, fps=5) as src:
            src.put(Image.new('RGB', (320, 240)))
            src.put(Image.new('RGB', (1920, 1080)))  # should be resized
            time.sleep(2.0)

            with rtsp.Client(uri) as client:
                frame = wait_for_frame(client)

        assert frame is not None
        assert frame.size == (320, 240)
