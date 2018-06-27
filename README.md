
## RTSP Package

RTSP Client. Uses [ffmpeg](https://www.ffmpeg.org/) system call for RTSP support and [Pillow](https://pillow.readthedocs.io/en/5.1.x/) for parsing and conversion.

## Features

  * fetch a single image as Pillow Image

## Examples

    import rtsp
    image = rtsp.fetch_image('rtsp://1.0.0.1/StreamId=1')
    
