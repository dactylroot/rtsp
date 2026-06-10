"""Python-native RTSP client.

Handles RTSP/1.0 session negotiation in pure Python and decodes H.264 with
PyAV (libavcodec Python bindings).  No FFmpeg subprocess is spawned for
rtsp:// streams.  Integer device indices (e.g. ``0``) open a local camera
directly through PyAV without any RTSP handshake.

Install PyAV for native decode: ``pip install av``
"""

import base64
import errno
import logging
import platform as _platform
import re
import socket
import struct
import threading
import time
from urllib.parse import urlparse

from PIL import Image

log = logging.getLogger('rtsp.native_client')

_DEFAULT_PORT = 554
_RECV_SIZE = 65536
_BACKOFF_INITIAL = 0.05   # seconds before first retry
_BACKOFF_CAP = 2.0        # maximum sleep between retries
_CONNECT_TIMEOUT = 15.0   # total seconds before giving up

try:
    import av as _av
except ImportError:
    _av = None


def _local_device_args(index):
    """Return ``(device_string, format_name, options)`` for ``av.open`` on this platform."""
    system = _platform.system()
    if system == 'Darwin':
        return str(index), 'avfoundation', {'framerate': '30'}
    if system == 'Linux':
        return '/dev/video{}'.format(index), 'v4l2', {}
    if system == 'Windows':
        return str(index), 'dshow', {}
    return str(index), None, {}


def _device_open_kwargs(fmt, options):
    """Build the kwargs dict for ``av.open()`` from format name and options dict."""
    kw = {}
    if fmt:
        kw['format'] = fmt
    if options:
        kw['options'] = options
    return kw


def _macos_camera_names():
    import json
    import subprocess
    try:
        r = subprocess.run(
            ['system_profiler', 'SPCameraDataType', '-json'],
            capture_output=True, text=True, timeout=5,
        )
        cameras = json.loads(r.stdout).get('SPCameraDataType', [])
        return [c.get('_name', 'Camera {}'.format(i)) for i, c in enumerate(cameras)]
    except Exception:
        return None


def _linux_camera_names():
    from pathlib import Path
    names = []
    i = 0
    while Path('/dev/video{}'.format(i)).exists():
        p = Path('/sys/class/video4linux/video{}/name'.format(i))
        names.append(p.read_text().strip() if p.exists() else 'Camera {}'.format(i))
        i += 1
    return names or None


def _platform_device_names():
    system = _platform.system()
    if system == 'Darwin':
        return _macos_camera_names()
    if system == 'Linux':
        return _linux_camera_names()
    return None


def _probe_one_frame(index, timeout=5):
    """Open device *index*, decode one frame, and return (width, height) or (None, None)."""
    if _av is None:
        return None, None
    result = [None, None]

    def _do():
        device_str, fmt, opts = _local_device_args(index)
        kw = _device_open_kwargs(fmt, opts)
        try:
            container = _av.open(device_str, **kw)
        except Exception:
            return
        try:
            vs = next((s for s in container.streams if s.type == 'video'), None)
            if vs is None:
                return
            for _ in range(30):
                try:
                    packet = next(container.demux(vs))
                    for frame in packet.decode():
                        result[0], result[1] = frame.width, frame.height
                        return
                except StopIteration:
                    break
                except OSError as exc:
                    if exc.errno == errno.EAGAIN:
                        time.sleep(0.01)
                        continue
                    break
                except Exception:
                    break
        finally:
            try:
                container.close()
            except Exception:
                pass

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result[0], result[1]


def list_devices(probe=False):
    """Return a list of available local camera devices.

    Each entry is a dict:

    * ``'index'``  -- int to pass directly to :class:`Client`
    * ``'name'``   -- human-readable device name, or ``'Camera N'`` if not discoverable
    * ``'width'``  -- pixel width (``None`` unless *probe* is ``True``)
    * ``'height'`` -- pixel height (``None`` unless *probe* is ``True``)

    When *probe* is ``True``, each discovered device is briefly opened to read its
    resolution.  This momentarily activates the camera indicator light per device.

    On Windows without *probe*, an empty list is returned because device presence
    cannot be determined without opening each device.
    """
    names = _platform_device_names()

    if names is not None:
        indices = list(range(len(names)))
    elif probe:
        # Probe sequentially, stopping after the first index that fails to open.
        results = []
        for i in range(16):
            w, h = _probe_one_frame(i)
            if w is None:
                break
            results.append({'index': i, 'name': 'Camera {}'.format(i),
                            'width': w, 'height': h})
        return results
    else:
        return []

    devices = []
    for i, name in zip(indices, names):
        w, h = _probe_one_frame(i) if probe else (None, None)
        devices.append({'index': i, 'name': name, 'width': w, 'height': h})
    return devices


# ---------------------------------------------------------------------------
# Buffered socket reader (handshake phase only)
# ---------------------------------------------------------------------------

class _Reader:
    """Byte-accurate buffered reader over a blocking TCP socket."""

    __slots__ = ('_sock', '_buf')

    def __init__(self, sock):
        self._sock = sock
        self._buf = b''

    def read_until(self, delim):
        while delim not in self._buf:
            chunk = self._sock.recv(_RECV_SIZE)
            if not chunk:
                raise ConnectionError('RTSP connection closed during handshake')
            self._buf += chunk
        idx = self._buf.index(delim) + len(delim)
        result, self._buf = self._buf[:idx], self._buf[idx:]
        return result

    def read_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(_RECV_SIZE)
            if not chunk:
                raise ConnectionError('RTSP connection closed during handshake')
            self._buf += chunk
        result, self._buf = self._buf[:n], self._buf[n:]
        return result

    def take_remainder(self):
        out, self._buf = self._buf, b''
        return out


# ---------------------------------------------------------------------------
# RTP / H.264 demux
# ---------------------------------------------------------------------------

def _demux_h264_payload(payload, fu_buf=b'', fu_started=False):
    """Demux one H.264 RTP payload (RFC 6184).

    Returns ``(nals, fu_buf, fu_started)`` where *nals* is a list of complete
    NAL unit bytes (may be empty while a FU-A fragment is still accumulating).
    *fu_buf* and *fu_started* carry FU-A reassembly state across calls.
    """
    if not payload:
        return [], fu_buf, fu_started

    nal_type = payload[0] & 0x1F

    if 1 <= nal_type <= 23:
        return [payload], b'', False

    if nal_type == 24:  # STAP-A
        nals = []
        i = 1
        while i + 2 <= len(payload):
            sz = struct.unpack('!H', payload[i:i + 2])[0]
            i += 2
            if sz and i + sz <= len(payload):
                nals.append(payload[i:i + sz])
            i += sz
        return nals, b'', False

    if nal_type == 28 and len(payload) >= 2:  # FU-A
        fu_hdr = payload[1]
        is_start = bool(fu_hdr & 0x80)
        is_end = bool(fu_hdr & 0x40)

        if is_start:
            fu_buf = bytes([(payload[0] & 0xE0) | (fu_hdr & 0x1F)]) + payload[2:]
            fu_started = True
        elif fu_started:
            fu_buf += payload[2:]

        if is_end and fu_started:
            return [fu_buf], b'', False

        return [], fu_buf, fu_started

    return [], fu_buf, fu_started


# ---------------------------------------------------------------------------
# SDP parser
# ---------------------------------------------------------------------------

def _parse_sdp(sdp, content_base=''):
    """Parse an SDP string and return ``(track_url, sps_bytes, pps_bytes)``.

    *track_url* is the URL to use in the RTSP SETUP request.
    *sps_bytes* and *pps_bytes* are raw NAL bytes from ``sprop-parameter-sets``
    (both may be ``None`` if the SDP omits them).
    """
    track_url = None
    sprop_sps = None
    sprop_pps = None
    in_video = False
    base = content_base.rstrip('/')

    for raw_line in sdp.splitlines():
        line = raw_line.strip()

        if line.startswith('m='):
            in_video = line.startswith('m=video')
            continue

        if not in_video:
            continue

        m = re.match(r'a=control:(.*)', line)
        if m:
            ctrl = m.group(1).strip()
            if ctrl.startswith('rtsp://') or ctrl.startswith('rtsps://'):
                track_url = ctrl
            elif ctrl == '*':
                track_url = base or None
            else:
                track_url = (base + '/' + ctrl.lstrip('/')) if base else ctrl

        m = re.search(r'sprop-parameter-sets=([^;\s\r\n]+)', line)
        if m:
            parts = m.group(1).split(',')
            try:
                sprop_sps = base64.b64decode(parts[0]) if parts else None
            except Exception:
                pass
            try:
                sprop_pps = base64.b64decode(parts[1]) if len(parts) > 1 else None
            except Exception:
                pass

    return track_url or base, sprop_sps, sprop_pps


# ---------------------------------------------------------------------------
# Public API: _RtspClient (implementation) + Client (factory)
# ---------------------------------------------------------------------------

class _RtspClient:
    """RTSP client using a native Python stack and PyAV for decoding.

    Handles ``rtsp://``, ``rtsps://``, ``http://``, ``https://``, ``tcp://``,
    and local device indices.  Use the ``Client`` factory for automatic
    routing, which dispatches ``rtmp://`` and ``rtmps://`` to RTMPClient.

    Requires PyAV: ``pip install av``
    """

    def __init__(self, rtsp_server_uri, verbose=False):
        self.rtsp_server_uri = rtsp_server_uri
        self._verbose = verbose
        self._queue = None
        self._bg_run = False
        self._width = None
        self._height = None
        self._lock = threading.Lock()
        self._bgt = None

        # Determine open strategy: device index, direct av.open (http/https/tcp), or RTSP socket
        if isinstance(rtsp_server_uri, int):
            device_str, fmt, dev_opts = _local_device_args(rtsp_server_uri)
            self._av_open_args = (device_str,)
            self._av_open_kwargs = _device_open_kwargs(fmt, dev_opts)
            self._is_local = True
        elif isinstance(rtsp_server_uri, str) and rtsp_server_uri.strip().isdigit():
            device_str, fmt, dev_opts = _local_device_args(int(rtsp_server_uri.strip()))
            self._av_open_args = (device_str,)
            self._av_open_kwargs = _device_open_kwargs(fmt, dev_opts)
            self._is_local = True
        else:
            uri = rtsp_server_uri if '//' in rtsp_server_uri else 'rtsp://' + rtsp_server_uri
            parsed = urlparse(uri)
            if parsed.scheme in ('http', 'https', 'tcp'):
                self._av_open_args = (rtsp_server_uri,)
                self._av_open_kwargs = {}
                self._is_local = True
            else:
                self._is_local = False
                self._host = parsed.hostname
                self._port = parsed.port or _DEFAULT_PORT
                self._uri = rtsp_server_uri

        # RTSP-only state
        self._sock = None
        self._session_id = None
        self._cseq = 0
        # av.open-based state (local device or direct network)
        self._av_container = None

        self.open()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ---- public API ----

    def open(self):
        if self.isOpened():
            return self
        if _av is None:
            raise ImportError('Client requires PyAV: pip install av')
        if self._is_local:
            return self._open_local()
        return self._open_rtsp()

    def _open_local(self):
        ready = threading.Event()
        err = []
        self._bg_run = True
        t = threading.Thread(
            target=self._device_loop,
            args=(ready, err),
            daemon=True,
            name='rtsp-native-device',
        )
        t.start()
        self._bgt = t
        ready.wait(timeout=10)
        if err:
            self._bg_run = False
            raise err[0]
        return self

    def _open_rtsp(self):
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        delay = _BACKOFF_INITIAL
        while True:
            try:
                self._sock = socket.create_connection(
                    (self._host, self._port), timeout=_CONNECT_TIMEOUT)
                break
            except ConnectionRefusedError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, _BACKOFF_CAP)
        reader = _Reader(self._sock)
        self._cseq = 0
        self._session_id = None

        try:
            self._rtsp_options(reader)
            sdp, content_base = self._rtsp_describe(reader)
            track_url, sprop_sps, sprop_pps = _parse_sdp(sdp, content_base)
            self._rtsp_setup(reader, track_url)
            self._rtsp_play(reader)
        except Exception:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            raise

        initial_buf = reader.take_remainder()

        if self._verbose:
            log.info('connected to %s', self._uri)

        self._bg_run = True
        t = threading.Thread(
            target=self._recv_loop,
            args=(sprop_sps, sprop_pps, initial_buf),
            daemon=True,
            name='rtsp-native-client',
        )
        t.start()
        self._bgt = t
        return self

    def close(self):
        self._bg_run = False
        if self._is_local:
            container, self._av_container = self._av_container, None
            if container:
                try:
                    container.close()
                except Exception:
                    pass
        else:
            sock, self._sock = self._sock, None
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._bgt:
            self._bgt.join(timeout=2)
            self._bgt = None

    def isOpened(self):
        return self._bg_run

    def read(self, raw=False):
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
        root.title('RTSP')
        label = tk.Label(root)
        label.pack()

        after_id = None

        def _stop():
            nonlocal after_id
            self._bg_run = False
            if after_id is not None:
                try:
                    root.after_cancel(after_id)
                except Exception:
                    pass
                after_id = None
            try:
                root.destroy()
            except Exception:
                pass

        def _tick():
            nonlocal after_id
            after_id = None
            if not self._bg_run:
                try:
                    root.destroy()
                except Exception:
                    pass
                return
            frame = self.read()
            if frame is not None:
                photo = ImageTk.PhotoImage(frame)
                label.config(image=photo)
                label.image = photo
            after_id = root.after(33, _tick)

        root.protocol('WM_DELETE_WINDOW', _stop)
        root.bind_all('<Key>', lambda e: _stop() if e.keysym in ('q', 'Escape') else None)
        root.focus_force()
        _tick()
        root.mainloop()
        self.close()

    # ---- RTSP handshake ----

    def _send(self, method, uri, extra=None):
        self._cseq += 1
        lines = [
            '{} {} RTSP/1.0'.format(method, uri),
            'CSeq: {}'.format(self._cseq),
            'User-Agent: python-rtsp',
        ]
        if self._session_id:
            lines.append('Session: {}'.format(self._session_id))
        if extra:
            lines.extend('{}: {}'.format(k, v) for k, v in extra.items())
        self._sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())

    def _recv_response(self, reader):
        raw = reader.read_until(b'\r\n\r\n')
        lines = raw.decode('utf-8', errors='ignore').rstrip('\r\n').split('\r\n')

        headers = {}
        for line in lines[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip().lower()] = v.strip()

        body = b''
        if 'content-length' in headers:
            body = reader.read_exact(int(headers['content-length']))

        parts = lines[0].split(None, 2)
        code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        if code not in (200, 301, 302):
            raise RuntimeError('RTSP error: {}'.format(lines[0]))

        return code, headers, body

    def _rtsp_options(self, reader):
        self._send('OPTIONS', self._uri)
        self._recv_response(reader)

    def _rtsp_describe(self, reader):
        self._send('DESCRIBE', self._uri, {'Accept': 'application/sdp'})
        _, headers, body = self._recv_response(reader)
        content_base = headers.get('content-base', self._uri).rstrip('/')
        return body.decode('utf-8', errors='ignore'), content_base

    def _rtsp_setup(self, reader, track_url):
        self._send('SETUP', track_url, {
            'Transport': 'RTP/AVP/TCP;unicast;interleaved=0-1',
        })
        _, headers, _ = self._recv_response(reader)
        session = headers.get('session', '')
        self._session_id = session.split(';')[0].strip()

    def _rtsp_play(self, reader):
        self._send('PLAY', self._uri, {'Range': 'npt=0.000-'})
        self._recv_response(reader)

    # ---- local device loop ----

    def _device_loop(self, ready, err):
        try:
            container = _av.open(*self._av_open_args, **self._av_open_kwargs)
        except Exception as exc:
            err.append(RuntimeError(
                'Could not open {!r}: {}'.format(self._av_open_args[0], exc)
            ))
            ready.set()
            self._bg_run = False
            return

        self._av_container = container
        if self._verbose:
            log.info('opened %s', self._av_open_args[0])

        video_stream = next((s for s in container.streams if s.type == 'video'), None)
        if video_stream is None:
            err.append(RuntimeError('No video stream in {!r}'.format(self._av_open_args[0])))
            ready.set()
            self._bg_run = False
            return

        ready.set()
        try:
            while self._bg_run:
                try:
                    for packet in container.demux(video_stream):
                        if not self._bg_run:
                            break
                        for frame in packet.decode():
                            if not self._bg_run:
                                break
                            arr = frame.to_ndarray(format='rgb24')
                            with self._lock:
                                self._queue = arr
                                if self._width is None:
                                    self._width = arr.shape[1]
                                    self._height = arr.shape[0]
                                    if self._verbose:
                                        log.info('device resolution: %dx%d', self._width, self._height)
                    break  # demux ended cleanly
                except OSError as exc:
                    if exc.errno == errno.EAGAIN:
                        # avfoundation returned no frame yet; retry
                        time.sleep(0.005)
                        continue
                    log.debug('device read error: %s', exc)
                    break
        finally:
            self._bg_run = False

    # ---- RTP receive loop ----

    def _recv_loop(self, sprop_sps, sprop_pps, initial_buf):
        codec = _av.CodecContext.create('h264', 'r')
        codec.thread_count = 1  # disable internal threading to avoid concurrent-free crashes

        # Pre-load out-of-band parameter sets so the decoder is ready for the
        # first IDR without needing to see the in-band SPS/PPS first.
        params = b''
        if sprop_sps:
            params += b'\x00\x00\x00\x01' + sprop_sps
        if sprop_pps:
            params += b'\x00\x00\x00\x01' + sprop_pps
        if params:
            self._decode_access_unit(codec, params)

        buf = initial_buf
        fu_buf = b''
        fu_started = False
        idr_seen = False  # gate: hold all slice data until an IDR is in the DPB
        au_nals = []      # NALs accumulating for the current access unit

        while self._bg_run:
            while len(buf) < 4:
                try:
                    chunk = self._sock.recv(_RECV_SIZE)
                except OSError:
                    self._bg_run = False
                    return
                if not chunk:
                    self._bg_run = False
                    return
                buf += chunk

            if buf[0:1] != b'$':
                idx = buf.find(b'$')
                buf = buf[idx:] if idx >= 0 else b''
                continue

            channel = buf[1]
            length = struct.unpack('!H', buf[2:4])[0]

            needed = 4 + length
            while len(buf) < needed:
                try:
                    chunk = self._sock.recv(_RECV_SIZE)
                except OSError:
                    self._bg_run = False
                    return
                if not chunk:
                    self._bg_run = False
                    return
                buf += chunk

            rtp = buf[4:needed]
            buf = buf[needed:]

            if channel != 0 or len(rtp) < 12:
                continue

            # RTP byte 1: M(1) PT(7). Marker bit signals end of access unit.
            marker = bool(rtp[1] & 0x80)

            cc = rtp[0] & 0x0F
            hdr = 12 + cc * 4
            if rtp[0] & 0x10:
                if len(rtp) < hdr + 4:
                    continue
                xlen = struct.unpack('!H', rtp[hdr + 2:hdr + 4])[0]
                hdr += 4 + xlen * 4

            if len(rtp) <= hdr:
                continue
            payload = rtp[hdr:]

            if rtp[0] & 0x20 and rtp[-1] < len(payload):
                payload = payload[:-rtp[-1]]

            nals, fu_buf, fu_started = _demux_h264_payload(payload, fu_buf, fu_started)
            for nal in nals:
                nal_t = nal[0] & 0x1F if nal else 0
                if not idr_seen:
                    if nal_t == 5:       # IDR: open the gate
                        idr_seen = True
                    elif nal_t not in (7, 8):  # pass SPS/PPS, drop everything else
                        continue
                au_nals.append(nal)

            # Decode the complete access unit when the marker bit arrives.
            # RFC 6184 §5.1: marker=1 on the last RTP packet of every access unit.
            if marker and au_nals:
                au_data = b''.join(b'\x00\x00\x00\x01' + n for n in au_nals)
                self._decode_access_unit(codec, au_data)
                au_nals = []

        self._bg_run = False

    def _decode_access_unit(self, codec, data):
        """Decode one complete Annex-B access unit and update the frame queue."""
        try:
            for frame in codec.decode(_av.Packet(data)):
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
            log.debug('H.264 decode error: %s', exc)


def Client(rtsp_server_uri, verbose=False):
    """Return an _RtspClient or RTMPClient for *rtsp_server_uri*.

    Routes ``rtmp://`` and ``rtmps://`` URIs to RTMPClient.  All other URIs
    (``rtsp://``, ``rtsps://``, ``http://``, ``https://``, ``tcp://``, and
    local device indices) return an _RtspClient.
    """
    uri = rtsp_server_uri
    if isinstance(uri, str) and '//' not in uri and not uri.strip().isdigit():
        uri = 'rtsp://' + uri
    if isinstance(uri, str):
        from urllib.parse import urlparse as _urlparse
        if _urlparse(uri).scheme in ('rtmp', 'rtmps'):
            from .rtmp import RTMPClient
            return RTMPClient(rtsp_server_uri, verbose=verbose)
    return _RtspClient(rtsp_server_uri, verbose=verbose)
