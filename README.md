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


RTSP Client. Requires [ffmpeg](https://www.ffmpeg.org/) system call for RTSP support and [Pillow](https://pillow.readthedocs.io/en/5.1.x/) for parsing and conversion.

## Features

  * fetch a single image as Pillow Image

## Examples

One-off Retrieval

    import rtsp
    image = rtsp.fetch_image('rtsp://1.0.0.1/StreamId=1')

Continuous Retrieval

    import rtsp
    import time

    collector = rtsp.BackgroundListener()

    ## image_1 may be None but has no delay
    image_1 = collector.current_image

    ## image_2 will not be None but may have a delay
    image_2 = collector.blocking_get_new_image()

    ## image_2 and image_3 will not be the same
    image_3 = collector.blocking_get_new_image(old_image = image_2)

    collector.shutdown(verbose=False)

Continuous Retrieval Context Manager

    import rtsp
    import time
    with rtsp.BackgroundListener() as collector:
        _image = collector.blocking_get_new_image()

        while True:
            process_image(_image)
            _image = collector.blocking_get_new_image(old_image = _image)