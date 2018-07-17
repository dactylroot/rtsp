"""

    Requires [ffmpeg](https://www.ffmpeg.org/) system call for RTSP support and [Pillow](https://pillow.readthedocs.io/en/5.1.x/) for parsing and conversion.

"""



import subprocess as _sp


class FFmpegClient:
    """ Continuously fetches image frames from RTSP server in a background thread. """

    def running(self):
        """ Is background thread running """
        return (self._proc != None) and (self._proc.poll() == None)

    def _check_ffmpeg():
        """ Throw exception if ffmpeg isn't found """
        try:
            if _sp.check_output(['ffmpeg','-version']):
                return
        except:
            pass
        raise ModuleNotFoundError("`ffmpeg` not found by system call")

    def fetch_image(self,rtsp_server_uri = _source,timeout_secs = 15):
        """ Fetch a single frame using FFMPEG. Convert to PIL Image. Slow. """
        
        self._check_ffmpeg()
        cmd = "ffmpeg -rtsp_transport tcp -i {} -loglevel quiet -frames 1 -f image2pipe -".format(rtsp_server_uri)

        #stdout = _sp.check_output(ffmpeg_cmd,timeout = timeout_secs)
        with _sp.Popen(cmd, shell=True,  stdout=_sp.PIPE) as process:
            try:
                stdout,stderr = process.communicate(timeout=timeout_secs)
            except _sp.TimeoutExpired as e:
                process.kill()
                raise TimeoutError("Connection to {} timed out".format(rtsp_server_uri),e)
        
        return _Image.open(_io.BytesIO(stdout))

    def __init__(self, rtsp_server_uri = _source):
        self._check_ffmpeg()

        self._proc = None
        self._cache_path = _os.path.abspath(_os.path.dirname(__file__))+'/_current.png'
        self._rtsp_server_uri = rtsp_server_uri

    def __enter__(self,*args,**kwargs):
        """ Returns the object which later will have __exit__ called.
            This relationship creates a context manager. """
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        """ Together with __enter__, allows support for `with-` clauses. """
        self.shutdown()

    def read(self):
        if not self.running:
            self.start()
        return _Image.open(self._cache_path)

    def start(self):
        cmd = "ffmpeg -rtsp_transport tcp -r 0.5 -i {} -update 1 {}".format(self._rtsp_server_uri,self._cache_path)
        self._proc = _sp.Popen( cmd.split(' '), stdout=_sp.DEVNULL, stderr=_sp.STDOUT )

    def shutdown(self):
        if self._proc:
            self._proc.terminate()
            self._proc = None
