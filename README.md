# RTSP

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


FFmpeg-based RTSP client

## Features

  * read most-recent RTSP frame as Pillow Image on demand
  * preview stream in a tkinter window. 'q' or ESC to quit.
  * URI shortcuts for rapid prototyping
    * integers (or numeric strings) load a local capture device via **FFmpeg**, e.g. `rtsp.Client(0)`
    * bare host strings default to `rtsp://`, e.g. `rtsp.Client('192.168.1.1/stream')`
    * 'picam' uses a Raspberry Pi camera as source e.g. `rtsp.Client('picam')`

## Examples

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

## Roadmap:

I don't plan to develop this module any further, as more complex applications are better suited to use OpenCV, Gstreamer, or ffmpeg directly.
