""" RTSP Client
    Bare functionality relying on ffmpeg system call. """

import io as _io
import os as _os
import time as _time
from threading import Thread as _Thread
import signal as _signal
import subprocess as _sp

from PIL import Image as _Image

# TODO can't figure out how to elegantly import and use README as docstring

_sources = [ 
              'rtsp://10.38.5.145/ufirststream'
           ]

def _check_ffmpeg():
    """ Throw exception if ffmpeg isn't found """
    try:
        if _sp.check_output(['ffmpeg','-version']):
            return
    except _sp.CalledProcessError:
        pass
    raise ModuleNotFoundError("`ffmpeg` not found by system call")

def fetch_image(rtsp_server_uri = _sources[0],timeout_secs = 15):
    """ Fetch a single frame using FFMPEG. Convert to PIL Image. """
    
    _check_ffmpeg()
    
    ffmpeg_cmd = "ffmpeg -rtsp_transport tcp -i {} -loglevel quiet -frames 1 -f image2pipe -".format(rtsp_server_uri)

    #stdout = _sp.check_output(ffmpeg_cmd,timeout = timeout_secs)
    with _sp.Popen(ffmpeg_cmd, shell=True,  stdout=_sp.PIPE) as process:
        try:
            stdout,stderr = process.communicate(timeout=timeout_secs)
        except _sp.TimeoutExpired:
            process.kill()
            raise TimeoutError("Connection to {} timed out".format(rtsp_server_uri))
    
    return _Image.open(_io.BytesIO(stdout))


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
            _time.sleep(2)
        return self.current_image

    def restart_worker(self):
        self._current_image = None
        self._runflag = True
        self._thread = _Thread(target=self._fetch_image_loop)
        self._thread.start()

    def shutdown(self,verbose=True):
        self.__exit__()
        self._verbose = verbose

    def _fetch_image_loop(self):
        """ Target for background thread """
        #image = _Image.open(_io.BytesIO(self.proc.stdout.read(-1)))
        while self._runflag:
            self._current_image = fetch_image(self._rtsp_server_uri)
            _time.sleep(self._fetch_wait_secs)
        if self._verbose:
            print("<BackgroundListener>: shut down image fetch loop")
