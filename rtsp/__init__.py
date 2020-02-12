""" RTSP Client
    Wrapper around OpenCV-Python. """

import os as _os

from . import cvstream
from .cvstream import Client, PicamVideoFeed, WebcamVideoFeed
del(cvstream)

with open(_os.path.abspath(_os.path.dirname(__file__))+'/__doc__','r') as _f:
    __doc__ = _f.read()
