### https://github.com/jrosebr1/imutils/blob/master/imutils/video/filevideostream.py

# import the necessary packages
from threading import Thread
import cv2
import time

class LiveVideoFeed:
    """ Maintain live RTSP feed without buffering. """
    def __init__(self, rtsp_server_uri, drop_frame_limit = 5, retry_connection = True, verbose = False):
        """ 
            rtsp_server_uri: the path to an RTSP server. should start with "rtsp://"
            verbose: print log or not
            drop_frame_limit: how many dropped frames to endure before dropping a connection
            retry_connection: whether to retry opening the RTSP connection (after a fixed delay of 15s)
        """
        self._rtsp_server_uri = rtsp_server_uri
        self._drop_frame_limit = drop_frame_limit
        self._retry_connection = retry_connection
        self._verbose = verbose
        self._latest = None
        self.start()

    def __enter__(self,*args,**kwargs):
        """ Returns the object which later will have __exit__ called.
            This relationship creates a context manager. """
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        """ Together with __enter__, allows support for `with-` clauses. """
        self.stop()

    def start(self):
        """Open new connection and continually read and cache latest frame"""
        t = Thread(target=self._cache_update, args=())
        t.daemon = True
        t.start()

    def isOpened(self):
        return self._caching

    def preview(self):
        """ Blocking function. Opens OpenCV window to display stream. """
        win_name = 'RTSP'
        cv2.namedWindow(win_name, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(win_name,20,20)
        while(self._caching):
            if self._latest is not None:
                cv2.imshow(win_name,self._latest)
            if cv2.waitKey(25) & 0xFF == ord('q'):
                break
        cv2.waitKey()
        cv2.destroyAllWindows()
        cv2.waitKey()

    def _cache_update(self):
        # Worker for background thread. Quits on signal from `self._caching`.
        while True:
            self._dropped = 0
            self._caching = True
            self._stream = cv2.VideoCapture(self._rtsp_server_uri)

            if self._verbose:
                if self.isOpened():
                    print("Connected to RTSP video source "+self._rtsp_server_uri+".")
                else:
                    print("Failed to connect to RTSP source "+self._rtsp_server_uri+".")
                    return

            while self._dropped < self._drop_frame_limit and self._caching:
                (grabbed, frame) = self._stream.read()

                if not grabbed:
                    self._dropped += 1
                else:
                    self._dropped = 0
                    self._latest = frame

            self._stream.release()

            if self._verbose:
                print("Dropped RTSP connection.")
                if self._dropped >= self._drop_frame_limit:
                    print("Too many frames lost from RTSP connection.")
                elif not self._caching:
                    print("Received signal to stop.")

            if not self._retry_connection:
                break
            else:
                time.sleep(15)

        self._caching = False

    def read(self):
        return self._latest

    def stop(self):
        self._caching = False