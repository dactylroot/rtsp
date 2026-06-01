import shutil
import socket
import subprocess
import time

import pytest


def _on_path(binary):
    return shutil.which(binary) is not None


def _free_port():
    """Return an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def wait_for_frame(client, raw=False, timeout=8):
    """Poll client.read() until a frame arrives or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = client.read(raw=raw)
        if frame is not None:
            return frame
        time.sleep(0.1)
    return None


requires_ffmpeg = pytest.mark.skipif(
    not _on_path('ffmpeg'), reason='ffmpeg not on PATH'
)
requires_mediamtx = pytest.mark.skipif(
    not _on_path('mediamtx'), reason='mediamtx not on PATH'
)


@pytest.fixture
def ffmpeg_rtsp_server():
    """Single-client FFmpeg RTSP server streaming a 320x240 lavfi test pattern at 5 fps."""
    if not _on_path('ffmpeg'):
        pytest.skip('ffmpeg not on PATH')
    port = _free_port()
    uri = 'rtsp://127.0.0.1:{}/test'.format(port)
    proc = subprocess.Popen(
        [
            'ffmpeg', '-re',
            '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=5',
            '-f', 'rtsp', '-rtsp_flags', 'listen', uri,
        ],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    time.sleep(1.5)   # let FFmpeg bind and begin listening
    yield uri
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope='module')
def mediamtx_server(tmp_path_factory):
    """mediamtx relay on a private port, shared across the module."""
    if not _on_path('mediamtx'):
        pytest.skip('mediamtx not on PATH')
    port = _free_port()
    cfg = tmp_path_factory.mktemp('mediamtx') / 'mediamtx.yml'
    cfg.write_text('rtspAddress: :{}\n'.format(port))
    proc = subprocess.Popen(
        ['mediamtx', str(cfg)],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    yield 'rtsp://localhost:{}'.format(port)
    proc.terminate()
    proc.wait(timeout=5)
