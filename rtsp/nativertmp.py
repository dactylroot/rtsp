"""Python-native RTMP client and publisher via PyAV/libavformat.

RTMPClient    — receive frames from an RTMP stream (av.open read mode).
RTMPPublisher — push frames to an RTMP relay/server (av.open write mode).

libavformat's RTMP protocol handler manages the handshake, AMF commands,
and chunk framing.  No FFmpeg subprocess is spawned.

Requires PyAV: ``pip install av``
"""

import logging
import time
from threading import Lock, Thread

import numpy as np
from PIL import Image

from ._utils import _parse_uri, _to_pil

log = logging.getLogger('rtsp.rtmp')

_MIN_ENCODE_FPS = 10
_CONNECT_TIMEOUT = 15   # seconds

try:
    import av as _av
except ImportError:
    _av = None


# ---------------------------------------------------------------------------
# Public API: RTMPClient
# ---------------------------------------------------------------------------

class RTMPClient:
    """Receive frames from an RTMP stream via PyAV/libavformat.

    Same public API as ``Client``: ``open()``, ``close()``,
    ``read()``, ``isOpened()``, ``preview()``.

    Requires PyAV: ``pip install av``
    """

    def __init__(self, rtmp_uri: str, verbose: bool = False) -> None:
        if _av is None:
            raise ImportError('RTMPClient requires PyAV: pip install av')

        self.rtsp_server_uri = rtmp_uri
        self._verbose = verbose
        self._queue = None
        self._bg_run = False
        self._width = None
        self._height = None
        self._lock = Lock()
        self._container = None
        self._bgt = None

        self.open()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ---- public API ----

    def open(self):
        if self.isOpened():
            return self
        self._bg_run = True
        t = Thread(target=self._recv_loop, daemon=True, name='rtmp-client')
        t.start()
        self._bgt = t
        return self

    def close(self):
        self._bg_run = False
        container, self._container = self._container, None
        if container:
            try:
                container.close()
            except Exception:
                pass
        if self._bgt:
            self._bgt.join(timeout=2)
            self._bgt = None

    def isOpened(self) -> bool:
        return self._bg_run

    def read(self, raw: bool = False):
        """Return the most recent frame as a PIL Image, or RGB numpy array with ``raw=True``."""
        with self._lock:
            q = self._queue
        try:
            return q if raw else Image.fromarray(q)
        except Exception:
            return None

    def preview(self):
        """Blocking.  Opens a window to display the stream.  Press 'q' or ESC to quit."""
        import tkinter as tk
        from PIL import ImageTk

        root = tk.Tk()
        root.title('RTMP')
        label = tk.Label(root)
        label.pack()

        def _stop():
            self._bg_run = False

        def _tick():
            if not self._bg_run:
                root.destroy()
                return
            frame = self.read()
            if frame is not None:
                photo = ImageTk.PhotoImage(frame)
                label.config(image=photo)
                label.image = photo
            root.after(33, _tick)

        root.protocol('WM_DELETE_WINDOW', _stop)
        root.bind_all('<Key>', lambda e: _stop() if e.keysym in ('q', 'Escape') else None)
        _tick()
        root.mainloop()
        self.close()

    # ---- background thread ----

    def _recv_loop(self) -> None:
        try:
            container = _av.open(
                self.rtsp_server_uri,
                timeout=(_CONNECT_TIMEOUT, _CONNECT_TIMEOUT * 2),
            )
        except Exception as exc:
            log.error('RTMP connect failed: %s', exc)
            self._bg_run = False
            return

        self._container = container
        video = next((s for s in container.streams if s.type == 'video'), None)
        if video is None:
            log.error('no video stream in %s', self.rtsp_server_uri)
            container.close()
            self._bg_run = False
            return

        try:
            for frame in container.decode(video):
                if not self._bg_run:
                    break
                arr = frame.to_ndarray(format='rgb24')
                with self._lock:
                    self._queue = arr
                    if self._width is None:
                        self._width = arr.shape[1]
                        self._height = arr.shape[0]
                        if self._verbose:
                            log.info('stream resolution: %dx%d',
                                     self._width, self._height)
        except Exception as exc:
            log.debug('RTMP decode error: %s', exc)
        finally:
            try:
                container.close()
            except Exception:
                pass
        self._bg_run = False


# ---------------------------------------------------------------------------
# Public API: RTMPPublisher
# ---------------------------------------------------------------------------

class RTMPPublisher:
    """Push frames to an RTMP relay/server via PyAV/libavformat.

    libavformat handles the RTMP handshake, AMF connect/publish commands,
    and FLV packetization.  Same frame-input API as ``Publisher``:
    ``put()``, ``open()``, ``close()``, ``isOpened()``.

    Requires PyAV: ``pip install av``
    """

    def __init__(self, rtmp_uri: str, fps: float = 25,
                 verbose: bool = False,
                 size: tuple[int, int] | None = None,
                 frame_buffer=None) -> None:
        if _av is None:
            raise ImportError('RTMPPublisher requires PyAV: pip install av')

        kind, uri = _parse_uri(rtmp_uri)
        if kind != 'network':
            raise ValueError(
                'Source URI must be a network address, e.g. rtmp://server/live/stream'
            )
        self._uri = uri
        self._fps = fps
        self._verbose = verbose
        self._size: tuple[int, int] | None = None
        self._buffer: list[Image.Image] = []
        self._lock = Lock()
        self._bg_run = False
        self._container = None
        self._stream = None
        self._loader = None

        if size is not None:
            w, h = size
            self._size = (w & ~1, h & ~1)
            self.open()

        if frame_buffer is not None:
            it = iter(frame_buffer)
            try:
                self.put(next(it))
            except StopIteration:
                pass
            else:
                def _load(iterator):
                    for f in iterator:
                        self.put(f)
                self._loader = Thread(target=_load, args=(it,), daemon=True)
                self._loader.start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ---- public API ----

    def put(self, frame) -> None:
        """Add a frame to the buffer.  Connects to relay on first call."""
        frame = _to_pil(frame)
        if self._size is None:
            self._size = (frame.width & ~1, frame.height & ~1)
            self.open()
        elif frame.size != self._size:
            frame = frame.resize(self._size)
        with self._lock:
            self._buffer.append(frame)

    def open(self):
        """Connect to RTMP relay and start the encode loop."""
        if self.isOpened() or self._size is None:
            return self

        w, h = self._size
        encode_fps = max(self._fps, _MIN_ENCODE_FPS)

        container = _av.open(
            self._uri,
            mode='w',
            format='flv',
            timeout=(_CONNECT_TIMEOUT, _CONNECT_TIMEOUT * 2),
        )
        stream = container.add_stream('libx264', rate=int(encode_fps))
        stream.width = w
        stream.height = h
        stream.pix_fmt = 'yuv420p'
        stream.options = {'preset': 'ultrafast', 'tune': 'zerolatency'}

        self._container = container
        self._stream = stream

        if self._verbose:
            log.info('publishing to %s', self._uri)

        self._bg_run = True
        Thread(target=self._encode_loop, daemon=True, name='rtmp-publisher').start()
        return self

    def close(self) -> None:
        self._bg_run = False
        if self._loader and self._loader.is_alive():
            self._loader.join(timeout=10)
        container, self._container = self._container, None
        stream, self._stream = self._stream, None
        if container and stream:
            try:
                for pkt in stream.encode():
                    container.mux(pkt)
                container.close()
            except Exception:
                pass

    def isOpened(self) -> bool:
        return self._bg_run

    def serve_forever(self):
        raise RuntimeError(
            'serve_forever() requires serve=True; use '
            'rtsp.Source(..., serve=True) for a built-in server.'
        )

    # ---- background thread ----

    def _encode_loop(self) -> None:
        encode_fps = max(self._fps, _MIN_ENCODE_FPS)
        encode_interval = 1.0 / encode_fps
        advance_interval = 1.0 / self._fps
        pts = 0
        idx = 0
        next_encode = next_advance = None

        while self._bg_run:
            with self._lock:
                buf = list(self._buffer)
            if not buf:
                time.sleep(0.05)
                continue

            if next_encode is None:
                next_encode = time.monotonic()
                next_advance = next_encode + advance_interval

            now = time.monotonic()
            while now >= next_advance:
                idx += 1
                next_advance += advance_interval

            av_frame = _av.VideoFrame.from_ndarray(
                np.array(buf[idx % len(buf)]), format='rgb24')
            av_frame = av_frame.reformat(format='yuv420p')
            av_frame.pts = pts
            pts += 1

            container = self._container
            stream = self._stream
            if container is None or stream is None:
                break

            try:
                for pkt in stream.encode(av_frame):
                    container.mux(pkt)
            except Exception as exc:
                log.debug('RTMP mux error: %s', exc)
                self._bg_run = False
                break

            next_encode += encode_interval
            delay = next_encode - time.monotonic()
            if delay > 0:
                time.sleep(delay)

        self._bg_run = False
