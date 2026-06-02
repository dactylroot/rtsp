"""
Serve Art Nouveau images as an RTSP stream and preview them locally.
"""

import rtsp

import nouveau
morris = nouveau.Morris()

FPS = 0.2           # one frame every 5 seconds
SIZE = (960, 1200)  # scale down; source scans are 4K+

with rtsp.Source('rtsp://0.0.0.0:8554/live', fps=FPS, size=SIZE,
                       frame_buffer=morris, verbose=True) as source:
    with rtsp.Client(source.client_uri) as client:
        client.preview()   # blocks; press q or ESC to quit
