### https://github.com/jrosebr1/imutils/blob/master/imutils/video/filevideostream.py

# import the necessary packages
from threading import Thread
import cv2
import time

class LiveVideoFeed:
    """ Maintain live RTSP feed without buffering. """
    _stream = None
    _latest = None

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
        self.open()
        t = Thread(target=self._cache_update, args=())
        t.daemon = True
        t.start()

    def open(self,rtsp_server_uri = None):
        if not rtsp_server_uri:
            rtsp_server_uri = self._rtsp_server_uri

        self.close()
        self._stream = cv2.VideoCapture(rtsp_server_uri)

        if self._verbose:
            if self.isOpened():
                print("Connected to RTSP video source {}.".format(rtsp_server_uri))
            else:
                print("Failed to connect to RTSP source {}.".format(rtsp_server_uri))
                return

    def close(self):
        if self.isOpened():
            self._stream.release()

    def isOpened(self):
        return self._stream is not None and self._stream.isOpened()

    def preview(self):
        """ Blocking function. Opens OpenCV window to display stream. """
        win_name = 'RTSP'
        cv2.namedWindow(win_name, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(win_name,20,20)
        while(self.isOpened()):
            if self._latest is not None:
                cv2.imshow(win_name,self._latest)
            if cv2.waitKey(25) & 0xFF == ord('q'):
                break
        cv2.waitKey()
        cv2.destroyAllWindows()
        cv2.waitKey()

    def _cache_update(self):
        # Worker for background thread. Quits on signal from `self.close()`.
        while self.isOpened():
            self._dropped = 0

            while self._dropped < self._drop_frame_limit and self.isOpened():
                (grabbed, frame) = self._stream.read()

                if not grabbed:
                    self._dropped += 1
                else:
                    self._dropped = 0
                    self._latest = frame

            self._stream.release()

            if self._verbose:
                announce = "Closed RTSP connection."
                if self._dropped >= self._drop_frame_limit:
                    print(announce + " - " + "Too many frames lost from RTSP connection.")
                elif not self.isOpened():
                    print(announce + " - " + "Received signal to stop.")

            if self._retry_connection:
                time.sleep(15)
                self.open()
            else:
                self.close()
                break

    def read(self):
        return self._latest

    def stop(self):
        self.close()