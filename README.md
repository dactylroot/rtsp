# RTSP Package

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


Convenience-wrapper around OpenCV-Python RTSP functions.

## Features

  * fetch a single image as Pillow Image
  * open RTSP stream and poll most recent frame as Pillow Image
  * preview stream in OpenCV
  * uniform interface for local web-cameras for rapid prototyping
    * integers will load a local USB or webcam starting with interface 0 via `OpenCV` e.g. `rtsp.Client(0)`
    * 'picam' uses a Raspberry Pi camera as source e.g. `rtsp.Client('picam')`

## Examples

Use RTSP access credentials in your connection string e.g. `RTSP_URL = f"rtsp://{USERNAME}:{PASSWORD}@192.168.1.221:554/11"`

One-off Retrieval

    import rtsp
    client = rtsp.Client(rtsp_server_uri = 'rtsp://...')
    client.read().show()
    client.close()

Stream Preview

    import rtsp
    with rtsp.Client('rtsp://...') as client:
        client.preview()

Stream to Image Type Preview

    import rtsp

    RTSP_URL = "rtsp://..."
    client = rtsp.Client(rtsp_server_uri = RTSP_URL)
    
    client.read().show()

Stream to Image Type Resize Picture

    import rtsp

    RTSP_URL = "rtsp://..."
    client = rtsp.Client(rtsp_server_uri = RTSP_URL)
    
    client.read().resize([width, height]).show()

Stream to Image Type and Rotate Picture

    import rtsp

    RTSP_URL = "rtsp://..."
    client = rtsp.Client(rtsp_server_uri = RTSP_URL)
    
    client.read().resize([client.read().size[0], client.read().size[0]]).rotate(90).resize([client.read().size[1], client.read().size[0]]).show()

Stream Image Restore (With the TimeStamp)

    import rtsp
    import datetime

    RTSP_URL = "rtsp://..."
    IMAGE_COUNT = 10

    client = rtsp.Client(rtsp_server_uri = RTSP_URL)
    while client.isOpened() and IMAGE_COUNT > 0:
        client.read().save("./"+ str(datetime.datetime.now()) +".jpg")
        IMAGE_COUNT = IMAGE_COUNT - 1

Continuous Retrieval

    import rtsp

    with rtsp.Client(rtsp_server_uri = 'rtsp://...') as client:
        _image = client.read()

        while True:
            process_image(_image)
            _image = client.read(raw=True)

## Roadmap:

I don't plan to develop this module any further, as more complex applications are better suited to use OpenCV, Gstreamer, or ffmpeg directly.

To do:
  * add better parsing for the RTSP resource URIs
