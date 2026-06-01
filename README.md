# RTSP

[![CI](https://github.com/dactylroot/rtsp/actions/workflows/test.yml/badge.svg)](https://github.com/dactylroot/rtsp/actions/workflows/test.yml)
[![PyPI version](https://badge.fury.io/py/rtsp.svg)](https://pypi.org/project/rtsp/)
[![Downloads](https://static.pepy.tech/badge/rtsp)](https://pepy.tech/project/rtsp)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

            /((((((\\\\
    =======((((((((((\\\\\
         ((           \\\\\\\
         ( (*    _/      \\\\\\\
           \    /  \      \\\\\\________________
            |  |   |      </    __             ((\\\\
            o_|   /        ____/ / _______       \ \\\\    \\\\\\\
                 |  ._    / __/ __(_-</ _ \       \ \\\\\\\\\\\\\\\\
                 | /     /_/  \__/___/ .__/       /    \\\\\\\     \\
         .______/\/     /           /_/           /         \\\
        / __.____/    _/         ________(       /\
       / / / ________/`---------'         \     /  \_
      / /  \ \                             \   \ \_  \
     ( <    \ \                             >  /    \ \
      \/      \\_                          / /       > )
               \_|                        / /       / /
                                        _//       _//
                                       /_|       /_|


FFmpeg-based RTSP client and microserver

## Features

  * read most-recent RTSP frame as Pillow Image on demand
  * preview stream in a tkinter window. 'q' or ESC to quit.
  * URI shortcuts for rapid prototyping
    * integers (or numeric strings) load a local capture device via **FFmpeg**, e.g. `rtsp.Client(0)`
    * bare host strings default to `rtsp://`, e.g. `rtsp.Client('192.168.1.1/stream')`
    * 'picam' uses a Raspberry Pi camera as source e.g. `rtsp.Client('picam')`
  * lightweight RTSP server
 
## Examples

### Client Use

Use RTSP access credentials in your connection string e.g.

    RTSP_URL = f"rtsp://{USERNAME}:{PASSWORD}@192.168.1.221:554/11"

One-off Retrieval

    import rtsp
    client = rtsp.Client(rtsp_server_uri = 'rtsp://...', verbose=True)
    client.read().show()
    client.close()

Stream Preview

    import rtsp
    with rtsp.Client(0) as client: # previews USB webcam 0
        client.preview()

Continuous Retrieval

    import rtsp

    with rtsp.Client(rtsp_server_uri = 'rtsp://...') as client:
        _image = client.read()

        while True:
            process_image(_image)
            _image = client.read(raw=True)

Resize Retrieval Image

    import rtsp

    RTSP_URL = "rtsp://..."
    client = rtsp.Client(rtsp_server_uri = RTSP_URL)

    width = 640
    height = 480

    client.read().resize([width, height]).show()
    client.close()

Rotate Retrieval Image

    import rtsp

    RTSP_URL = "rtsp://..."
    client = rtsp.Client(rtsp_server_uri = RTSP_URL)

    client.read().resize([client.read().size[0], client.read().size[0]]).rotate(90).resize([client.read().size[1], client.read().size[0]]).show()
    client.close()

Save Retrieval Image (With the TimeStamp Format and Set Number of Save Image)

    import rtsp
    import datetime

    RTSP_URL = "rtsp://..."
    IMAGE_COUNT = 10

    client = rtsp.Client(rtsp_server_uri = RTSP_URL)
    while client.isOpened() and IMAGE_COUNT > 0:
        client.read().save("./"+ str(datetime.datetime.now()) +".jpg")
        IMAGE_COUNT = IMAGE_COUNT - 1
    client.close()

### Source Use

Single-client stream using FFmpeg server. FFmpeg listens for one viewer.
`serve_forever()` blocks and restarts automatically after each client disconnect:

    import rtsp

    _frame_buffer=['frame1.jpg', 'frame2.jpg', 'frame3.jpg']

    with rtsp.Source('rtsp://0.0.0.0:8554/live', frame_buffer=_frame_buffer) as source:
        source.serve_forever()   # blocks; Ctrl-C to stop

Multi-client stream via [MediaMTX](https://github.com/bluenviron/mediamtx) relay.
Also, frames can be added incrementally with `put()`:

    # Terminal: ./mediamtx          (listens on :8554 by default)

    import rtsp

    # serve=False pushes to the running MediaMTX relay.
    # Any number of Client() instances can then read from the same URI.
    with rtsp.Source('rtsp://localhost:8554/live', serve=False) as source:
        for frame in incoming_frames():
            source.put(frame)

    # Elsewhere, any number of concurrent readers:
    with rtsp.Client('rtsp://localhost:8554/live') as client:
        client.preview()

## When to use something else

For performance-intensive pipelines, consider OpenCV, GStreamer, or FFmpeg directly. For multi-client streaming without a relay, see [MediaMTX](https://github.com/bluenviron/mediamtx).
