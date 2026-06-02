""" RTSP Client """

import os as _os

from . import _utils
from . import client as _client
del client

Client = _client.Client
Source = _utils._source_factory
list_devices = _client.list_devices

__all__ = ['Client', 'Source', 'list_devices']

from pathlib import Path as _Path

with open(_Path(_os.path.abspath(_os.path.dirname(__file__))) / '__doc__','r') as _f:
    __doc__ = _f.read()
