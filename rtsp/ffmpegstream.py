""" FFmpeg Backend RTSP Client """

import logging
import re
import socket
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path as _Path
from threading import Event, Lock, Thread
from urllib.parse import urlparse, urlunparse

import numpy as np
from PIL import Image

_NETWORK_SCHEMES = {'rtsp', 'rtsps', 'rtmp', 'rtmps', 'http', 'https', 'tcp'}
_DIM_RE = re.compile(r'Video:.*?(\d{2,5})x(\d{2,5})')

logging.getLogger('rtsp').addHandler(logging.NullHandler())


def _enable_verbose():
    """Attach a StreamHandler to the 'rtsp' logger if none is already configured.

    Called when verbose=True so output appears without requiring application-level
    logging configuration.  Idempotent — safe to call multiple times.
    """
    logger = logging.getLogger('rtsp')
    if not any(not isinstance(h, logging.NullHandler) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(name)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


def _parse_uri(uri):
    """Normalize *uri* and return ``(kind, normalized)`.

    kind is one of ``'device'``, ``'picam'``, or ``'network'``.
    normalized is an ``int`` for devices, otherwise a string.
    """
    if isinstance(uri, int):
        return 'device', uri
    if not isinstance(uri, str):
        raise TypeError("URI must be a str or int, got {!r}".format(type(uri).__name__))

    s = uri.strip()

    if s.isdigit():
        return 'device', int(s)

    if 'picam' in s.lower():
        return 'picam', s

    if '//' not in s:
        # bare host — e.g. '192.168.1.1/stream' or 'localhost:8554/live'
        # urlparse misreads 'host:port/path' as scheme='host' without '//'
        s = 'rtsp://' + s
    parsed = urlparse(s)

    if parsed.scheme not in _NETWORK_SCHEMES:
        raise ValueError(
            "Unsupported URI scheme {!r}. Supported: {}".format(
                parsed.scheme, ', '.join(sorted(_NETWORK_SCHEMES))
            )
        )
    if not parsed.hostname:
        raise ValueError("URI is missing a hostname: {!r}".format(s))

    return 'network', s


def _redact_uri(uri):
    """Return *uri* with any password replaced by *** for safe logging."""
    parsed = urlparse(uri)
    if not parsed.password:
        return uri
    netloc = '{}:***@{}'.format(parsed.username, parsed.hostname)
    if parsed.port:
        netloc += ':{}'.format(parsed.port)
    return urlunparse(parsed._replace(netloc=netloc))


def _ffmpeg_device_args(index):
    """Return ffmpeg input args for a local capture device."""
    if sys.platform == 'darwin':
        return ['-f', 'avfoundation', '-i', str(index)]
    if sys.platform.startswith('linux'):
        return ['-f', 'v4l2', '-i', '/dev/video{}'.format(index)]
    return ['-f', 'dshow', '-i', 'video={}'.format(index)]


def _ffmpeg_network_args(scheme, uri):
    """Return ffmpeg input args for a network URI."""
    if scheme in ('rtsp', 'rtsps'):
        return ['-rtsp_transport', 'tcp', '-i', uri]
    if scheme == 'tcp':
        return ['-fflags', 'nobuffer', '-analyzeduration', '500000', '-i', uri]
    return ['-i', uri]  # rtmp, http, https — ffmpeg handles natively


def _wait_port(host, port, timeout=10.0):
    """Poll until a TCP connection to host:port succeeds. Returns True on success."""
    if host in ('0.0.0.0', '', None):
        host = '127.0.0.1'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _to_pil(frame):
    """Convert a file path, numpy array, or PIL Image to PIL Image."""
    if isinstance(frame, Image.Image):
        return frame
    if isinstance(frame, np.ndarray):
        return Image.fromarray(frame)
    if isinstance(frame, (str, _Path)):
        img = Image.open(frame)
        img.load()  # force-read pixels so the file handle is released immediately
        return img
    raise TypeError("frame must be a path, numpy array, or PIL Image, got {!r}".format(type(frame).__name__))




class Client:
    """Maintain a live video feed without buffering."""
    _stream = None

    def __init__(self, rtsp_server_uri, verbose=False):
        """
        rtsp_server_uri: RTSP/RTMP/HTTP URL, bare host[:port][/path], or
                         int / numeric string for a local capture device index.
        verbose: stream ffmpeg log to stderr and print connection messages.
        """
        self.rtsp_server_uri = rtsp_server_uri
        self._verbose = verbose
        self._queue = None
        self._bg_run = False
        self._width = None
        self._height = None

        self._kind, self._uri = _parse_uri(rtsp_server_uri)

        if self._kind == 'picam':
            self.__class__ = PicamVideoFeed
            self.__dict__.update(PicamVideoFeed().__dict__)
            return

        self.open()

    def __enter__(self, *args, **kwargs):
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        self.close()

    def _ffmpeg_input_args(self):
        if self._kind == 'device':
            return _ffmpeg_device_args(self._uri)
        return _ffmpeg_network_args(urlparse(self._uri).scheme, self._uri)

    def open(self):
        if self.isOpened():
            return

        cmd = ['ffmpeg'] + self._ffmpeg_input_args() + [
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-vcodec', 'rawvideo', '-an', '-',
        ]
        self._stream = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10 ** 8,
        )

        # Parse width/height from FFmpeg's startup output before reading frames.
        # This avoids a separate ffprobe connection (critical for single-client servers).
        dims_ready = Event()
        stderr_t = Thread(target=self._read_stderr, args=(dims_ready,))
        stderr_t.daemon = True
        stderr_t.start()

        if not dims_ready.wait(timeout=15):
            try:
                self._stream.stdout.close()
            except (OSError, ValueError):
                pass
            self._stream.terminate()
            self._stream = None
            if self._kind == 'device':
                self._width, self._height = 640, 480   # fallback for local devices
            else:
                raise RuntimeError(
                    "Timed out waiting for video dimensions from {!r}".format(self._uri)
                )

        if self._verbose:
            label = self._uri if self._kind == 'device' else _redact_uri(self._uri)
            print("Connected to {} ({}x{}).".format(label, self._width, self._height))

        self._bg_run = True
        t = Thread(target=self._update, args=())
        t.daemon = True
        t.start()
        self._bgt = t
        return self

    def _read_stderr(self, dims_ready):
        """Drain FFmpeg stderr; signal *dims_ready* once video dimensions are parsed."""
        for raw in self._stream.stderr:
            line = raw.decode('utf-8', errors='ignore')
            if self._verbose:
                sys.stderr.write(line)
            if not dims_ready.is_set():
                m = _DIM_RE.search(line)
                if m:
                    self._width, self._height = int(m.group(1)), int(m.group(2))
                    dims_ready.set()

    def close(self):
        self._bg_run = False
        stream, self._stream = self._stream, None
        if stream:
            try:
                stream.stdout.close()
            except (OSError, ValueError):
                pass
            stream.terminate()
        if hasattr(self, '_bgt'):
            self._bgt.join(timeout=2)
        if self._verbose:
            label = self._uri if self._kind == 'device' else _redact_uri(self._uri)
            print("Disconnected from {}.".format(label))

    def isOpened(self):
        return self._stream is not None and self._bg_run

    def _update(self):
        frame_size = self._width * self._height * 3
        while self._bg_run:
            stream = self._stream
            if stream is None:
                break
            try:
                raw = stream.stdout.read(frame_size)
            except (OSError, ValueError):
                break
            if len(raw) != frame_size:
                self._bg_run = False
                break
            self._queue = np.frombuffer(raw, dtype=np.uint8).reshape(
                (self._height, self._width, 3)
            )
        stream, self._stream = self._stream, None
        if stream:
            try:
                stream.stdout.close()
            except (OSError, ValueError):
                pass
            try:
                stream.terminate()
            except Exception:
                pass

    def read(self, raw=False):
        """Return most recent frame as PIL Image, or RGB numpy array with raw=True."""
        try:
            if raw:
                return self._queue
            return Image.fromarray(self._queue)
        except Exception:
            return None

    def preview(self):
        """Blocking. Opens a window to display the stream. Press 'q' or ESC to quit."""
        import tkinter as tk
        from PIL import ImageTk

        root = tk.Tk()
        root.title('RTSP')
        label = tk.Label(root)
        label.pack()

        def _tick():
            frame = self.read()
            if frame is not None:
                photo = ImageTk.PhotoImage(frame)
                label.config(image=photo)
                label.image = photo
            if self._bg_run:
                root.after(33, _tick)
            else:
                root.destroy()

        def _on_key(event):
            if event.keysym in ('q', 'Escape'):
                root.destroy()

        root.bind('<Key>', _on_key)
        _tick()
        root.mainloop()


class PicamVideoFeed(Client):

    def __init__(self):
        import picamera
        self.cam = picamera.PiCamera()

    def preview(self, *args, **kwargs):
        self.cam.start_preview(*args, **kwargs)

    def open(self):
        pass

    def isOpened(self):
        return True

    def read(self, raw=False):
        """https://picamera.readthedocs.io/en/release-1.13/recipes1.html#capturing-to-a-pil-image"""
        stream = BytesIO()
        self.cam.capture(stream, format='png')
        stream.seek(0)
        return Image.open(stream)

    def close(self):
        pass

    def stop(self):
        pass


_MIN_ENCODE_FPS = 10  # minimum fps fed to FFmpeg — encoder needs several frames before it binds the output port


class Source:
    """Serve or push a looping buffer of frames as an RTSP stream via FFmpeg.

    By default (serve=True) FFmpeg listens for a single incoming client —
    no external server needed.  Set serve=False to push to a relay such as
    mediamtx, which supports multiple simultaneous clients.

    Usage (single client, no relay)::

        with rtsp.Source('rtsp://0.0.0.0:8554/live', fps=25) as src:
            for path in image_files:
                src.put(path)
            # blocks until a client connects, then streams until context exits

    Usage (push to mediamtx for multiple clients)::

        with rtsp.Source('rtsp://localhost:8554/live', fps=25, serve=False) as src:
            for path in image_files:
                src.put(path)
    """

    _stream = None

    def __init__(self, rtsp_server_uri, fps=25, serve=True, verbose=False,
                 size=None, frame_buffer=None):
        """
        rtsp_server_uri: RTSP URI to serve on or push to.
        fps: output frame rate.
        serve: if True (default), FFmpeg listens for one client (no relay needed).
               if False, FFmpeg pushes to an external RTSP server.
        verbose: pass ffmpeg stderr through and print status messages.
        size: (width, height) output resolution.  All frames are scaled to fit.
              If omitted, the first put() frame determines the resolution.
        frame_buffer: optional iterable of initial frames (paths, numpy arrays,
                      or PIL Images).  Loaded in a background thread after the
                      first frame is processed.
        """
        self._kind, self._uri = _parse_uri(rtsp_server_uri)
        if self._kind != 'network':
            raise ValueError(
                "Source URI must be a network address, e.g. rtsp://0.0.0.0:8554/live"
            )

        self._fps = fps
        self._serve = serve
        self._verbose = verbose
        self._buffer = []
        self._lock = Lock()
        self._bg_run = False
        self._loader = None
        self._ready = Event()

        if size is not None:
            w, h = size
            self._size = (w & ~1, h & ~1)   # snap to even for H.264
            self.open()
        else:
            self._size = None   # inferred from first put()

        if frame_buffer is not None:
            frame_iter = iter(frame_buffer)
            try:
                self.put(next(frame_iter))   # first frame: sets _size if not set, starts FFmpeg
            except StopIteration:
                pass
            else:
                def _load_rest(it):
                    for frame in it:
                        self.put(frame)
                self._loader = Thread(target=_load_rest, args=(frame_iter,), daemon=True)
                self._loader.start()

    def __enter__(self, *args, **kwargs):
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        self.close()

    def put(self, frame):
        """Add a frame to the buffer.

        Accepts a file path (str), RGB numpy array, or PIL Image.
        Frames that don't match the first frame's dimensions are resized.
        FFmpeg starts automatically on the first call.
        """
        frame = _to_pil(frame)
        if self._size is None:
            self._size = frame.size  # PIL size is (width, height)
            self.open()
        elif frame.size != self._size:
            frame = frame.resize(self._size)
        with self._lock:
            self._buffer.append(frame)

    def open(self):
        """Start serving or pushing. Called automatically by put()."""
        if self.isOpened() or self._size is None:
            return self
        w, h = self._size
        w, h = w & ~1, h & ~1   # H.264 requires even dimensions
        self._size = (w, h)
        encode_fps = max(self._fps, _MIN_ENCODE_FPS)

        if self._serve:
            # FFmpeg 4.x RTSP muxer with -rtsp_flags listen tries to connect
            # rather than bind.  Use TCP+mpegts listen mode instead, which
            # reliably binds the port.  Extract host:port from the rtsp:// URI.
            parsed = urlparse(self._uri)
            port = parsed.port or 8554
            output_url = 'tcp://0.0.0.0:{}?listen=1'.format(port)
            cmd = [
                'ffmpeg',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                '-s', '{}x{}'.format(w, h),
                '-r', str(encode_fps),
                '-i', 'pipe:0',
                '-vcodec', 'libx264', '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-g', str(encode_fps),   # keyframe every second so late-joining clients decode immediately
                '-pix_fmt', 'yuv420p',
                '-f', 'mpegts',
                output_url,
            ]
        else:
            cmd = [
                'ffmpeg',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                '-s', '{}x{}'.format(w, h),
                '-r', str(encode_fps),
                '-i', 'pipe:0',
                '-vcodec', 'libx264', '-pix_fmt', 'yuv420p',
                '-f', 'rtsp',
                self._uri,
            ]

        # Pipe stderr for serve=True so _drain_stderr can detect readiness.
        # For serve=False, inherit (verbose) or discard.
        if self._serve:
            stderr_arg = subprocess.PIPE
        elif self._verbose:
            stderr_arg = None
        else:
            stderr_arg = subprocess.DEVNULL
        self._ready.clear()
        self._stream = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stderr=stderr_arg,
        )
        if self._verbose:
            action = "Serving on" if self._serve else "Pushing to"
            print("{} {}.".format(action, _redact_uri(self._uri)))
        self._bg_run = True
        t = Thread(target=self._feed)
        t.daemon = True
        t.start()
        self._bgt = t
        if self._serve:
            parsed = urlparse(self._uri)
            port = parsed.port or 8554
            rt = Thread(target=self._poll_tcp_ready, args=(port,))
            rt.daemon = True
            rt.start()
            st = Thread(target=self._drain_stderr)
            st.daemon = True
            st.start()
        return self

    def close(self):
        """Stop the stream and terminate FFmpeg."""
        self._bg_run = False
        if self._loader and self._loader.is_alive():
            self._loader.join(timeout=10)
        if self._stream:
            try:
                self._stream.stdin.close()
            except OSError:
                pass
            self._stream.terminate()
            self._stream = None
        if hasattr(self, '_bgt'):
            self._bgt.join(timeout=2)
        if self._verbose:
            print("Stopped pushing to {}.".format(_redact_uri(self._uri)))

    def isOpened(self):
        return self._stream is not None and self._bg_run

    def _drain_stderr(self):
        """Drain Source FFmpeg stderr (verbose output only)."""
        try:
            for raw in self._stream.stderr:
                if self._verbose:
                    sys.stderr.write(raw.decode('utf-8', errors='ignore'))
        except (OSError, ValueError):
            pass

    def _poll_tcp_ready(self, port):
        """Background thread: set _ready once port is bound (bind probe fails).

        Probes 0.0.0.0 — on macOS, binding a specific address (127.0.0.1) can
        succeed even when a wildcard listener holds the port, but binding the
        wildcard address (0.0.0.0) always fails when the port is taken.
        """
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and self._bg_run:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('0.0.0.0', port))
                s.close()
                # Bind succeeded → port free → FFmpeg not yet listening
            except OSError:
                try:
                    s.close()
                except Exception:
                    pass
                self._ready.set()
                return
            time.sleep(0.05)
        self._ready.set()  # fallback after timeout

    def wait_ready(self, timeout=10.0):
        """Block until FFmpeg has initialized and is listening. Returns True on success.

        Only meaningful for serve=True. Detected by the first line FFmpeg writes
        to stderr — which appears immediately after it binds the listen port.
        """
        if not self._serve:
            return True
        return self._ready.wait(timeout=timeout)

    @property
    def client_uri(self):
        """TCP URI that a Client should use to connect to this served stream.

        Only valid when serve=True.  Returns ``tcp://host:port`` derived from
        the rtsp:// URI passed to the constructor (0.0.0.0 is replaced with
        127.0.0.1 for client use).
        """
        parsed = urlparse(self._uri)
        host = parsed.hostname or '127.0.0.1'
        if host == '0.0.0.0':
            host = '127.0.0.1'
        port = parsed.port or 8554
        return 'tcp://{}:{}'.format(host, port)

    def serve_forever(self):
        """Block and re-serve successive single clients, restarting after each disconnect.

        Only valid when serve=True.  With serve=False, the relay (e.g. mediamtx)
        handles reconnections automatically and this method is unnecessary.

        Raises RuntimeError if called with serve=False or before any frame is added.
        Stop with KeyboardInterrupt (Ctrl-C).
        """
        if not self._serve:
            raise RuntimeError(
                "serve_forever() requires serve=True; a relay such as mediamtx "
                "already handles multiple clients when serve=False."
            )
        if self._size is None:
            raise RuntimeError(
                "serve_forever() requires at least one frame in the buffer; "
                "call put() or pass frame_buffer= before calling serve_forever()."
            )
        try:
            while True:
                if not self.isOpened():
                    self.close()   # join dead thread and release stale process handle
                    self.open()    # start FFmpeg listening for next client
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    def _feed(self):
        """Background thread: loop buffer and write raw frames to FFmpeg stdin.

        When fps < _MIN_ENCODE_FPS the same frame is repeated at _MIN_ENCODE_FPS
        so the H.264 encoder stays healthy, while buffer advancement still happens
        at the user-specified fps.
        """
        encode_fps = max(self._fps, _MIN_ENCODE_FPS)
        encode_interval = 1.0 / encode_fps
        advance_interval = 1.0 / self._fps
        idx = 0
        next_encode = None
        next_advance = None

        while self._bg_run:
            with self._lock:
                buf = list(self._buffer)

            if not buf:
                time.sleep(0.05)   # fast poll while buffer is still loading
                continue

            if next_encode is None:   # first frame: initialize timing and write immediately
                next_encode = time.monotonic()
                next_advance = next_encode + advance_interval

            now = time.monotonic()
            while now >= next_advance:
                idx += 1
                next_advance += advance_interval

            try:
                self._stream.stdin.write(np.array(buf[idx % len(buf)]).tobytes())
                self._stream.stdin.flush()
            except (BrokenPipeError, OSError):
                self._bg_run = False
                break

            next_encode += encode_interval
            delay = next_encode - time.monotonic()
            if delay > 0:
                time.sleep(delay)
