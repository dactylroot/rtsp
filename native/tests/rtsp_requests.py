

import rtsp

with rtsp.Client(verbose=True) as client:
    client.options()

    client.describe()
