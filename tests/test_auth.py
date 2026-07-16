"""Tests for RTSP authentication (Basic and Digest).

Unit tests cover the shared ``_RtspAuth`` helper and ``_split_credentials`` in
``rtsp._utils``.  Regression tests drive the native ``Client`` against an
in-process mock RTSP server that issues a 401 ``WWW-Authenticate`` challenge and
validates the ``Authorization`` header the client sends back, reproducing the
LIVE555 IP-camera handshake that previously failed with a bare 401.
"""

import hashlib
import socket
import struct
import threading
import time
from unittest.mock import MagicMock

import pytest

import rtsp
from rtsp import client as _client
from rtsp import source as _source
from rtsp._utils import _Reader, _RtspAuth, _split_credentials


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Unit: _split_credentials
# ---------------------------------------------------------------------------

class TestSplitCredentials:

    def test_user_and_password(self):
        user, password, clean = _split_credentials('rtsp://218:420@host:8554/720p')
        assert user == '218'
        assert password == '420'
        assert clean == 'rtsp://host:8554/720p'
        assert '@' not in clean

    def test_no_credentials_returns_unchanged(self):
        user, password, clean = _split_credentials('rtsp://host:8554/live')
        assert user is None
        assert password is None
        assert clean == 'rtsp://host:8554/live'

    def test_username_only(self):
        user, password, clean = _split_credentials('rtsp://admin@host/live')
        assert user == 'admin'
        assert password is None
        assert clean == 'rtsp://host/live'

    def test_percent_encoded_credentials_decoded(self):
        user, password, clean = _split_credentials(
            'rtsp://us%40er:p%3Ass@host:554/s')
        assert user == 'us@er'
        assert password == 'p:ss'
        assert clean == 'rtsp://host:554/s'

    def test_default_port_omitted_from_clean_uri(self):
        _, _, clean = _split_credentials('rtsp://u:p@host/live')
        assert clean == 'rtsp://host/live'


# ---------------------------------------------------------------------------
# Unit: _RtspAuth
# ---------------------------------------------------------------------------

class TestRtspAuthProperties:

    def test_no_credentials(self):
        auth = _RtspAuth(None, None)
        assert auth.has_credentials is False
        assert auth.negotiated is False

    def test_has_credentials_with_user_only(self):
        assert _RtspAuth('user', None).has_credentials is True

    def test_has_credentials_with_password_only(self):
        assert _RtspAuth(None, 'pw').has_credentials is True

    def test_header_none_before_challenge(self):
        assert _RtspAuth('u', 'p').header('DESCRIBE', 'rtsp://h/s') is None


class TestRtspAuthChallenge:

    def test_missing_header_returns_false(self):
        auth = _RtspAuth('u', 'p')
        assert auth.challenge({}) is False
        assert auth.negotiated is False

    def test_no_credentials_returns_false(self):
        auth = _RtspAuth(None, None)
        assert auth.challenge({'www-authenticate': 'Digest realm="r", nonce="n"'}) is False

    def test_unsupported_scheme_returns_false(self):
        auth = _RtspAuth('u', 'p')
        assert auth.challenge({'www-authenticate': 'Bearer realm="r"'}) is False
        assert auth.negotiated is False

    def test_basic_negotiated(self):
        auth = _RtspAuth('u', 'p')
        assert auth.challenge({'www-authenticate': 'Basic realm="r"'}) is True
        assert auth.negotiated is True

    def test_digest_parses_all_params(self):
        auth = _RtspAuth('u', 'p')
        ok = auth.challenge({'www-authenticate':
            'Digest realm="R", nonce="N", opaque="O", algorithm=MD5, qop="auth"'})
        assert ok is True
        st = auth._state
        assert st['realm'] == 'R'
        assert st['nonce'] == 'N'
        assert st['opaque'] == 'O'
        assert st['algorithm'] == 'MD5'
        assert st['qop'] == 'auth'


class TestRtspAuthHeader:

    def test_basic_header_value(self):
        auth = _RtspAuth('user', 'pass')
        auth.challenge({'www-authenticate': 'Basic realm="r"'})
        # base64("user:pass")
        assert auth.header('DESCRIBE', 'rtsp://h/s') == 'Basic dXNlcjpwYXNz'

    def test_digest_no_qop_matches_rfc2069(self):
        auth = _RtspAuth('user', 'pass')
        auth.challenge({'www-authenticate':
            'Digest realm="LIVE555 Streaming Media", nonce="abc123"'})
        header = auth.header('DESCRIBE', 'rtsp://h/s')
        ha1 = _md5('user:LIVE555 Streaming Media:pass')
        ha2 = _md5('DESCRIBE:rtsp://h/s')
        expected = _md5('{}:{}:{}'.format(ha1, 'abc123', ha2))
        assert header.startswith('Digest ')
        assert 'response="{}"'.format(expected) in header
        assert 'username="user"' in header
        assert 'uri="rtsp://h/s"' in header
        # RFC 2069 style: no qop/nc/cnonce fields
        assert 'qop=' not in header
        assert 'cnonce=' not in header

    def test_digest_qop_auth_matches_rfc2617(self):
        auth = _RtspAuth('user', 'pass')
        auth.challenge({'www-authenticate':
            'Digest realm="r", nonce="n", qop="auth", opaque="op", algorithm=MD5'})
        header = auth.header('PLAY', 'rtsp://h/s')
        assert 'qop=auth' in header
        assert 'nc=00000001' in header
        assert 'cnonce=' in header
        assert 'opaque="op"' in header
        assert 'algorithm=MD5' in header

    def test_digest_qop_nc_increments_per_call(self):
        auth = _RtspAuth('user', 'pass')
        auth.challenge({'www-authenticate': 'Digest realm="r", nonce="n", qop="auth"'})
        h1 = auth.header('DESCRIBE', 'rtsp://h/s')
        h2 = auth.header('SETUP', 'rtsp://h/s/trackID=0')
        assert 'nc=00000001' in h1
        assert 'nc=00000002' in h2

    def test_digest_qop_list_prefers_first(self):
        auth = _RtspAuth('user', 'pass')
        auth.challenge({'www-authenticate':
            'Digest realm="r", nonce="n", qop="auth,auth-int"'})
        header = auth.header('PLAY', 'rtsp://h/s')
        assert 'qop=auth' in header
        assert 'auth-int' not in header

    def test_digest_response_depends_on_method_and_uri(self):
        auth = _RtspAuth('user', 'pass')
        auth.challenge({'www-authenticate': 'Digest realm="r", nonce="n"'})
        h_describe = auth.header('DESCRIBE', 'rtsp://h/s')
        h_setup = auth.header('SETUP', 'rtsp://h/s/trackID=0')
        assert h_describe != h_setup


# ---------------------------------------------------------------------------
# Regression: mock RTSP server that requires authentication
# ---------------------------------------------------------------------------

_SDP_TEMPLATE = (
    'v=0\r\n'
    'o=- 0 0 IN IP4 127.0.0.1\r\n'
    's=test\r\n'
    'c=IN IP4 0.0.0.0\r\n'
    't=0 0\r\n'
    'm=video 0 RTP/AVP 96\r\n'
    'a=rtpmap:96 H264/90000\r\n'
    'a=control:trackID=0\r\n'
)


class _MockAuthRTSPServer:
    """Minimal RTSP server that challenges with 401 and validates credentials.

    OPTIONS is answered without auth (as LIVE555 does); DESCRIBE/SETUP/PLAY
    require a valid ``Authorization`` header or receive a 401 challenge.
    Records every ``Authorization`` header seen and whether authentication
    ultimately succeeded, so tests can assert on the negotiated exchange.
    """

    def __init__(self, scheme='digest', user='218', password='420', qop=None,
                 inject_interleaved_noise=False):
        self.scheme = scheme
        self.user = user
        self.password = password
        self.qop = qop
        self.realm = 'Test Realm'
        self.nonce = '7293b54b2e4be4bbaf7ca446d1bb7f8d'
        self.received_auth = []
        self.request_lines = []
        self.authorized = False
        # Reproduces the buggy-camera behaviour observed live: stray
        # interleaved RTP frames arriving concatenated with a text response
        # in a single TCP segment, ahead of any SETUP/PLAY.
        self.inject_interleaved_noise = inject_interleaved_noise

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def uri(self):
        creds = ''
        if self.user is not None:
            creds = '{}:{}@'.format(self.user, self.password)
        return 'rtsp://{}127.0.0.1:{}/720p'.format(creds, self.port)

    def _challenge_value(self):
        if self.scheme == 'basic':
            return 'Basic realm="{}"'.format(self.realm)
        extra = ', qop="{}"'.format(self.qop) if self.qop else ''
        return 'Digest realm="{}", nonce="{}"{}'.format(self.realm, self.nonce, extra)

    def _valid_auth(self, method, uri, authhdr):
        if not authhdr:
            return False
        if self.scheme == 'basic':
            import base64
            token = base64.b64encode(
                '{}:{}'.format(self.user, self.password).encode()).decode()
            return authhdr == 'Basic ' + token
        # digest: recompute the expected response from the request's method+uri
        params = {}
        import re
        for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', authhdr):
            params[m.group(1).lower()] = (
                m.group(2) if m.group(2) is not None else m.group(3))
        ha1 = _md5('{}:{}:{}'.format(self.user, self.realm, self.password))
        ha2 = _md5('{}:{}'.format(method, uri))
        if self.qop:
            need = '{}:{}:{}:{}:{}:{}'.format(
                ha1, self.nonce, params.get('nc', ''),
                params.get('cnonce', ''), self.qop, ha2)
        else:
            need = '{}:{}:{}'.format(ha1, self.nonce, ha2)
        return params.get('response') == _md5(need)

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        conn.settimeout(10)
        buf = b''
        try:
            while True:
                while b'\r\n\r\n' not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                idx = buf.index(b'\r\n\r\n') + 4
                head, buf = buf[:idx], buf[idx:]
                lines = head.decode('utf-8', 'ignore').split('\r\n')
                request_line = lines[0]
                # Ignore interleaved RTP frames ($) a publisher may send.
                if not request_line or request_line[0] == '$':
                    buf = b''
                    continue
                self.request_lines.append(request_line)
                parts = request_line.split(' ')
                method, req_uri = parts[0], parts[1]
                headers = {}
                for line in lines[1:]:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        headers[k.strip().lower()] = v.strip()
                # Consume and discard any request body (e.g. ANNOUNCE's SDP).
                clen = int(headers.get('content-length', 0) or 0)
                while len(buf) < clen:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                buf = buf[clen:]
                cseq = headers.get('cseq', '0')
                authhdr = headers.get('authorization')
                if authhdr:
                    self.received_auth.append(authhdr)
                self._respond(conn, method, req_uri, cseq, authhdr)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _make_interleaved_noise(self):
        """Fabricate stray ``$``-framed binary records, as buggy cameras do.

        The first frame's payload contains a literal ``\\r\\n\\r\\n``, so a
        reader that ignores frame boundaries and just scans for the text
        delimiter would find this false-positive and return corrupted data.
        """
        payload1 = b'\x00\x01\r\n\r\n\x02\x03'
        frame1 = b'$' + bytes([0]) + struct.pack('!H', len(payload1)) + payload1
        payload2 = bytes(range(20))
        frame2 = b'$' + bytes([0]) + struct.pack('!H', len(payload2)) + payload2
        return frame1 + frame2

    def _respond(self, conn, method, req_uri, cseq, authhdr):
        def send(status, extra=None, body=b''):
            out = ['RTSP/1.0 {}'.format(status), 'CSeq: {}'.format(cseq)]
            if extra:
                out += ['{}: {}'.format(k, v) for k, v in extra.items()]
            if body:
                out.append('Content-Length: {}'.format(len(body)))
            msg = ('\r\n'.join(out) + '\r\n\r\n').encode() + body
            if self.inject_interleaved_noise and status.startswith('200'):
                # Concatenated into the same write so the client's next
                # recv() sees noise and response merged in one chunk, as
                # happened against the real camera.
                msg = self._make_interleaved_noise() + msg
            conn.sendall(msg)

        if method == 'OPTIONS':
            send('200 OK', {'Public': 'OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN'})
            return

        # All other methods require valid authentication.
        if not self._valid_auth(method, req_uri, authhdr):
            send('401 Unauthorized', {'WWW-Authenticate': self._challenge_value()})
            return

        self.authorized = True
        if method == 'DESCRIBE':
            body = _SDP_TEMPLATE.encode()
            send('200 OK', {
                'Content-Type': 'application/sdp',
                'Content-Base': 'rtsp://127.0.0.1:{}/720p/'.format(self.port),
            }, body)
        elif method == 'SETUP':
            send('200 OK', {
                'Session': '12345678;timeout=60',
                'Transport': 'RTP/AVP/TCP;unicast;interleaved=0-1',
            })
        elif method == 'PLAY':
            send('200 OK', {'Session': '12345678'})
        elif method == 'ANNOUNCE':
            send('200 OK')
        elif method == 'RECORD':
            send('200 OK', {'Session': '12345678'})
        else:
            send('200 OK')

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def mock_auth_server():
    servers = []

    def _make(**kwargs):
        s = _MockAuthRTSPServer(**kwargs)
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.close()


class TestClientAuthHandshake:

    def test_digest_no_qop_handshake(self, mock_auth_server):
        server = mock_auth_server(scheme='digest')
        client = rtsp.Client(server.uri)
        try:
            assert client.isOpened() is True
            assert server.authorized is True
            assert any(a.startswith('Digest ') for a in server.received_auth)
        finally:
            client.close()

    def test_digest_qop_auth_handshake(self, mock_auth_server):
        server = mock_auth_server(scheme='digest', qop='auth')
        client = rtsp.Client(server.uri)
        try:
            assert client.isOpened() is True
            assert server.authorized is True
            assert any('nc=' in a and 'cnonce=' in a for a in server.received_auth)
        finally:
            client.close()

    def test_basic_handshake(self, mock_auth_server):
        server = mock_auth_server(scheme='basic')
        client = rtsp.Client(server.uri)
        try:
            assert client.isOpened() is True
            assert server.authorized is True
            assert any(a.startswith('Basic ') for a in server.received_auth)
        finally:
            client.close()

    def test_credentials_stripped_from_request_line(self, mock_auth_server):
        server = mock_auth_server(scheme='digest')
        client = rtsp.Client(server.uri)
        try:
            assert client.isOpened() is True
            # The request-line URI must never carry userinfo.
            assert all('@' not in line for line in server.request_lines)
        finally:
            client.close()

    def test_wrong_password_raises(self, mock_auth_server):
        server = mock_auth_server(scheme='digest', user='218', password='420')
        bad_uri = 'rtsp://218:wrong@127.0.0.1:{}/720p'.format(server.port)
        with pytest.raises(RuntimeError, match='authentication failed'):
            rtsp.Client(bad_uri)

    def test_no_credentials_on_challenge_raises(self, mock_auth_server):
        server = mock_auth_server(scheme='digest', user=None, password=None)
        # URI has no userinfo, so the client cannot answer the 401 challenge.
        with pytest.raises(RuntimeError, match='authentication failed'):
            rtsp.Client('rtsp://127.0.0.1:{}/720p'.format(server.port))


class TestPublisherAuthHandshake:
    """The Publisher push handshake (ANNOUNCE/SETUP/RECORD) must authenticate."""

    def test_digest_announce_handshake(self, mock_auth_server):
        server = mock_auth_server(scheme='digest')
        pub = rtsp.Source(server.uri, serve=False, size=(160, 120))
        try:
            assert pub.isOpened() is True
            assert server.authorized is True
            assert 'ANNOUNCE' in server.request_lines[0]
            assert any(a.startswith('Digest ') for a in server.received_auth)
            # Credentials must never appear in the request-line URI.
            assert all('@' not in line for line in server.request_lines)
        finally:
            pub.close()

    def test_basic_announce_handshake(self, mock_auth_server):
        server = mock_auth_server(scheme='basic')
        pub = rtsp.Source(server.uri, serve=False, size=(160, 120))
        try:
            assert pub.isOpened() is True
            assert server.authorized is True
            assert any(a.startswith('Basic ') for a in server.received_auth)
        finally:
            pub.close()

    def test_wrong_password_raises(self, mock_auth_server):
        server = mock_auth_server(scheme='digest', user='218', password='420')
        bad_uri = 'rtsp://218:wrong@127.0.0.1:{}/live'.format(server.port)
        with pytest.raises(RuntimeError, match='authentication failed'):
            rtsp.Source(bad_uri, serve=False, size=(160, 120))

    def test_no_credentials_on_challenge_raises(self, mock_auth_server):
        server = mock_auth_server(scheme='digest', user=None, password=None)
        # No userinfo in the URI, so the publisher cannot answer the challenge.
        with pytest.raises(RuntimeError, match='authentication failed'):
            rtsp.Source('rtsp://127.0.0.1:{}/live'.format(server.port),
                        serve=False, size=(160, 120))


# ---------------------------------------------------------------------------
# Regression + unit: TEARDOWN on close()
#
# close() previously just dropped the TCP socket without a TEARDOWN, which
# leaves some servers (LIVE555-based cameras in particular) holding the
# session's stream source locked until the SETUP timeout expires, so a quick
# reconnect can fail even with fully correct credentials.
# ---------------------------------------------------------------------------

class TestClientTeardownRegression:

    def test_close_sends_teardown_with_negotiated_session(self, mock_auth_server):
        server = mock_auth_server(scheme='digest')
        client = rtsp.Client(server.uri)
        assert client.isOpened() is True
        client.close()
        # Give the server thread a moment to log the TEARDOWN request.
        for _ in range(20):
            if any(l.startswith('TEARDOWN') for l in server.request_lines):
                break
            time.sleep(0.05)
        teardown_lines = [l for l in server.request_lines if l.startswith('TEARDOWN')]
        assert teardown_lines, server.request_lines
        assert '@' not in teardown_lines[0]
        assert any(a.startswith('Digest ') for a in server.received_auth)


class TestClientTeardownUnit:

    def _bare(self):
        c = object.__new__(_client._RtspClient)
        c._uri = 'rtsp://h/s'
        c._cseq = 3
        c._auth = _RtspAuth(None, None)
        return c

    def test_noop_without_session(self):
        c = self._bare()
        c._session_id = None
        sock = MagicMock()
        c._teardown(sock)
        sock.sendall.assert_not_called()

    def test_sends_teardown_request_with_session_header(self):
        c = self._bare()
        c._session_id = '12345678'
        sock = MagicMock()
        c._teardown(sock)
        sent = sock.sendall.call_args[0][0].decode()
        assert sent.startswith('TEARDOWN rtsp://h/s RTSP/1.0')
        assert 'Session: 12345678' in sent
        assert 'CSeq: 4' in sent

    def test_includes_authorization_when_negotiated(self):
        c = self._bare()
        c._session_id = '1'
        c._auth = _RtspAuth('user', 'pass')
        c._auth.challenge({'www-authenticate': 'Digest realm="r", nonce="n"'})
        sock = MagicMock()
        c._teardown(sock)
        sent = sock.sendall.call_args[0][0].decode()
        assert 'Authorization: Digest ' in sent

    def test_oserror_is_swallowed(self):
        c = self._bare()
        c._session_id = '1'
        sock = MagicMock()
        sock.sendall.side_effect = OSError('gone')
        c._teardown(sock)  # must not raise


class TestPublisherTeardownRegression:

    def test_close_sends_teardown_with_negotiated_session(self, mock_auth_server):
        server = mock_auth_server(scheme='digest')
        pub = rtsp.Source(server.uri, serve=False, size=(160, 120))
        assert pub.isOpened() is True
        pub.close()
        for _ in range(20):
            if any(l.startswith('TEARDOWN') for l in server.request_lines):
                break
            time.sleep(0.05)
        teardown_lines = [l for l in server.request_lines if l.startswith('TEARDOWN')]
        assert teardown_lines, server.request_lines
        assert '@' not in teardown_lines[0]


class TestPublisherTeardownUnit:

    def _bare(self):
        p = object.__new__(_source.Publisher)
        p._uri = 'rtsp://h/s'
        p._cseq = 3
        p._auth = _RtspAuth(None, None)
        return p

    def test_noop_without_session(self):
        p = self._bare()
        p._session_id = None
        sock = MagicMock()
        p._teardown(sock)
        sock.sendall.assert_not_called()

    def test_sends_teardown_request_with_session_header(self):
        p = self._bare()
        p._session_id = '87654321'
        sock = MagicMock()
        p._teardown(sock)
        sent = sock.sendall.call_args[0][0].decode()
        assert sent.startswith('TEARDOWN rtsp://h/s RTSP/1.0')
        assert 'Session: 87654321' in sent

    def test_includes_authorization_when_negotiated(self):
        p = self._bare()
        p._session_id = '1'
        p._auth = _RtspAuth('user', 'pass')
        p._auth.challenge({'www-authenticate': 'Basic realm="r"'})
        sock = MagicMock()
        p._teardown(sock)
        sent = sock.sendall.call_args[0][0].decode()
        assert 'Authorization: Basic ' in sent

    def test_oserror_is_swallowed(self):
        p = self._bare()
        p._session_id = '1'
        sock = MagicMock()
        sock.sendall.side_effect = OSError('gone')
        p._teardown(sock)  # must not raise


# ---------------------------------------------------------------------------
# Unit + regression: _Reader tolerates interleaved ($) frames
#
# Reproduces a real IP camera's behaviour: stray interleaved RTP frames
# (left over from a prior session, or emitted early) arrive concatenated
# with a text response in a single TCP chunk, ahead of any SETUP/PLAY.
# Before the fix this corrupted read_until()'s search for the '\r\n\r\n'
# delimiter, raising a garbled "RTSP error" instead of parsing the response.
# ---------------------------------------------------------------------------

def _interleaved_frame(payload, channel=0):
    return b'$' + bytes([channel]) + struct.pack('!H', len(payload)) + payload


class _FakeSocket:
    """A socket whose recv() replays fixed chunks, then signals EOF."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        return b''


class TestReaderInterleavedFrames:

    def test_single_frame_skipped_before_response(self):
        noise = _interleaved_frame(b'junk12345')
        response = b'RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n'
        reader = _Reader(_FakeSocket([noise + response]))
        assert reader.read_until(b'\r\n\r\n') == response

    def test_multiple_frames_skipped(self):
        noise = _interleaved_frame(b'a') + _interleaved_frame(b'bb') + _interleaved_frame(b'')
        response = b'RTSP/1.0 200 OK\r\nCSeq: 2\r\n\r\n'
        reader = _Reader(_FakeSocket([noise + response]))
        assert reader.read_until(b'\r\n\r\n') == response

    def test_decoy_delimiter_inside_frame_payload_is_not_matched(self):
        # The frame payload itself contains a literal '\r\n\r\n'; a naive
        # byte-scan for the delimiter would stop here and return garbage.
        noise = _interleaved_frame(b'\x00\x01\r\n\r\n\x02\x03')
        response = b'RTSP/1.0 200 OK\r\nCSeq: 3\r\n\r\n'
        reader = _Reader(_FakeSocket([noise + response]))
        assert reader.read_until(b'\r\n\r\n') == response

    def test_frame_split_across_multiple_recv_calls(self):
        noise = _interleaved_frame(b'0123456789')
        response = b'RTSP/1.0 200 OK\r\nCSeq: 4\r\n\r\n'
        combined = noise + response
        # Force recv() to hand back the data a few bytes at a time.
        chunks = [combined[i:i + 3] for i in range(0, len(combined), 3)]
        reader = _Reader(_FakeSocket(chunks))
        assert reader.read_until(b'\r\n\r\n') == response

    def test_data_after_response_preserved_for_take_remainder(self):
        noise = _interleaved_frame(b'pre')
        response = b'RTSP/1.0 200 OK\r\nCSeq: 5\r\n\r\n'
        trailing_rtp = _interleaved_frame(b'real-rtp-payload')
        reader = _Reader(_FakeSocket([noise + response + trailing_rtp]))
        assert reader.read_until(b'\r\n\r\n') == response
        assert reader.take_remainder() == trailing_rtp

    def test_read_exact_unaffected_by_interleave_skip(self):
        body = b'v=0\r\no=- 0 0 IN IP4 0\r\n'
        reader = _Reader(_FakeSocket([body]))
        assert reader.read_exact(len(body)) == body

    def test_closed_connection_during_skip_raises(self):
        noise = _interleaved_frame(b'x')[:-1]  # truncated frame, then EOF
        reader = _Reader(_FakeSocket([noise]))
        with pytest.raises(ConnectionError):
            reader.read_until(b'\r\n\r\n')

    def test_closed_connection_while_awaiting_delimiter_raises(self):
        # No interleaved noise; delimiter just never arrives before EOF.
        reader = _Reader(_FakeSocket([b'RTSP/1.0 200 OK\r\nCSeq: 1']))
        with pytest.raises(ConnectionError):
            reader.read_until(b'\r\n\r\n')


class TestClientInterleavedNoiseRegression:
    """End-to-end: Client survives a server that mixes stray RTP into the
    handshake, exactly reproducing the /ch3 camera failure."""

    def test_connects_despite_noise_before_describe_response(self, mock_auth_server):
        server = mock_auth_server(scheme='digest', inject_interleaved_noise=True)
        client = rtsp.Client(server.uri)
        try:
            assert client.isOpened() is True
            assert server.authorized is True
        finally:
            client.close()

    def test_publisher_connects_despite_noise(self, mock_auth_server):
        server = mock_auth_server(scheme='digest', inject_interleaved_noise=True)
        pub = rtsp.Source(server.uri, serve=False, size=(160, 120))
        try:
            assert pub.isOpened() is True
            assert server.authorized is True
        finally:
            pub.close()
