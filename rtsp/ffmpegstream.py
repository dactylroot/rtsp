""" FFmpeg Backend RTSP Client """

import json
import subprocess
import sys
from io import BytesIO
from threading import Thread
from urllib.parse import urlparse, urlunparse

import numpy as np
from PIL import Image

_NETWORK_SCHEMES = {'rtsp', 'rtsps', 'rtmp', 'rtmps', 'http', 'https'}


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

    parsed = urlparse(s)
    if not parsed.scheme:
        # bare host or host/path — assume rtsp://
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
    return ['-i', uri]  # rtmp, http, https — ffmpeg handles natively


def _probe(ffprobe_input_args):
    """Run ffprobe with *ffprobe_input_args* and return ``(width, height)``."""
    cmd = [
        'ffprobe', '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams', '-select_streams', 'v:0',
    ] + ffprobe_input_args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    info = json.loads(result.stdout)
    s = info['streams'][0]
    return int(s['width']), int(s['height'])


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

    def _ffprobe_input_args(self):
        if self._kind == 'device':
            args = _ffmpeg_device_args(self._uri)
            # args = ['-f', fmt, '-i', path]; ffprobe wants the same flags
            return args
        return ['-i', self._uri]

    def open(self):
        if self.isOpened():
            return

        try:
            self._width, self._height = _probe(self._ffprobe_input_args())
        except Exception:
            if self._kind == 'device':
                self._width, self._height = 640, 480  # safe fallback for webcams
            else:
                raise

        cmd = ['ffmpeg'] + self._ffmpeg_input_args() + [
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-vcodec', 'rawvideo', '-an', '-',
        ]
        self._stream = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None if self._verbose else subprocess.DEVNULL,
            bufsize=10 ** 8,
        )
        if self._verbose:
            label = self._uri if self._kind == 'device' else _redact_uri(self._uri)
            print("Connected to {}.".format(label))

        self._bg_run = True
        t = Thread(target=self._update, args=())
        t.daemon = True
        t.start()
        self._bgt = t
        return self

    def close(self):
        self._bg_run = False
        if self._stream:
            self._stream.terminate()
            self._stream = None
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
            raw = self._stream.stdout.read(frame_size)
            if len(raw) != frame_size:
                self._bg_run = False
                break
            self._queue = np.frombuffer(raw, dtype=np.uint8).reshape(
                (self._height, self._width, 3)
            )
        if self._stream:
            self._stream.stdout.close()
            self._stream.terminate()
            self._stream = None

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
