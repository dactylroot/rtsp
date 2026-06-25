"""Integration tests: Source, Client, and external tool compatibility.

Skipped automatically when the required tools are not on PATH.
Run mediamtx before the mediamtx suite: https://github.com/bluenviron/mediamtx

TestClientSubprocessServer and TestSourceFFmpegCompat require no mediamtx but
do rely on subprocess spawning and real RTSP networking over loopback, so they
are also kept here rather than in the main CI suite.
"""
import json
import socket
import subprocess
import sys
import time

import numpy as np
import pytest
from PIL import Image

import rtsp
from rtsp.source import Source

from conftest import (
    requires_ffmpeg, requires_ffprobe, requires_gstreamer, requires_mediamtx,
    wait_for_frame,
)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _wait_frame(client, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        f = client.read()
        if f is not None:
            return f
        time.sleep(0.1)
    return None


def _synthetic_frames(n=6, size=(320, 240)):
    """Return n distinct solid-colour PIL Images using no external dataset."""
    return [Image.new('RGB', size, color=(i * 40 % 256, 100, 200)) for i in range(n)]


# ---------------------------------------------------------------------------
# Source → mediamtx → Client round-trip
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Source → FFmpeg tools
# ---------------------------------------------------------------------------

@requires_ffmpeg
@requires_ffprobe
class TestSourceFFmpegCompat:
    """Source (server) consumed by ffprobe and ffmpeg."""

    @pytest.fixture(scope='class')
    def source_uri(self):
        frames = _synthetic_frames()
        port = _free_port()
        src = Source('rtsp://0.0.0.0:{}/live'.format(port),
                     fps=5, size=(320, 240), frame_buffer=frames)
        src.wait_ready(timeout=10)
        yield src.client_uri
        src.close()

    def _ffprobe_streams(self, uri):
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet',
             '-print_format', 'json', '-show_streams',
             '-rtsp_transport', 'tcp', uri],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, 'ffprobe failed:\n' + result.stderr
        return json.loads(result.stdout).get('streams', [])

    def test_ffprobe_reports_h264(self, source_uri):
        streams = self._ffprobe_streams(source_uri)
        video = next((s for s in streams if s['codec_type'] == 'video'), None)
        assert video is not None, 'no video stream in ffprobe output'
        assert video['codec_name'] == 'h264'

    def test_ffprobe_reports_correct_dimensions(self, source_uri):
        streams = self._ffprobe_streams(source_uri)
        video = next(s for s in streams if s['codec_type'] == 'video')
        assert video['width'] == 320
        assert video['height'] == 240

    def test_ffprobe_reports_no_audio(self, source_uri):
        streams = self._ffprobe_streams(source_uri)
        audio = [s for s in streams if s['codec_type'] == 'audio']
        assert not audio, 'unexpected audio stream reported'

    def test_ffmpeg_pulls_frames(self, source_uri):
        """ffmpeg can decode at least 3 frames from our Source and exit cleanly."""
        result = subprocess.run(
            ['ffmpeg', '-rtsp_transport', 'tcp',
             '-i', source_uri,
             '-frames:v', '3', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, 'ffmpeg pull failed:\n' + result.stderr


# ---------------------------------------------------------------------------
# Client ← subprocess server (process-isolation test)
# ---------------------------------------------------------------------------

class TestClientSubprocessServer:
    """Client consuming from a Source running in a separate subprocess.

    Ensures Client works across process boundaries and that no shared in-process
    state masks bugs in the RTSP session or decode path.
    """

    def test_receives_frame(self, subprocess_rtsp_server):
        with rtsp.Client(subprocess_rtsp_server) as client:
            frame = _wait_frame(client, timeout=15)
        assert frame is not None

    def test_frame_is_pil_image(self, subprocess_rtsp_server):
        with rtsp.Client(subprocess_rtsp_server) as client:
            frame = _wait_frame(client, timeout=15)
        assert isinstance(frame, Image.Image)

    def test_frame_dimensions(self, subprocess_rtsp_server):
        with rtsp.Client(subprocess_rtsp_server) as client:
            frame = _wait_frame(client, timeout=15)
        assert frame is not None
        assert frame.size == (320, 240)

    def test_multiple_distinct_frames(self, subprocess_rtsp_server):
        collected = []
        deadline = time.monotonic() + 10
        with rtsp.Client(subprocess_rtsp_server) as client:
            while time.monotonic() < deadline and len(collected) < 3:
                f = client.read()
                if f is not None and (not collected or f is not collected[-1]):
                    collected.append(f)
                time.sleep(0.05)
        assert len(collected) >= 3, \
            'expected at least 3 distinct frames, got {}'.format(len(collected))


# ---------------------------------------------------------------------------
# Source → GStreamer (external consumer)
# ---------------------------------------------------------------------------

@pytest.mark.slow
@requires_gstreamer
class TestSourceGStreamerCompat:
    """Source consumed by gst-launch-1.0 using an independent RTSP/RTP stack."""

    @pytest.fixture(scope='class')
    def source_uri(self):
        frames = _synthetic_frames(n=30)
        port = _free_port()
        src = Source('rtsp://0.0.0.0:{}/live'.format(port),
                     fps=5, size=(320, 240), frame_buffer=frames)
        src.wait_ready(timeout=10)
        src.wait_encoding_started(timeout=5)
        yield src.client_uri
        src.close()

    def _gst_run(self, uri, num_buffers=5, timeout=20):
        return subprocess.run(
            ['gst-launch-1.0', '-e',
             'rtspsrc', 'location={}'.format(uri), 'protocols=tcp', '!',
             'rtph264depay', '!', 'h264parse', '!', 'decodebin', '!',
             'fakesink', 'num-buffers={}'.format(num_buffers)],
            capture_output=True, text=True, timeout=timeout,
        )

    def test_gstreamer_exits_cleanly(self, source_uri):
        result = self._gst_run(source_uri)
        assert result.returncode == 0, \
            'gst-launch-1.0 failed:\n' + result.stderr

    def test_gstreamer_decodes_multiple_buffers(self, source_uri):
        result = self._gst_run(source_uri, num_buffers=10)
        assert result.returncode == 0, \
            'gst-launch-1.0 failed decoding 10 buffers:\n' + result.stderr


# ---------------------------------------------------------------------------
# Client ← mediamtx ← Source (Client against a foreign RTSP stack)
# ---------------------------------------------------------------------------

@requires_mediamtx
class TestClientViaMediamtxRelay:
    """Client consuming a stream relayed through mediamtx."""

    def test_receives_frame_through_relay(self, mediamtx_relay):
        with rtsp.Client(mediamtx_relay) as client:
            frame = _wait_frame(client, timeout=20)
        assert frame is not None, 'no frame received through mediamtx relay'

    def test_frame_is_pil_image(self, mediamtx_relay):
        with rtsp.Client(mediamtx_relay) as client:
            frame = _wait_frame(client, timeout=20)
        assert isinstance(frame, Image.Image)

    def test_frame_dimensions(self, mediamtx_relay):
        with rtsp.Client(mediamtx_relay) as client:
            frame = _wait_frame(client, timeout=20)
        assert frame is not None
        assert frame.size == (320, 240)
