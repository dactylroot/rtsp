""" OpenCV Backend RTSP Client """

import cv2
from io import BytesIO
from PIL import Image

class Client:
    """ Maintain live RTSP feed without buffering. """
    _stream = None

    def __init__(self, rtsp_server_uri, verbose = False, buffer_skip = 120):
        """
            rtsp_server_uri: the path to an RTSP server. should start with "rtsp://"
            verbose: print log or not
        """
        self.rtsp_server_uri = rtsp_server_uri
        self._verbose = verbose
        self._buffer_skip = buffer_skip

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
        #self._stream.set(cv2.CAP_PROP_BUFFERSIZE,self._buffer_length)

        if self._verbose:
            if self.isOpened():
                print("Connected to video source {}.".format(self.rtsp_server_uri))
            else:
                print("Failed to connect to source {}.".format(self.rtsp_server_uri))
                return

    def close(self):
        if self.isOpened():
            self._stream.release()
            if self._verbose:
                print("Disconnected from {}".format(self.rtsp_server_uri))

    def isOpened(self):
        try:
            return self._stream is not None and self._stream.isOpened()
        except:
            return False

    def read(self):
        """ cv2.VideoCapture.set(cv2.CAP_PROP_BUFFERSIZE,1)
            Doesn't seem to work.
            Kludge: skipping `buffer_skip` frames before a read """
        self.open()
        for i in range(self._buffer_skip):
            self._stream.grab()
        (grabbed, frame) = self._stream.read()
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    def preview(self):
        """ Blocking function. Opens OpenCV window to display stream. """
        win_name = 'RTSP'
        cv2.namedWindow(win_name, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(win_name,20,20)
        self.open()
        #self._stream.set(cv2.CAP_PROP_BUFFERSIZE,15) # ideally, we increase buffer for preview
        while(self.isOpened()):
            for i in range(10):
                self._stream.grab()
            cv2.imshow(win_name,self._stream.read()[1])
            if cv2.waitKey(25) & 0xFF == ord('q'):
                break
        cv2.waitKey()
        cv2.destroyAllWindows()
        cv2.waitKey()

        #self._stream.set(cv2.CAP_PROP_BUFFERSIZE,self._buffer_length)

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

class WebcamVideoFeed:
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
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

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
