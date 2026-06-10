from . import _utils
from . import client as _client

Client = _client.Client
Source = _utils._source_factory
list_devices = _client.list_devices

__all__ = ['Client', 'Source', 'list_devices']

try:
    from importlib.metadata import metadata as _metadata
    __doc__ = _metadata('rtsp')['Description']
except Exception:
    pass
