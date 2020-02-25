""" RTSP Client
    Wrapper around OpenCV-Python. """

import os as _os

from . import ffmpegstream
from .ffmpegstream import Client, PicamVideoFeed, WebcamVideoFeed
del(ffmpegstream)

with open(_os.path.abspath(_os.path.dirname(__file__))+'/__doc__','r') as _f:
    __doc__ = _f.read()
