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


RTSP Client. Requires OpenCV-Python

## Features

  * fetch a single image as Pillow Image
  * open RTSP stream and poll most recent frame as Pillow Image
  * preview stream in OpenCV

  * Robustness
    * configurable number of frames to allow dropped
    * configure time to retry creating a new connection

### Client

    Client(rtsp_server_uri, drop_frame_limit=15, retry_connection=TRUE, verbose = TRUE)

## Examples

One-off Retrieval

    import rtsp
    client = rtsp.Client(rtsp_server_uri = 'rtsp://...')
    client.read().show()
    client.close()

Stream Preview

    import rtsp
    with rtsp.Client('rtsp://...') as client:
        client.preview()

Continuous Retrieval

    import rtsp

    with rtsp.Client(rtsp_server_uri = 'rtsp://...',drop_frame_limit=10) as client:
        _image = client.read()

        while True:
            process_image(_image)
            _image = client.read()

Verbose mode

    In [1]: import rtsp
    In [2]: client = rtsp.Client()
    Connected to RTSP video source rtsp://192.168.1.3/ufirststream/track1.
    In [3]: client.preview()
    In [4]: client.close()
    Dropped RTSP connection.
    Received signal to stop.
    In [5]: client.open()
    Connected to RTSP video source rtsp://192.168.1.3/ufirststream/track1.
    In [6]: client.read().show()
    Connected to RTSP video source rtsp://192.168.1.3/ufirststream/track1.
    In [7]: client.close()
    Dropped RTSP connection.
    Received signal to stop.

## Roadmap:
  * v1.0.0 - basic function relying on OpenCV or ffmpeg
  * v2.0.0 - lightweight native-Python implementation rtsp client functions
    * live stream reading & buffer
    * on-demand frame retrieval
  * v2.1.0 - native Python rtsp server functions