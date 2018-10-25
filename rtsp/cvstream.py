""" OpenCV Backend RTSP Client """

from threading import Thread
import cv2
import time
from io import BytesIO
from PIL import Image

class PicamVideoFeed:

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

class LocalVideoFeed:
    """ For Picamera and local USB-interface cameras. """
    def __init__(self, source_id, verbose = False):
        """ 
            source_id: the id of a camera interface. Should be an integer
            verbose: print log or not
        """
        self._cam_id = source_id
        self._verbose = verbose
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

        self._stream = cv2.VideoCapture(self._cam_id)

        if self._verbose:
            if self.isOpened():
                print("Connected to video source {}.".format(self._cam_id))
            else:
                print("Failed to connect to source {}.".format(self._cam_id))
                return

        time.sleep(.5)

    def close(self):
        if self.isOpened():
            self._stream.release()

    def isOpened(self):
        try:
            return self._stream is not None and self._stream.isOpened()
        except:
            return False

    def read(self):
        (grabbed, frame) = self._stream.read()
        return Image.fromarray(cv2.cvtColor(self._latest, cv2.COLOR_BGR2RGB))

    def preview(self):
        """ Blocking function. Opens OpenCV window to display stream. """
        win_name = 'Camera'
        cv2.namedWindow(win_name, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(win_name,20,20)
        self.open()
        while(self.isOpened()):
            cv2.imshow(win_name,self._stream.read()[1])
            if cv2.waitKey(25) & 0xFF == ord('q'):
                break
        cv2.waitKey()
        cv2.destroyAllWindows()
        cv2.waitKey()

class RTSPVideoFeed:
    """ Maintain live RTSP feed without buffering. """
    _stream = None
    _latest = None

    def __init__(self, rtsp_server_uri, verbose = False):
        """ 
            rtsp_server_uri: the path to an RTSP server. should start with "rtsp://"
            verbose: print log or not
        """
        self.rtsp_server_uri = rtsp_server_uri
        self._verbose = verbose

    def __enter__(self,*args,**kwargs):
        """ Returns the object which later will have __exit__ called.
            This relationship creates a context manager. """
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        """ Together with __enter__, allows support for `with-` clauses. """
        self.close()

    def open(self):
        self.close()
        self._stream = cv2.VideoCapture(self.rtsp_server_uri)

        if self._verbose:
            if self.isOpened():
                print("Connected to video source {}.".format(self.rtsp_server_uri))
            else:
                print("Failed to connect to source {}.".format(self.rtsp_server_uri))
                return

        time.sleep(.5)

    def close(self):
        if self.isOpened():
            self._stream.release()

    def isOpened(self):
        try:
            return self._stream is not None and self._stream.isOpened()
        except:
            return False

    def read(self):
        self.open()
        (grabbed, frame) = self._stream.read()
        self._latest = frame
        self._stream.release()
        return Image.fromarray(cv2.cvtColor(self._latest, cv2.COLOR_BGR2RGB))

    def preview(self):
        """ Blocking function. Opens OpenCV window to display stream. """
        win_name = 'RTSP'
        cv2.namedWindow(win_name, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(win_name,20,20)
        self.open()
        while(self.isOpened()):
            cv2.imshow(win_name,self._stream.read()[1])
            #if self._latest is not None:
            #    cv2.imshow(win_name,self._latest)
            if cv2.waitKey(25) & 0xFF == ord('q'):
                break
        cv2.waitKey()
        cv2.destroyAllWindows()
        cv2.waitKey()

#    def _cache_update(self):
#        # Worker for background thread. Quits on signal from `self.close()`.
#        while self.isOpened():
#            self._dropped = 0
#
#            while self._dropped < self._drop_frame_limit and self.isOpened():
#                (grabbed, frame) = self._stream.read()
#
#                if not grabbed:
#                    self._dropped += 1
#                else:
#                    self._dropped = 0
#                    self._latest = frame
#
#            self._stream.release()
#
#            if self._verbose:
#                announce = "Closed RTSP connection."
#                if self._dropped >= self._drop_frame_limit:
#                    print(announce + " - " + "Too many frames lost from RTSP connection.")
#                elif not self.isOpened():
#                    print(announce + " - " + "Received signal to stop.")
#
#            if self._retry_connection:
#                time.sleep(15)
#                self.open()
#            else:
#                self.close()
#                break

