""" RTSP Client
    Wrapper around OpenCV-Python. """

import os as _os

import cv2 as _cv2
from PIL import Image as _Image
from .cvstream import RTSPVideoFeed  as _Netstream
from .cvstream import PicamVideoFeed as _Picam
from .cvstream import LocalVideoFeed as _Webcam
del(cvstream)

with open(_os.path.abspath(_os.path.dirname(__file__))+'/__doc__','r') as _f:
    __doc__ = _f.read()

_default_source  = "rtsp://192.168.1.168/ufirststream/track1"
_default_verbose = False        

class Client:
    def __init__(self, rtsp_server_uri = _default_source, verbose = _default_verbose):
        """ 
            rtsp_server_uri: the path to an RTSP server. should start with "rtsp://"
            verbose: print log or not
            drop_frame_limit: how many dropped frames to endure before dropping a connection
            retry_connection: whether to retry opening the RTSP connection (after a fixed delay of 15s)
        """
        self.verbose = verbose
        self.rtsp_server_uri = rtsp_server_uri
        self.open(rtsp_server_uri)

    def __enter__(self,*args,**kwargs):
        """ Returns the object which later will have __exit__ called.
            This relationship creates a context manager. """
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        """ Together with __enter__, allows support for `with-` clauses. """
        self.close()

    def open(self,rtsp_server_uri=None):
        if rtsp_server_uri:
            self.rtsp_server_uri = rtsp_server_uri
        else:
            rtsp_server_uri = self.rtsp_server_uri

        if isinstance(rtsp_server_uri, int):
            self._capture = _Webcam(rtsp_server_uri,self.verbose)
        elif rtsp_server_uri.lower() in 'picamera':
            self._capture = _Picam()
        else:
            self._capture = _Netstream(rtsp_server_uri,self.verbose)

    def isOpened(self):
        return self._capture.isOpened()

    def read(self):
        """ Return most recent frame as Pillow image. Returns None if none have been retrieved. """
        return self._capture.read()

    def preview(self):
        self._capture.preview()

    def close(self):
        self._capture.close()

