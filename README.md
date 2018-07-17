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
  * open RTSP stream in FFmpeg and poll for most recent frame

## Examples

One-off Retrieval

    import rtsp
    image = rtsp.fetch_image('rtsp://1.0.0.1/StreamId=1')

Continuous Retrieval

    import rtsp

    collector = rtsp.FFmpegListener()

    image = collector.read()

Continuous Retrieval Context Manager

    import rtsp
    with rtsp.FFmpegListener() as collector:
        _image = collector.read()

        while True:
            process_image(_image)
            _image = collector.read()