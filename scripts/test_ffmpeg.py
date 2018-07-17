""" RTSP Client
    Bare functionality relying on ffmpeg system call. """

import subprocess as _sp
from PIL import Image as _Image

_source = 'rtsp://10.38.5.145/ufirststream'

cmd = "ffmpeg -rtsp_transport tcp -i rtsp://10.38.5.145/ufirststream -update 1 _current.png"


def read():
    return _Image.open('./_current.png')

with _sp.Popen( cmd.split(' '), stderr=None ) as proc:
    read().show()



