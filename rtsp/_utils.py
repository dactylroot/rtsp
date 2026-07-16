"""Shared utilities and Source factory for the rtsp package."""

import base64
import hashlib
import logging
import os
import re
import struct
from pathlib import Path as _Path
from urllib.parse import unquote, urlparse, urlunparse

import numpy as np
from PIL import Image

_NETWORK_SCHEMES = {'rtsp', 'rtsps', 'rtmp', 'rtmps', 'http', 'https', 'tcp'}
_READER_RECV_SIZE = 65536


class _Reader:
    """Byte-accurate buffered reader over a blocking TCP socket.

    Used during the RTSP handshake phase only (OPTIONS/DESCRIBE/SETUP/PLAY or
    ANNOUNCE/SETUP/RECORD).  Tolerates interleaved ``$``-framed binary data
    arriving ahead of the text response currently being waited for -- some
    servers begin streaming RTP (or replay frames left over from a prior
    session) before the handshake completes.  Such frames are discarded;
    genuine data queued *after* a response is left in the buffer for
    ``take_remainder()`` to hand to the RTP receive loop.
    """

    __slots__ = ('_sock', '_buf')

    def __init__(self, sock):
        self._sock = sock
        self._buf = b''

    def _fill(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(_READER_RECV_SIZE)
            if not chunk:
                raise ConnectionError('RTSP connection closed during handshake')
            self._buf += chunk

    def _skip_interleaved_frames(self):
        """Discard complete ``$``-framed binary records queued ahead of text.

        Per RFC 2326 Sec 10.12, an interleaved frame and an RTSP text message
        are never spliced together -- each is written as a complete, atomic
        unit -- so it is always safe to classify by the leading byte and
        fully consume one frame before re-checking.
        """
        while True:
            self._fill(1)
            if self._buf[0:1] != b'$':
                return
            self._fill(4)
            length = struct.unpack('!H', self._buf[2:4])[0]
            self._fill(4 + length)
            self._buf = self._buf[4 + length:]

    def read_until(self, delim):
        self._skip_interleaved_frames()
        while delim not in self._buf:
            chunk = self._sock.recv(_READER_RECV_SIZE)
            if not chunk:
                raise ConnectionError('RTSP connection closed during handshake')
            self._buf += chunk
        idx = self._buf.index(delim) + len(delim)
        result, self._buf = self._buf[:idx], self._buf[idx:]
        return result

    def read_exact(self, n):
        self._fill(n)
        result, self._buf = self._buf[:n], self._buf[n:]
        return result

    def take_remainder(self):
        out, self._buf = self._buf, b''
        return out

logging.getLogger('rtsp').addHandler(logging.NullHandler())


def _enable_verbose():
    """Attach a StreamHandler to the 'rtsp' logger if none is already configured.

    Called when verbose=True so output appears without requiring application-level
    logging configuration.  Idempotent - safe to call multiple times.
    """
    logger = logging.getLogger('rtsp')
    if not any(not isinstance(h, logging.NullHandler) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(name)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


def _parse_uri(uri):
    """Normalize *uri* and return ``(kind, normalized)``.

    kind is one of ``'device'`` or ``'network'``.
    normalized is an ``int`` for devices, otherwise a string.
    """
    if isinstance(uri, int):
        return 'device', uri
    if not isinstance(uri, str):
        raise TypeError("URI must be a str or int, got {!r}".format(type(uri).__name__))

    s = uri.strip()

    if s.isdigit():
        return 'device', int(s)

    if '//' not in s:
        # bare host e.g. '192.168.1.1/stream' or 'localhost:8554/live'
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


def _split_credentials(uri):
    """Split userinfo from a network URI.

    Return ``(user, password, clean_uri)`` where *clean_uri* has any
    ``user:pass@`` removed.  Credentials are percent-decoded.  A URI without
    userinfo returns ``(None, None, uri)`` unchanged.  Credentials belong in
    the ``Authorization`` header, never in the RTSP request-line URI.
    """
    parsed = urlparse(uri)
    if not (parsed.username or parsed.password):
        return None, None, uri
    user = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    netloc = parsed.hostname or ''
    if parsed.port:
        netloc += ':{}'.format(parsed.port)
    return user, password, urlunparse(parsed._replace(netloc=netloc))


class _RtspAuth:
    """RTSP authentication helper for Basic and Digest schemes.

    Holds credentials and, once a ``WWW-Authenticate`` challenge has been
    parsed via :meth:`challenge`, produces per-request ``Authorization`` header
    values via :meth:`header`.  Shared by the native Client and Publisher
    handshakes.

    Digest supports both RFC 2069 (no ``qop``, used by LIVE555 and many IP
    cameras) and RFC 2617 ``qop=auth`` with client nonce counting.
    """

    __slots__ = ('user', 'password', '_state')

    def __init__(self, user, password):
        self.user = user
        self.password = password
        self._state = None  # negotiated scheme + params, or None until challenged

    @property
    def negotiated(self):
        """True once a supported challenge has been accepted."""
        return self._state is not None

    @property
    def has_credentials(self):
        """True if a username or password was supplied."""
        return self.user is not None or self.password is not None

    def challenge(self, headers):
        """Parse a ``WWW-Authenticate`` header from a response headers dict.

        Returns True if a supported scheme (Basic or Digest) was negotiated and
        credentials are available, otherwise False.
        """
        challenge = headers.get('www-authenticate')
        if not challenge or not self.has_credentials:
            return False

        scheme, _, rest = challenge.partition(' ')
        scheme = scheme.lower()

        if scheme == 'basic':
            self._state = {'scheme': 'basic'}
            return True

        if scheme == 'digest':
            params = {}
            for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', rest):
                params[m.group(1).lower()] = (
                    m.group(2) if m.group(2) is not None else m.group(3))
            self._state = {
                'scheme': 'digest',
                'realm': params.get('realm', ''),
                'nonce': params.get('nonce', ''),
                'opaque': params.get('opaque'),
                'algorithm': params.get('algorithm'),
                'qop': params.get('qop'),
                'nc': 0,
            }
            return True

        return False

    def header(self, method, uri):
        """Return the ``Authorization`` header value for *method*/*uri*, or None.

        Recomputes the digest response for each call so every request is signed
        with its own method and URI.
        """
        state = self._state
        if not state:
            return None

        user = self.user or ''
        password = self.password or ''

        if state['scheme'] == 'basic':
            token = base64.b64encode('{}:{}'.format(user, password).encode()).decode()
            return 'Basic ' + token

        def _md5(s):
            return hashlib.md5(s.encode()).hexdigest()

        realm = state['realm']
        nonce = state['nonce']
        ha1 = _md5('{}:{}:{}'.format(user, realm, password))
        ha2 = _md5('{}:{}'.format(method, uri))

        qop = state.get('qop')
        if qop:
            qop = qop.split(',')[0].strip()  # prefer the first offered ('auth')

        parts = [
            'username="{}"'.format(user),
            'realm="{}"'.format(realm),
            'nonce="{}"'.format(nonce),
            'uri="{}"'.format(uri),
        ]
        if qop == 'auth':
            state['nc'] += 1
            nc = '{:08x}'.format(state['nc'])
            cnonce = os.urandom(8).hex()
            response = _md5('{}:{}:{}:{}:{}:{}'.format(
                ha1, nonce, nc, cnonce, qop, ha2))
            parts += [
                'response="{}"'.format(response),
                'qop={}'.format(qop),
                'nc={}'.format(nc),
                'cnonce="{}"'.format(cnonce),
            ]
        else:
            # RFC 2069-style Digest (no qop) - used by LIVE555 and many cameras.
            response = _md5('{}:{}:{}'.format(ha1, nonce, ha2))
            parts.append('response="{}"'.format(response))

        if state.get('opaque'):
            parts.append('opaque="{}"'.format(state['opaque']))
        if state.get('algorithm'):
            parts.append('algorithm={}'.format(state['algorithm']))

        return 'Digest ' + ', '.join(parts)


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


def _source_factory(rtsp_server_uri, fps=25, serve=True, verbose=False,
                    size=None, frame_buffer=None):
    """Return a Source, Publisher, or RTMPPublisher for *rtsp_server_uri*.

    ``serve=True`` (the default) returns a built-in Python RTSP server with no
    external relay that supports multiple simultaneous clients.  ``serve=False``
    returns a Publisher (RTSP relay push) or RTMPPublisher (RTMP relay push).

    Usage (built-in server, no relay)::

        with rtsp.Source('rtsp://0.0.0.0:8554/live', fps=25) as src:
            for path in image_files:
                src.put(path)
            src.serve_forever()

    Usage (push to mediamtx for multiple clients)::

        with rtsp.Source('rtsp://localhost:8554/live', fps=25, serve=False) as src:
            for path in image_files:
                src.put(path)
    """
    if serve:
        from .source import Source
        return Source(rtsp_server_uri, fps=fps, verbose=verbose,
                      size=size, frame_buffer=frame_buffer)
    kind, uri = _parse_uri(rtsp_server_uri)
    if kind == 'network' and urlparse(uri).scheme in ('rtmp', 'rtmps'):
        from .rtmp import RTMPPublisher
        return RTMPPublisher(rtsp_server_uri, fps=fps, verbose=verbose,
                             size=size, frame_buffer=frame_buffer)
    from .source import Publisher
    return Publisher(rtsp_server_uri, fps=fps, verbose=verbose,
                     size=size, frame_buffer=frame_buffer)
