""" RTSP Client
    Wrapper around OpenCV-Python. """

import io as _io
import os as _os

import cv2 as _cv2
from PIL import Image as _Image
from .cvstream import LiveVideoFeed as _Stream

del(cvstream)

with open(_os.path.abspath(_os.path.dirname(__file__))+'/__doc__','r') as _f:
    __doc__ = _f.read()

_default_source  = "rtsp://192.168.1.3/ufirststream/track1"
_default_verbose = False
_default_drop_frame_limit = 5
_default_retry_connection = True

class Client:
    def __init__(self, rtsp_server_uri = _default_source, drop_frame_limit = _default_drop_frame_limit, retry_connection=_default_retry_connection, verbose = _default_verbose):
        """ 
            rtsp_server_uri: the path to an RTSP server. should start with "rtsp://"
            verbose: print log or not
            drop_frame_limit: how many dropped frames to endure before dropping a connection
            retry_connection: whether to retry opening the RTSP connection (after a fixed delay of 15s)
        """
        self.open(rtsp_server_uri,drop_frame_limit,retry_connection,verbose)

    def __enter__(self,*args,**kwargs):
        """ Returns the object which later will have __exit__ called.
            This relationship creates a context manager. """
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        """ Together with __enter__, allows support for `with-` clauses. """
        self.close()

    def open(self, rtsp_server_uri = _default_source, drop_frame_limit = _default_drop_frame_limit, retry_connection=_default_retry_connection,verbose = _default_verbose):
        self._capture = _Stream(rtsp_server_uri,drop_frame_limit,retry_connection,verbose)

    def isOpened(self):
        return self._capture.isOpened()

    def read(self):
        """ Read single frame """
        frame = self._capture.read()
        return _Image.fromarray(_cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB))

    def preview(self):
        self._capture.preview()

    def close(self):
        self._capture.stop()

