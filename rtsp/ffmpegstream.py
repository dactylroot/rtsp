""" OpenCV Backend RTSP Client """

import cv2
from io import BytesIO
from PIL import Image

from threading import Thread

class Client:
    """ Maintain live RTSP feed without buffering. """
    _stream = None

    def __init__(self, rtsp_server_uri, verbose = False):
        """
            rtsp_server_uri: the path to an RTSP server. should start with "rtsp://"
            verbose: print log or not
        """
        self.rtsp_server_uri = rtsp_server_uri
        self._verbose = verbose

        if isinstance(rtsp_server_uri,str) and 'picam' in rtsp_server_uri:
            self.__class__ = PicamVideoFeed
            _pc = PicamVideoFeed()
            self.__dict__.update(_pc.__dict__)

        self._bg_run = False
        self.open()

    def __enter__(self,*args,**kwargs):
        """ Returns the object which later will have __exit__ called.
            This relationship creates a context manager. """
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        """ Together with __enter__, allows support for `with-` clauses. """
        self.close()

    def open(self):
        if self.isOpened():
            return
        self._stream = cv2.VideoCapture(self.rtsp_server_uri)
        if self._verbose:
            print("Connected to video source {}.".format(self.rtsp_server_uri))
        self._bg_run = True
        t = Thread(target=self._update, args=())
        t.daemon = True
        t.start()
        self._bgt = t
        return self

    def close(self):
        """ signal background thread to stop. release CV stream """
        self._bg_run = False
        self._bgt.join()
        if self._verbose:
            print("Disconnected from {}".format(self.rtsp_server_uri))

    def isOpened(self):
        """ return true if stream is opened and being read, else ensure closed """
        try:
            return (self._stream is not None) and self._stream.isOpened() and self._bg_run
        except:
            self.close()
            return False

    def _update(self):
        while self.isOpened():
            try:
                self._stream.grab()
            except:
                self._bg = False

    def read(self,raw=False):
        """ Retrieve most recent frame and convert to PIL. Return unconverted with raw=True. """
        try:
            (grabbed, frame) = self._stream.retrieve()
            if raw:
                return frame
            else:
                return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        except:
            return None

    def preview(self):
        """ Blocking function. Opens OpenCV window to display stream. """
        win_name = 'RTSP'
        cv2.namedWindow(win_name, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(win_name,20,20)
        while(self.isOpened()):
            cv2.imshow(win_name,self.read(raw=True))
            if cv2.waitKey(30) == ord('q'): # wait 30 ms for 'q' input
                break
        cv2.waitKey(1)
        cv2.destroyAllWindows()
        cv2.waitKey(1)

class PicamVideoFeed(Client):

    def __init__(self):
        import picamera
        self.cam = picamera.PiCamera()

    def preview(self,*args,**kwargs):
        """ Blocking function. Opens OpenCV window to display stream. """
        self.cam.start_preview(*args,**kwargs)

    def open(self):
        pass

    def isOpened(self):
        return True

    def read(self):
        """https://picamera.readthedocs.io/en/release-1.13/recipes1.html#capturing-to-a-pil-image"""
        stream = BytesIO()
        self.cam.capture(stream, format='png')
        # "Rewind" the stream to the beginning so we can read its content
        stream.seek(0)
        return Image.open(stream)

    def close(self):
        pass

    def stop(self):
        pass
