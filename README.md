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

    with rtsp.Client(rtsp_server_uri = 'rtsp://...') as client:
        _image = client.read()

        while True:
            process_image(_image)
            _image = client.read()