"""Python-native RTSP/RTP server and publisher.

Encodes frames to H.264 via PyAV (libx264), then handles RTSP session
negotiation and RTP packetization in Python.  No FFmpeg subprocess is spawned.

Source    — built-in RTSP server; clients connect to it directly.
Publisher — outbound RTSP publisher; pushes to a relay (mediamtx etc.)
                  via ANNOUNCE/SETUP/RECORD.

Requires PyAV: ``pip install av``
"""

import asyncio
import base64
import logging
import random
import re
import socket
import struct
import time
from fractions import Fraction
from threading import Event, Lock, Thread
from urllib.parse import urlparse

try:
    import av as _av
except ImportError:
    _av = None

import numpy as np
from PIL import Image

from ._utils import _enable_verbose, _parse_uri, _to_pil

log = logging.getLogger('rtsp.native')

_MIN_ENCODE_FPS = 10
_START4 = b'\x00\x00\x00\x01'
_START3 = b'\x00\x00\x01'
_MAX_RTP_PAYLOAD = 1400
_SESSION_TIMEOUT = 60
_RECV_SIZE = 65536
_DEFAULT_PORT = 554


# ---------------------------------------------------------------------------
# Buffered socket reader (used during RTSP handshake)
# ---------------------------------------------------------------------------

class _Reader:
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


# ---------------------------------------------------------------------------
# H.264 Annex-B parsing
# ---------------------------------------------------------------------------

def _split_nals(data: bytes) -> tuple[list[bytes], bytes]:
    """Extract complete NAL units from an Annex-B buffer.

    Returns (complete_nals, remainder) where remainder starts at the last
    start code (held back because the NAL may not yet be complete).
    """
    positions: list[tuple[int, int]] = []  # (offset, start_code_len)
    i = 0
    n = len(data)
    while i < n - 2:
        if data[i:i + 4] == _START4:
            positions.append((i, 4))
            i += 4
        elif data[i:i + 3] == _START3:
            positions.append((i, 3))
            i += 3
        else:
            i += 1

    if len(positions) < 2:
        return [], data

    nals: list[bytes] = []
    for j in range(len(positions) - 1):
        pos, sc = positions[j]
        end = positions[j + 1][0]
        nal = data[pos + sc:end]
        if nal:
            nals.append(nal)

    remainder = data[positions[-1][0]:]
    return nals, remainder


def _nal_type(nal: bytes) -> int:
    return nal[0] & 0x1F if nal else 0


# ---------------------------------------------------------------------------
# RTP packetizer (RFC 6184 - H.264)
# ---------------------------------------------------------------------------

class _RTPPacketizer:
    PT = 96  # dynamic payload type for H.264

    def __init__(self) -> None:
        self.ssrc = random.randint(1, 0xFFFFFFFF)
        self.seq = random.randint(0, 0xFFFF)

    def _header(self, marker: bool, ts: int) -> bytes:
        self.seq = (self.seq + 1) & 0xFFFF
        return struct.pack('!BBHII',
            0x80,
            (0x80 if marker else 0) | self.PT,
            self.seq,
            ts,
            self.ssrc,
        )

    def packetize(self, nal: bytes, ts: int, last_nal: bool) -> list[bytes]:
        """Return RTP packet(s) for one NAL unit."""
        if len(nal) <= _MAX_RTP_PAYLOAD:
            return [self._header(last_nal, ts) + nal]

        # FU-A fragmentation
        nal_hdr = nal[0]
        nal_type = nal_hdr & 0x1F
        nri = nal_hdr & 0x60
        fu_ind = nri | 28          # FU-A indicator byte
        payload = nal[1:]
        pkts: list[bytes] = []
        offset = 0
        first = True
        while offset < len(payload):
            chunk = payload[offset:offset + _MAX_RTP_PAYLOAD - 2]
            is_last = offset + len(chunk) >= len(payload)
            fu_hdr = nal_type | (0x80 if first else 0) | (0x40 if is_last else 0)
            pkts.append(
                self._header(last_nal and is_last, ts)
                + bytes([fu_ind, fu_hdr])
                + chunk
            )
            offset += len(chunk)
            first = False
        return pkts


# ---------------------------------------------------------------------------
# Per-client RTSP session
# ---------------------------------------------------------------------------

class _Session:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 server: '_RTSPServer', loop: asyncio.AbstractEventLoop) -> None:
        self._reader = reader
        self._writer = writer
        self._server = server
        self._loop = loop
        self._id = '{:08X}'.format(random.randint(0, 0xFFFFFFFF))
        self._packetizer = _RTPPacketizer()
        self._udp_sock: socket.socket | None = None
        self._rtp_addr: tuple[str, int] | None = None
        self._tcp_channel: int | None = None
        self._playing = False

    # ---- RTP delivery ----

    def send_rtp(self, pkt: bytes) -> None:
        """Thread-safe. Called from the H.264 reader thread."""
        if not self._playing:
            return
        if self._udp_sock is not None:
            try:
                self._udp_sock.sendto(pkt, self._rtp_addr)
            except OSError:
                pass
        elif self._tcp_channel is not None:
            frame = (b'$' + bytes([self._tcp_channel])
                     + struct.pack('!H', len(pkt)) + pkt)
            def _write(w=self._writer, d=frame):
                if not w.is_closing():
                    w.write(d)
            self._loop.call_soon_threadsafe(_write)

    # ---- RTSP protocol ----

    async def run(self) -> None:
        peer = self._writer.get_extra_info('peername')
        log.debug('connection from %s', peer)
        try:
            while True:
                req = await self._read_request()
                if req is None:
                    break
                method, uri, headers, body = req
                await self._dispatch(method, uri, headers, peer)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._playing = False
            if self._udp_sock:
                self._udp_sock.close()
            try:
                self._writer.close()
            except Exception:
                pass
            self._server._drop(self)
            log.debug('connection closed from %s', peer)

    async def _read_request(self):
        try:
            raw = await self._reader.readline()
        except Exception:
            return None
        line = raw.decode('utf-8', errors='ignore').strip()
        if not line:
            raw = await self._reader.readline()
            line = raw.decode('utf-8', errors='ignore').strip()
        if not line:
            return None
        parts = line.split(' ', 2)
        if len(parts) < 2:
            return None
        method, uri = parts[0], parts[1]

        headers: dict[str, str] = {}
        while True:
            raw = await self._reader.readline()
            h = raw.decode('utf-8', errors='ignore').strip()
            if not h:
                break
            if ':' in h:
                k, v = h.split(':', 1)
                headers[k.strip().lower()] = v.strip()

        body = b''
        if 'content-length' in headers:
            try:
                body = await self._reader.readexactly(int(headers['content-length']))
            except Exception:
                pass
        return method, uri, headers, body

    def _reply(self, cseq: str, status: str,
               extra: dict[str, str] | None = None, body: bytes = b'') -> None:
        lines = ['RTSP/1.0 {}\r\nCSeq: {}'.format(status, cseq)]
        if extra:
            for k, v in extra.items():
                lines.append('{}: {}'.format(k, v))
        if body:
            lines.append('Content-Length: {}'.format(len(body)))
        msg = ('\r\n'.join(lines) + '\r\n\r\n').encode()
        if body:
            msg += body
        self._writer.write(msg)

    async def _dispatch(self, method: str, uri: str,
                        headers: dict[str, str], peer) -> None:
        cseq = headers.get('cseq', '0')

        if method == 'OPTIONS':
            self._reply(cseq, '200 OK', {
                'Public': 'OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN',
            })

        elif method == 'DESCRIBE':
            sdp = self._server._sdp().encode()
            self._reply(cseq, '200 OK', {
                'Content-Type': 'application/sdp',
                'Content-Base': uri.rstrip('/') + '/',
            }, sdp)

        elif method == 'SETUP':
            transport_hdr = headers.get('transport', '')
            ok = self._setup_transport(transport_hdr, peer)
            if not ok:
                self._reply(cseq, '461 Unsupported Transport')
                return
            self._reply(cseq, '200 OK', {
                'Session': '{};timeout={}'.format(self._id, _SESSION_TIMEOUT),
                'Transport': self._transport_response(transport_hdr),
            })

        elif method == 'PLAY':
            self._playing = True
            self._reply(cseq, '200 OK', {
                'Session': self._id,
                'RTP-Info': 'url={};seq={};rtptime=0'.format(
                    uri, self._packetizer.seq),
            })
            log.info('client %s playing', peer)

        elif method in ('TEARDOWN', 'GET_PARAMETER'):
            self._playing = False
            self._reply(cseq, '200 OK', {'Session': self._id})

        else:
            self._reply(cseq, '501 Not Implemented')

    def _setup_transport(self, hdr: str, peer) -> bool:
        tcp = 'RTP/AVP/TCP' in hdr
        if tcp:
            m = re.search(r'interleaved=(\d+)', hdr)
            self._tcp_channel = int(m.group(1)) if m else 0
            self._udp_sock = None
            return True

        # UDP
        m = re.search(r'client_port=(\d+)', hdr)
        if not m:
            return False
        client_rtp = int(m.group(1))
        host = peer[0] if peer else '127.0.0.1'
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('', 0))
        self._udp_sock = sock
        self._rtp_addr = (host, client_rtp)
        self._tcp_channel = None
        return True

    def _transport_response(self, hdr: str) -> str:
        if self._tcp_channel is not None:
            return 'RTP/AVP/TCP;unicast;interleaved={}-{}'.format(
                self._tcp_channel, self._tcp_channel + 1)
        port = self._udp_sock.getsockname()[1]
        client_port = self._rtp_addr[1]
        return 'RTP/AVP;unicast;client_port={}-{};server_port={}-{}'.format(
            client_port, client_port + 1, port, port + 1)


# ---------------------------------------------------------------------------
# RTSP server (asyncio)
# ---------------------------------------------------------------------------

class _RTSPServer:
    def __init__(self, host: str, port: int, sdp_fn) -> None:
        self._host = host
        self._port = port
        self._sdp_fn = sdp_fn
        self._sessions: list[_Session] = []
        self._tasks: set[asyncio.Task] = set()
        self._lock = Lock()
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _sdp(self) -> str:
        return self._sdp_fn()

    def _drop(self, s: _Session) -> None:
        with self._lock:
            self._sessions = [x for x in self._sessions if x is not s]

    def broadcast(self, nal: bytes, ts: int, last_nal: bool) -> None:
        with self._lock:
            sessions = list(self._sessions)
        packetizer_calls = [(s, s._packetizer.packetize(nal, ts, last_nal))
                            for s in sessions]
        for s, pkts in packetizer_calls:
            for pkt in pkts:
                s.send_rtp(pkt)

    async def _on_connect(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_event_loop()
        session = _Session(reader, writer, self, loop)
        with self._lock:
            self._sessions.append(session)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await session.run()
        finally:
            self._tasks.discard(task)

    async def start(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._server = await asyncio.start_server(
            self._on_connect, self._host, self._port)
        log.debug('RTSP server listening on %s:%d', self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Public API: Source
# ---------------------------------------------------------------------------

class Source:
    """Serve frames as a real RTSP stream using a Python-native server.

    Encodes to H.264 via PyAV (libx264), manages RTSP sessions and RTP
    packetization in Python.  No FFmpeg subprocess.  Supports multiple
    simultaneous clients with no external relay.

    Requires PyAV: ``pip install av``

    Usage::

        with rtsp.Source('rtsp://0.0.0.0:8554/live', fps=25,
                         frame_buffer=images) as src:
            src.wait_ready()
            print(src.client_uri)   # rtsp://127.0.0.1:8554/live
            src.serve_forever()
    """

    def __init__(self, rtsp_server_uri: str, fps: float = 25,
                 verbose: bool = False, size: tuple[int, int] | None = None,
                 frame_buffer=None) -> None:
        if _av is None:
            raise ImportError('Source requires PyAV: pip install av')

        _, uri = _parse_uri(rtsp_server_uri)
        parsed = urlparse(uri)
        self._host = parsed.hostname or '0.0.0.0'
        self._port = parsed.port or 8554
        self._path = parsed.path or '/live'
        self._fps = fps
        self._verbose = verbose
        if verbose:
            _enable_verbose()
        self._size: tuple[int, int] | None = None
        self._buffer: list[Image.Image] = []
        self._lock = Lock()
        self._ready = Event()
        self._bg_run = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rtsp: _RTSPServer | None = None
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._loader: Thread | None = None

        if size is not None:
            w, h = size
            self._size = (w & ~1, h & ~1)
            self._start()

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

    # ---- frame input ----

    def put(self, frame) -> None:
        """Add a frame to the buffer. Starts the server on the first call."""
        frame = _to_pil(frame)
        if self._size is None:
            self._size = (frame.width & ~1, frame.height & ~1)
            self._start()
        elif frame.size != self._size:
            frame = frame.resize(self._size)
        with self._lock:
            self._buffer.append(frame)

    # ---- lifecycle ----

    def _start(self) -> None:
        if self._bg_run:
            return
        self._bg_run = True

        self._loop = asyncio.new_event_loop()
        self._rtsp = _RTSPServer('0.0.0.0', self._port, self._sdp)

        def _run_loop() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._rtsp.start())
            self._ready.set()
            log.info('serving at %g fps on %s', self._fps, self.client_uri)
            self._loop.run_forever()

        Thread(target=_run_loop, daemon=True, name='rtsp-native-loop').start()
        Thread(target=self._encode_loop, daemon=True, name='rtsp-native-encode').start()

    def close(self) -> None:
        self._bg_run = False
        if self._loop and self._loop.is_running():
            if self._rtsp:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._rtsp.stop(), self._loop
                    ).result(timeout=5.0)
                except Exception:
                    pass
            self._loop.call_soon_threadsafe(self._loop.stop)

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Block until the RTSP server is listening. Returns True on success."""
        return self._ready.wait(timeout=timeout)

    def serve_forever(self) -> None:
        """Block until KeyboardInterrupt, keeping the server alive."""
        try:
            while self._bg_run:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    @property
    def client_uri(self) -> str:
        host = self._host if self._host not in ('0.0.0.0', '') else '127.0.0.1'
        return 'rtsp://{}:{}{}'.format(host, self._port, self._path)

    def isOpened(self) -> bool:
        return self._bg_run

    # ---- SDP ----

    def _sdp(self) -> str:
        sprop = ''
        if self._sps and self._pps:
            s64 = base64.b64encode(self._sps).decode()
            p64 = base64.b64encode(self._pps).decode()
            sprop = ';sprop-parameter-sets={},{}'.format(s64, p64)
        return (
            'v=0\r\n'
            'o=- 0 0 IN IP4 127.0.0.1\r\n'
            's=live\r\n'
            'c=IN IP4 0.0.0.0\r\n'
            't=0 0\r\n'
            'm=video 0 RTP/AVP 96\r\n'
            'a=rtpmap:96 H264/90000\r\n'
            'a=fmtp:96 packetization-mode=1{}\r\n'
            'a=control:trackID=0\r\n'
        ).format(sprop)

    # ---- background thread ----

    def _encode_loop(self) -> None:
        """Encode frames with PyAV/libx264 and broadcast NAL units as RTP."""
        w, h = self._size
        encode_fps = max(self._fps, _MIN_ENCODE_FPS)
        encode_interval = 1.0 / encode_fps
        advance_interval = 1.0 / self._fps
        ts_increment = int(90000 / encode_fps)

        codec = _av.CodecContext.create('libx264', 'w')
        codec.width = w
        codec.height = h
        codec.pix_fmt = 'yuv420p'
        codec.framerate = Fraction(int(encode_fps), 1)
        codec.time_base = Fraction(1, int(encode_fps))
        codec.gop_size = int(encode_fps)
        codec.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'forced-idr': '1'}
        codec.open()

        pts = 0
        ts = 0
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

            for pkt in codec.encode(av_frame):
                raw = bytes(pkt)
                if not raw:
                    continue
                # Sentinel forces _split_nals to yield the final NAL in each complete packet.
                nals, _ = _split_nals(raw + b'\x00\x00\x00\x01')
                for nal in nals:
                    t = _nal_type(nal)
                    if t == 7:
                        self._sps = nal
                    elif t == 8:
                        self._pps = nal
                for i, nal in enumerate(nals):
                    self._rtsp.broadcast(nal, ts, i == len(nals) - 1)
                ts = (ts + ts_increment) & 0xFFFFFFFF

            next_encode += encode_interval
            delay = next_encode - time.monotonic()
            if delay > 0:
                time.sleep(delay)


# ---------------------------------------------------------------------------
# Public API: Publisher
# ---------------------------------------------------------------------------

class Publisher:
    """Push frames to an RTSP relay (mediamtx etc.) via ANNOUNCE/RECORD.

    Encodes H.264 with PyAV, packetizes as RTP, and delivers over a single
    TCP connection to the relay.  No FFmpeg subprocess.  Same frame-input
    API as ``Source``: ``put()``, ``open()``, ``close()``, ``isOpened()``.

    Requires PyAV: ``pip install av``
    """

    def __init__(self, rtsp_server_uri: str, fps: float = 25,
                 verbose: bool = False, size: tuple[int, int] | None = None,
                 frame_buffer=None) -> None:
        if _av is None:
            raise ImportError('Publisher requires PyAV: pip install av')

        kind, uri = _parse_uri(rtsp_server_uri)
        if kind != 'network':
            raise ValueError(
                'Source URI must be a network address, e.g. rtsp://localhost:8554/live'
            )
        from urllib.parse import urlparse as _up
        parsed = _up(uri)
        self._host = parsed.hostname
        self._port = parsed.port or _DEFAULT_PORT
        self._uri = uri
        self._fps = fps
        self._verbose = verbose
        if verbose:
            _enable_verbose()
        self._size: tuple[int, int] | None = None
        self._buffer: list[Image.Image] = []
        self._lock = Lock()
        self._bg_run = False
        self._sock: socket.socket | None = None
        self._cseq = 0
        self._session_id: str | None = None
        self._loader: Thread | None = None

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
        """Connect to relay and begin ANNOUNCE/SETUP/RECORD."""
        if self.isOpened() or self._size is None:
            return self

        self._cseq = 0
        self._session_id = None
        self._sock = socket.create_connection((self._host, self._port), timeout=15)
        reader = _Reader(self._sock)

        try:
            self._rtsp_announce(reader)
            self._rtsp_setup(reader)
            self._rtsp_record(reader)
        except Exception:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            raise

        if self._verbose:
            log.info('publishing to %s', self._uri)

        self._bg_run = True
        Thread(target=self._encode_loop, daemon=True,
               name='rtsp-native-publish').start()
        return self

    def close(self) -> None:
        self._bg_run = False
        if self._loader and self._loader.is_alive():
            self._loader.join(timeout=10)
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def isOpened(self) -> bool:
        return self._bg_run

    def serve_forever(self):
        raise RuntimeError(
            'serve_forever() requires serve=True; use '
            'rtsp.Source(..., serve=True) for a built-in server.'
        )

    # ---- RTSP handshake ----

    def _send(self, method: str, uri: str,
              extra: dict | None = None, body: bytes = b'') -> None:
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
        if body:
            lines.append('Content-Length: {}'.format(len(body)))
        msg = ('\r\n'.join(lines) + '\r\n\r\n').encode()
        if body:
            msg += body
        self._sock.sendall(msg)

    def _recv_response(self, reader: _Reader):
        raw = reader.read_until(b'\r\n\r\n')
        lines = raw.decode('utf-8', errors='ignore').rstrip('\r\n').split('\r\n')
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip().lower()] = v.strip()
        body = b''
        if 'content-length' in headers:
            body = reader.read_exact(int(headers['content-length']))
        parts = lines[0].split(None, 2)
        code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        if code not in (200, 201):
            raise RuntimeError('RTSP error: {}'.format(lines[0]))
        return code, headers, body

    def _rtsp_announce(self, reader: _Reader) -> None:
        sdp = (
            'v=0\r\n'
            'o=- 0 0 IN IP4 127.0.0.1\r\n'
            's=live\r\n'
            'c=IN IP4 0.0.0.0\r\n'
            't=0 0\r\n'
            'm=video 0 RTP/AVP 96\r\n'
            'a=rtpmap:96 H264/90000\r\n'
            'a=fmtp:96 packetization-mode=1\r\n'
            'a=control:trackID=0\r\n'
        ).encode()
        self._send('ANNOUNCE', self._uri, {'Content-Type': 'application/sdp'}, sdp)
        self._recv_response(reader)

    def _rtsp_setup(self, reader: _Reader) -> None:
        track_uri = self._uri.rstrip('/') + '/trackID=0'
        self._send('SETUP', track_uri, {
            'Transport': 'RTP/AVP/TCP;unicast;interleaved=0-1',
        })
        _, headers, _ = self._recv_response(reader)
        session = headers.get('session', '')
        self._session_id = session.split(';')[0].strip()

    def _rtsp_record(self, reader: _Reader) -> None:
        self._send('RECORD', self._uri, {'Range': 'npt=0.000-'})
        self._recv_response(reader)

    # ---- background thread ----

    def _encode_loop(self) -> None:
        """Encode frames with PyAV/libx264 and send RTP packets to relay."""
        w, h = self._size
        encode_fps = max(self._fps, _MIN_ENCODE_FPS)
        encode_interval = 1.0 / encode_fps
        advance_interval = 1.0 / self._fps
        ts_increment = int(90000 / encode_fps)

        codec = _av.CodecContext.create('libx264', 'w')
        codec.width = w
        codec.height = h
        codec.pix_fmt = 'yuv420p'
        codec.framerate = Fraction(int(encode_fps), 1)
        codec.time_base = Fraction(1, int(encode_fps))
        codec.gop_size = int(encode_fps)
        codec.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'forced-idr': '1'}
        codec.open()

        packetizer = _RTPPacketizer()
        pts = 0
        ts = 0
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

            for pkt in codec.encode(av_frame):
                raw = bytes(pkt)
                if not raw:
                    continue
                nals, _ = _split_nals(raw + b'\x00\x00\x00\x01')
                for i, nal in enumerate(nals):
                    for rtp_pkt in packetizer.packetize(nal, ts, i == len(nals) - 1):
                        self._send_rtp(rtp_pkt)
                ts = (ts + ts_increment) & 0xFFFFFFFF

            next_encode += encode_interval
            delay = next_encode - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def _send_rtp(self, pkt: bytes) -> None:
        frame = b'$\x00' + struct.pack('!H', len(pkt)) + pkt
        try:
            self._sock.sendall(frame)
        except OSError:
            self._bg_run = False
