"""Tests for rtsp.Publisher — Python-native RTSP push publisher.

Unit tests cover the ANNOUNCE/SETUP/RECORD handshake using a fake TCP relay
that speaks just enough RTSP to accept the connection.  No PyAV or real relay
required for unit tests.

Integration tests use a real mediamtx relay and verify that Client can
receive frames published by Publisher.
"""

import socket
import struct
import threading
import time

import pytest
from PIL import Image

import rtsp
from rtsp.source import Publisher

from conftest import requires_mediamtx

requires_av = pytest.mark.skipif(
    __import__('importlib').util.find_spec('av') is None,
    reason='PyAV (av) not installed',
)


# ---------------------------------------------------------------------------
# Fake relay — accepts ANNOUNCE/SETUP/RECORD and records what it receives
# ---------------------------------------------------------------------------

class _FakeRelay:
    """Minimal TCP server that accepts one RTSP ANNOUNCE/SETUP/RECORD session."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.settimeout(5)
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]

        self.received_methods: list[str] = []
        self.sdp_body: bytes = b''
        self.rtp_packets: list[bytes] = []
        self._conn: socket.socket | None = None
        self._session_id = 'fakesession123'

        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        self._conn = conn
        conn.settimeout(10)
        buf = b''
        cseq = 0

        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk

            while b'\r\n\r\n' in buf:
                hdr_end = buf.index(b'\r\n\r\n') + 4
                header_block = buf[:hdr_end].decode('utf-8', errors='ignore')
                lines = header_block.strip().split('\r\n')
                headers = {}
                for line in lines[1:]:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        headers[k.strip().lower()] = v.strip()
                cseq = headers.get('cseq', '0')
                content_len = int(headers.get('content-length', 0))

                buf = buf[hdr_end:]
                while len(buf) < content_len:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                body = buf[:content_len]
                buf = buf[content_len:]

                method = lines[0].split()[0]
                self.received_methods.append(method)

                if method == 'ANNOUNCE':
                    self.sdp_body = body
                    reply = 'RTSP/1.0 200 OK\r\nCSeq: {}\r\n\r\n'.format(cseq)
                elif method == 'SETUP':
                    reply = (
                        'RTSP/1.0 200 OK\r\nCSeq: {}\r\n'
                        'Session: {}\r\n'
                        'Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n'
                    ).format(cseq, self._session_id)
                elif method == 'RECORD':
                    reply = 'RTSP/1.0 200 OK\r\nCSeq: {}\r\nSession: {}\r\n\r\n'.format(
                        cseq, self._session_id)
                    conn.sendall(reply.encode())
                    self._drain_rtp(conn, buf)
                    return
                else:
                    reply = 'RTSP/1.0 200 OK\r\nCSeq: {}\r\n\r\n'.format(cseq)

                conn.sendall(reply.encode())

    def _drain_rtp(self, conn, initial_buf):
        buf = initial_buf
        while True:
            while len(buf) < 4:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk

            if buf[0:1] != b'$':
                break

            length = struct.unpack('!H', buf[2:4])[0]
            needed = 4 + length
            while len(buf) < needed:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk

            self.rtp_packets.append(buf[4:needed])
            buf = buf[needed:]

    def wait_for_methods(self, methods, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(m in self.received_methods for m in methods):
                return True
            time.sleep(0.05)
        return False

    def wait_for_rtp(self, count=1, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.rtp_packets) >= count:
                return True
            time.sleep(0.05)
        return False

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass
        if self._conn:
            try:
                self._conn.close()
            except OSError:
                pass


@pytest.fixture
def fake_relay():
    relay = _FakeRelay()
    yield relay
    relay.close()


# ---------------------------------------------------------------------------
# Unit: RTSP handshake
# ---------------------------------------------------------------------------

class TestHandshake:

    def test_announce_is_sent_first(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        pub = Publisher(uri, size=(64, 64))
        assert fake_relay.wait_for_methods(['ANNOUNCE'])
        assert fake_relay.received_methods[0] == 'ANNOUNCE'
        pub.close()

    def test_setup_follows_announce(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        pub = Publisher(uri, size=(64, 64))
        assert fake_relay.wait_for_methods(['ANNOUNCE', 'SETUP', 'RECORD'])
        assert fake_relay.received_methods == ['ANNOUNCE', 'SETUP', 'RECORD']
        pub.close()

    def test_sdp_contains_h264(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        pub = Publisher(uri, size=(64, 64))
        fake_relay.wait_for_methods(['ANNOUNCE'])
        pub.close()
        sdp = fake_relay.sdp_body.decode()
        assert 'H264' in sdp
        assert 'RTP/AVP' in sdp

    def test_session_id_stored_after_setup(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        pub = Publisher(uri, size=(64, 64))
        fake_relay.wait_for_methods(['SETUP'])
        pub.close()
        assert pub._session_id == 'fakesession123'

    def test_is_opened_after_connect(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        pub = Publisher(uri, size=(64, 64))
        fake_relay.wait_for_methods(['RECORD'])
        assert pub.isOpened()
        pub.close()

    def test_is_not_opened_after_close(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        pub = Publisher(uri, size=(64, 64))
        fake_relay.wait_for_methods(['RECORD'])
        pub.close()
        assert not pub.isOpened()

    def test_close_is_idempotent(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        pub = Publisher(uri, size=(64, 64))
        fake_relay.wait_for_methods(['RECORD'])
        pub.close()
        pub.close()

    def test_context_manager(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        with Publisher(uri, size=(64, 64)) as pub:
            fake_relay.wait_for_methods(['RECORD'])
            assert pub.isOpened()
        assert not pub.isOpened()

    def test_connection_refused_raises(self):
        with pytest.raises(OSError):
            Publisher('rtsp://127.0.0.1:1/live', size=(64, 64))

    def test_serve_forever_raises(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        with Publisher(uri, size=(64, 64)) as pub:
            fake_relay.wait_for_methods(['RECORD'])
            with pytest.raises(RuntimeError, match='serve=True'):
                pub.serve_forever()


# ---------------------------------------------------------------------------
# Unit: RTP delivery (requires PyAV for encoding)
# ---------------------------------------------------------------------------

@requires_av
class TestRTPDelivery:

    def test_rtp_packets_sent_after_put(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        frame = Image.new('RGB', (64, 64), color=(128, 64, 32))
        with Publisher(uri, size=(64, 64)) as pub:
            assert fake_relay.wait_for_methods(['RECORD'])
            pub.put(frame)
            assert fake_relay.wait_for_rtp(count=1, timeout=8)

    def test_rtp_packets_have_valid_header(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        frame = Image.new('RGB', (64, 64))
        with Publisher(uri, size=(64, 64)) as pub:
            assert fake_relay.wait_for_methods(['RECORD'])
            pub.put(frame)
            fake_relay.wait_for_rtp(count=1, timeout=8)

        assert fake_relay.rtp_packets
        pkt = fake_relay.rtp_packets[0]
        assert len(pkt) >= 12
        assert pkt[0] & 0xC0 == 0x80       # RTP version=2
        assert pkt[1] & 0x7F == 96         # payload type 96 (H.264)

    def test_source_serve_false_routes_to_native_publisher(self, fake_relay):
        uri = 'rtsp://127.0.0.1:{}/live'.format(fake_relay.port)
        src = rtsp.Source(uri, size=(64, 64), serve=False)
        assert isinstance(src, Publisher)
        assert fake_relay.wait_for_methods(['RECORD'])
        src.close()


# ---------------------------------------------------------------------------
# Integration: Publisher → mediamtx → Client round-trip
# ---------------------------------------------------------------------------

@requires_mediamtx
@requires_av
class TestMediamtxRoundTrip:

    def test_client_receives_frame_from_publisher(self, mediamtx_server, nouveau_frames):
        from rtsp.client import Client

        path = '/pub_test'
        pub_uri = mediamtx_server + path
        client_uri = mediamtx_server + path

        frames = [Image.open(p).convert('RGB') for p in nouveau_frames[:3]]
        with Publisher(pub_uri, fps=5, frame_buffer=frames) as pub:
            assert pub.isOpened()
            time.sleep(1.5)  # let mediamtx pick up the stream
            with Client(client_uri) as client:
                deadline = time.monotonic() + 15
                received = None
                while time.monotonic() < deadline:
                    received = client.read()
                    if received is not None:
                        break
                    time.sleep(0.1)

        assert received is not None, 'Client got no frame from mediamtx relay'
        assert isinstance(received, Image.Image)
