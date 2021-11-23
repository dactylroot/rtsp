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

  * read most-recent RTSP frame as Pillow Image on demand
  * preview stream in OpenCV. 'q' to quit preview.
  * URI shortcuts for rapid prototyping
    * integers load a USB or webcam from starting with interface 0 via **OpenCV**, e.g. `rtsp.Client(0)`
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

## Roadmap:

I don't plan to develop this module any further, as more complex applications are better suited to use OpenCV, Gstreamer, or ffmpeg directly.

To do:
  * might improve parsing for RTSP server URIs
