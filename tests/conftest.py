import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_NOUVEAU_ROOT = Path.home() / 'data' / 'nouveau' / 'nouveau'


def _nouveau_jpgs(n=6):
    """Return up to *n* jpg paths sampled evenly from the nouveau dataset."""
    paths = sorted(_NOUVEAU_ROOT.glob('**/*.jpg'))
    if not paths:
        return []
    step = max(1, len(paths) // n)
    return paths[::step][:n]


requires_nouveau = pytest.mark.skipif(
    not _NOUVEAU_ROOT.exists(),
    reason='~/data/nouveau/nouveau not found',
)


@pytest.fixture(scope='session')
def nouveau_frames():
    """A small list of Path objects sampled from the nouveau dataset."""
    paths = _nouveau_jpgs()
    if not paths:
        pytest.skip('no images found in ~/data/nouveau/nouveau')
    return paths


@pytest.fixture(scope='session')
def nouveau_frame(nouveau_frames):
    """A single nouveau image path."""
    return nouveau_frames[0]


def _on_path(binary):
    return shutil.which(binary) is not None


def _free_port():
    """Return an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _wait_tcp(host, port, timeout=10.0):
    """Block until a TCP connection to host:port succeeds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def wait_for_frame(client, raw=False, timeout=8):
    """Poll client.read() until a frame arrives or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = client.read(raw=raw)
        if frame is not None:
            return frame
        time.sleep(0.1)
    return None


@pytest.fixture(scope='session')
def _display_ok():
    """Run once per session: True if a Tk window can be created in a subprocess."""
    try:
        proc = subprocess.Popen(
            [sys.executable, '-c',
             'import tkinter; r=tkinter.Tk(); r.after(0,r.quit); r.mainloop()'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=5)
            return proc.returncode == 0
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
            return False
    except Exception:
        return False


@pytest.fixture
def requires_display(_display_ok):
    """Skip the test if no display is available. Use with @pytest.mark.usefixtures."""
    if not _display_ok:
        pytest.skip('no display available for tkinter')


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
    _wait_tcp('127.0.0.1', port)
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
    _wait_tcp('localhost', port)
    yield 'rtsp://localhost:{}'.format(port)
    proc.terminate()
    proc.wait(timeout=5)
