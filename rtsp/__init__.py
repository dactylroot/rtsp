""" RTSP Client
    Bare functionality relying on ffmpeg system call. """

import io as _io
import subprocess as _sp
import time
from threading import Thread

from PIL import Image as _Image

with open('README.md') as f:
    __doc__ = f.read()
 
_sources = [ 'rtsp://root:pass@10.38.4.76/StreamId=1'
            ,'rtsp://10.38.5.145/ufirststream' ]

def _has_ffmpeg():
    """ Check if ffmpeg is installed on the system """
    try:
        if _sp.check_output(['ffmpeg','-version']):
            return True
    except _sp.CalledProcessError:
        pass
    return False

def _check_ffmpeg():
    if not _has_ffmpeg():
        raise ModuleNotFoundError("`ffmpeg` not found by system call")

def fetch_image(rtsp_server_uri = _sources[0]):
    """ Fetch a single frame using FFMPEG. Convert to PIL Image. """
    
    _check_ffmpeg()
    
    # ffmpeg -rtsp_transport tcp -i rtsp://root:pass@1.0.0.1/StreamId=1 -loglevel quiet -frames 1 -f image2pipe -
    ffmpeg_cmd =  ['ffmpeg',
            '-rtsp_transport','tcp',
            '-i', 'rtsp://root:pass@10.38.4.76/StreamId=1',
            '-frames', '1',
            '-loglevel','quiet',
            '-f', 'image2pipe','-']

    raw = _sp.check_output(ffmpeg_cmd)
    return _Image.open(_io.BytesIO(raw))

def _fetch_batch():
    """ Returns three images concatenated. Don't currently know how to delimit and separate them. """
    cmd = ['ffmpeg', '-rtsp_transport', 'tcp', '-i', 'rtsp://root:pass@10.38.4.76/StreamId=2',  '-frames', '3', '-f', 'image2pipe', '-']
    
    return _sp.check_output(cmd)

class BackgroundListener:
    """ Continuously fetches image frames from RTSP server in a background thread. """
    @property
    def current_image(self):
        return self._current_image

    def __init__(self,rtsp_server_uri = _sources[0],fetch_wait_secs = 10):
        _check_ffmpeg()

        self._rtsp_server_uri = rtsp_server_uri
        self._fetch_wait_secs = fetch_wait_secs
        self._verbose = False
        self.restart_worker()

    def __enter__(self,*args,**kwargs):
        """ Returns the object which later will have __exit__ called.
            This relationship creates a context manager. """
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        """ Together with __enter__, allows support for `with-` clauses. """
        self._runflag = False

    def blocking_get_new_image(self,old_image = None):
        """ Wait until a new image is ready, then return it """
        while self.current_image is old_image:
            time.sleep(2)
        return self.current_image

    def restart_worker(self):
        self._current_image = None
        self._runflag = True
        self._thread = Thread(target=self._fetch_image_loop)
        self._thread.start()

    def shutdown(self,verbose=True):
        self.__exit__()
        self._verbose = verbose

    def _fetch_image_loop(self):
        """ Target for background thread """
        #image = _Image.open(_io.BytesIO(self.proc.stdout.read(-1)))
        while self._runflag:
            self._current_image = fetch_image(self._rtsp_server_uri)
            time.sleep(self._fetch_wait_secs)
        if self._verbose:
            print("<BackgroundListener>: shut down image fetch loop")
