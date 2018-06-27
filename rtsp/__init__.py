""" RTSP Client
    Bare functionality relying on ffmpeg system call. """

import io as _io
import numpy as _np
from PIL import Image as _Image
import subprocess as _sp
 
_sources = [ 'rtsp://root:pass@10.38.4.76/StreamId=1'
                ,'rtsp://10.38.5.145/ufirststream']

def _has_ffmpeg():
    """ Check if ffmpeg is installed on the system """
    try:
        if _sp.check_output(['which','ffmpeg']):
            return True
    except _sp.CalledProcessError:
        pass
    return False

def fetch_image(rtsp_server_uri = _sources[0]):
    """ Fetch a single frame using FFMPEG. Convert to PIL Image. """
    
    if not _has_ffmpeg():
        raise ModuleNotFoundError("ffmpeg not found by system call")

    # 'ffmpeg -rtsp_transport tcp -i rtsp://root:pass@10.38.4.76/StreamId=1 -loglevel quiet -frames 1 -f image2pipe -'
    ffmpeg_cmd =  ['ffmpeg',
            '-rtsp_transport','tcp',
            '-i', 'rtsp://root:pass@10.38.4.76/StreamId=1',
            '-frames', '1',
            '-loglevel','quiet',
            '-f', 'image2pipe','-']

    raw = _sp.check_output(ffmpeg_cmd)
    return _Image.open(_io.BytesIO(raw))









