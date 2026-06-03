# Changelog

## 2.0.0

**Breaking changes**

- OpenCV is no longer used or required. PyAV (`av`) is now the sole media backend for RTSP decoding, RTSP encoding, and RTMP/RTMPS.

**New**

- Native Python RTSP server — `rtsp.Source` can serve a frame buffer over RTSP without any external relay or FFmpeg subprocess. A single context manager handles encoding, chunked RTP packetization, and session management.
- RTMP/RTMPS client and publisher — `rtsp.Client('rtmp://...')` and `rtsp.Source('rtmp://...', serve=False)` route to PyAV-backed implementations (`RTMPClient` / `RTMPPublisher`) when PyAV is installed.
- `rtsp.list_devices()` — enumerate local capture devices by index.
- `rtsp.Source` accepts a `frame_buffer` iterable and a `size` hint at construction time.
- `serve_forever()` on `Source` blocks and loops the buffer until Ctrl-C.
- `source.client_uri` property returns the address clients should connect to.

**Improved**

- Frame capture no longer spawns an FFmpeg subprocess; the client uses a background thread with exponential-backoff reconnection.
- `Client.read(raw=True)` returns a NumPy array directly, skipping the PIL conversion.
- `verbose=True` on any class now routes through the `rtsp` logger instead of printing directly.

## 1.1.12 and earlier

See git tags for details.
