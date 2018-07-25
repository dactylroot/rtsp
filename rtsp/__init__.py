""" RTSP Client
    Wrapper around OpenCV-Python. """

import io as _io
import os as _os

import cv2 as _cv2
from PIL import Image as _Image
from .cvstream import VideoSkip as _Stream

del(cvstream)

with open(_os.path.abspath(_os.path.dirname(__file__))+'/__doc__','r') as _f:
    __doc__ = _f.read()

_source  = "rtsp://192.168.1.3/ufirststream/track1"
_source2 = "rtsp://10.38.5.145/ufirststream"

class Client:

    def __init__(self, rtsp_server_uri = _source, verbose = True):
        self.capture = _Stream(rtsp_server_uri)
        if self.capture.isOpened():
            print("Connected to video source "+rtsp_server_uri)
        else:
            print("Couldn't connect to "+rtsp_server_uri)

    def __enter__(self,*args,**kwargs):
        """ Returns the object which later will have __exit__ called.
            This relationship creates a context manager. """
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        """ Together with __enter__, allows support for `with-` clauses. """
        self.close()

    def read(self):
        """ Read single frame """
        frame = self.capture.read()
        return _Image.fromarray(_cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB))

    def preview(self):
        self.capture.preview()

    def close(self):
        self.capture.stop()

