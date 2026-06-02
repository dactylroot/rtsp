"""Shared utilities and Source factory for the rtsp package."""

import logging
from pathlib import Path as _Path
from urllib.parse import urlparse, urlunparse

import numpy as np
from PIL import Image

_NETWORK_SCHEMES = {'rtsp', 'rtsps', 'rtmp', 'rtmps', 'http', 'https', 'tcp'}

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
        from .nativertmp import RTMPPublisher
        return RTMPPublisher(rtsp_server_uri, fps=fps, verbose=verbose,
                             size=size, frame_buffer=frame_buffer)
    from .source import Publisher
    return Publisher(rtsp_server_uri, fps=fps, verbose=verbose,
                     size=size, frame_buffer=frame_buffer)
