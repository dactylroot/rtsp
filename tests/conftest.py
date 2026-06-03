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
def synthetic_frames():
    """Six solid-colour PIL Images — no external files required."""
    from PIL import Image
    return [Image.new('RGB', (160, 120), color=(i * 40 % 256, 100, 80))
            for i in range(6)]


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
requires_ffprobe = pytest.mark.skipif(
    not _on_path('ffprobe'), reason='ffprobe not on PATH'
)
requires_gstreamer = pytest.mark.skipif(
    not _on_path('gst-launch-1.0'), reason='gst-launch-1.0 not on PATH'
)
requires_mediamtx = pytest.mark.skipif(
    not _on_path('mediamtx'), reason='mediamtx not on PATH'
)


@pytest.fixture(scope='module')
def mediamtx_rtmp_server(tmp_path_factory):
    """mediamtx instance with RTMP enabled on a private port, shared across the module.

    Yields a dict with 'rtsp' and 'rtmp' base URI strings so tests can publish
    via RTMP and optionally read back via either protocol.
    """
    if not _on_path('mediamtx'):
        pytest.skip('mediamtx not on PATH')
    rtsp_port = _free_port()
    rtmp_port = _free_port()
    rtp = _free_udp_even_port()
    rtcp = rtp + 1
    cfg_text = (
        'rtspAddress: :{rtsp}\n'
        'rtpAddress: 127.0.0.1:{rtp}\n'
        'rtcpAddress: 127.0.0.1:{rtcp}\n'
        'rtmpAddress: :{rtmp}\n'
        'rtmp: true\n'
        'hls: false\n'
        'webrtc: false\n'
        'srt: false\n'
        'moq: false\n'
        'paths:\n'
        '  "~.*":\n'
        '    source: publisher\n'
    ).format(rtsp=rtsp_port, rtp=rtp, rtcp=rtcp, rtmp=rtmp_port)
    cfg = tmp_path_factory.mktemp('mediamtx_rtmp') / 'mediamtx.yml'
    cfg.write_text(cfg_text)
    proc = subprocess.Popen(
        ['mediamtx', str(cfg)],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    _wait_tcp('localhost', rtsp_port)
    _wait_tcp('localhost', rtmp_port)
    yield {
        'rtsp': 'rtsp://localhost:{}'.format(rtsp_port),
        'rtmp': 'rtmp://localhost:{}'.format(rtmp_port),
    }
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def subprocess_rtsp_server():
    """Source server in a separate subprocess streaming a 320x240 pattern at 5 fps.

    Exercises Client against a server running in a different process, so any
    shared in-process state cannot mask bugs.  Yields the client URI string.
    """
    import textwrap
    root = str(Path(__file__).parent.parent)
    port = _free_port()
    server_uri = 'rtsp://0.0.0.0:{}/live'.format(port)
    script = textwrap.dedent('''\
        import sys, time
        sys.path.insert(0, {root!r})
        from rtsp.source import Source
        from PIL import Image
        frames = [Image.new('RGB', (320, 240), color=(i * 40 % 256, 100, 200))
                  for i in range(30)]
        src = Source({uri!r}, fps=5, size=(320, 240), frame_buffer=frames)
        src.wait_ready(timeout=10)
        print(src.client_uri, flush=True)
        while True:
            time.sleep(1)
    ''').format(root=root, uri=server_uri)
    proc = subprocess.Popen(
        [sys.executable, '-c', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    client_uri = proc.stdout.readline().decode().strip()
    if not client_uri:
        proc.terminate()
        pytest.fail('subprocess_rtsp_server did not emit a URI')
    yield client_uri
    proc.terminate()
    proc.wait(timeout=5)


def _free_udp_even_port():
    """Return a free UDP port that is even (required for RTP by mediamtx)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('', 0))
        p = s.getsockname()[1]
        return p & ~1  # round down to even


def _mediamtx_cfg(rtsp_port, paths_extra=''):
    """Return a mediamtx YAML config string for the given RTSP port.

    All non-RTSP protocols are disabled to avoid conflicts with any fixed
    default ports the test host may already be using (WebRTC UDP :8000,
    MOQ TCP :8892, etc.).  RTP/RTCP UDP ports are chosen dynamically and
    are always even/odd as required by mediamtx.  The wildcard path regex
    '~.*' is required so mediamtx v1.19+ accepts any incoming publisher
    stream.  Additional path entries can be passed via paths_extra (already
    indented two spaces, placed after the wildcard entry).
    """
    rtp = _free_udp_even_port()
    rtcp = rtp + 1
    return (
        'rtspAddress: :{rtsp}\n'
        'rtpAddress: 127.0.0.1:{rtp}\n'
        'rtcpAddress: 127.0.0.1:{rtcp}\n'
        'rtmp: false\n'
        'hls: false\n'
        'webrtc: false\n'
        'srt: false\n'
        'moq: false\n'
        'paths:\n'
        '  "~.*":\n'
        '    source: publisher\n'
        '{extra}'
    ).format(rtsp=rtsp_port, rtp=rtp, rtcp=rtcp, extra=paths_extra)


@pytest.fixture(scope='module')
def mediamtx_server(tmp_path_factory):
    """mediamtx instance on a private RTSP port, shared across the module."""
    if not _on_path('mediamtx'):
        pytest.skip('mediamtx not on PATH')
    port = _free_port()
    cfg = tmp_path_factory.mktemp('mediamtx') / 'mediamtx.yml'
    cfg.write_text(_mediamtx_cfg(port))
    proc = subprocess.Popen(
        ['mediamtx', str(cfg)],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    _wait_tcp('localhost', port)
    yield 'rtsp://localhost:{}'.format(port)
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def mediamtx_relay(tmp_path, subprocess_rtsp_server):
    """mediamtx configured to pull from a Source subprocess and relay it.

    Yields the mediamtx URI that Client can read from.  The relay pulls
    from subprocess_rtsp_server on demand so Client connects to a different
    RTSP stack (mediamtx) rather than directly to our Source.
    """
    if not _on_path('mediamtx'):
        pytest.skip('mediamtx not on PATH')
    port = _free_port()
    cfg = tmp_path / 'mediamtx.yml'
    relay_path = (
        '  live:\n'
        '    source: {src}\n'
        '    sourceOnDemand: true\n'
    ).format(src=subprocess_rtsp_server)
    cfg.write_text(_mediamtx_cfg(port, paths_extra=relay_path))
    proc = subprocess.Popen(
        ['mediamtx', str(cfg)],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    _wait_tcp('localhost', port)
    yield 'rtsp://localhost:{}/live'.format(port)
    proc.terminate()
    proc.wait(timeout=5)
