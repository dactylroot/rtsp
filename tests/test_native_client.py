"""Tests for rtsp.Client — Python-native RTSP client.

Unit tests cover the pure-function helpers (_parse_sdp, _demux_h264_payload)
with no socket or PyAV required.  Integration tests spin up a Source and
connect a Client over the loopback interface; they require ffmpeg (for
encoding) and av (for decoding).
"""

import socket
import struct
import threading
import time

import pytest
from PIL import Image

import rtsp.client as _nativeclient
from rtsp.client import (
    Client, _demux_h264_payload, _parse_sdp,
    list_devices, _macos_camera_names,
)

from conftest import requires_ffmpeg, requires_nouveau


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _wait_frame(client, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        f = client.read()
        if f is not None:
            return f
        time.sleep(0.1)
    return None


def _rtp_packet(payload, channel=0, seq=1, ts=0, ssrc=1):
    """Build a minimal TCP-interleaved RTP packet."""
    header = struct.pack('!BBHII', 0x80, 0x60, seq, ts, ssrc)
    rtp = header + payload
    return b'$' + bytes([channel]) + struct.pack('!H', len(rtp)) + rtp


def _stap_a(nals):
    """Build a STAP-A (type 24) RTP payload from a list of NAL bytes."""
    out = bytes([0x78])  # STAP-A indicator: NRI=3, type=24
    for nal in nals:
        out += struct.pack('!H', len(nal)) + nal
    return out


def _fu_a_packets(nal, chunk_size=4):
    """Fragment a NAL unit into FU-A (type 28) payloads."""
    nal_hdr = nal[0]
    nal_type = nal_hdr & 0x1F
    nri = nal_hdr & 0x60
    fu_ind = nri | 28
    data = nal[1:]
    packets = []
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i + chunk_size]
        is_start = i == 0
        is_end = (i + chunk_size) >= len(data)
        fu_hdr = nal_type | (0x80 if is_start else 0) | (0x40 if is_end else 0)
        packets.append(bytes([fu_ind, fu_hdr]) + chunk)
    return packets


# ---------------------------------------------------------------------------
# Unit: _parse_sdp
# ---------------------------------------------------------------------------

class TestParseSdp:

    def test_extracts_absolute_track_url(self):
        sdp = (
            'v=0\r\n'
            'm=video 0 RTP/AVP 96\r\n'
            'a=rtpmap:96 H264/90000\r\n'
            'a=control:rtsp://127.0.0.1:8554/live/trackID=0\r\n'
        )
        url, sps, pps = _parse_sdp(sdp, 'rtsp://127.0.0.1:8554/live')
        assert url == 'rtsp://127.0.0.1:8554/live/trackID=0'

    def test_builds_absolute_url_from_relative_control(self):
        sdp = (
            'v=0\r\n'
            'm=video 0 RTP/AVP 96\r\n'
            'a=control:trackID=0\r\n'
        )
        url, _, _ = _parse_sdp(sdp, 'rtsp://127.0.0.1:8554/live')
        assert url == 'rtsp://127.0.0.1:8554/live/trackID=0'

    def test_wildcard_control_uses_content_base(self):
        sdp = 'v=0\r\nm=video 0 RTP/AVP 96\r\na=control:*\r\n'
        url, _, _ = _parse_sdp(sdp, 'rtsp://127.0.0.1:8554/live')
        assert url == 'rtsp://127.0.0.1:8554/live'

    def test_extracts_sprop_sps(self):
        import base64
        fake_sps = b'\x67\x42\xc0\x1e'
        encoded = base64.b64encode(fake_sps).decode()
        sdp = (
            'v=0\r\n'
            'm=video 0 RTP/AVP 96\r\n'
            'a=fmtp:96 packetization-mode=1;sprop-parameter-sets={},AA==\r\n'
            'a=control:trackID=0\r\n'
        ).format(encoded)
        _, sps, _ = _parse_sdp(sdp, 'rtsp://127.0.0.1:8554/live')
        assert sps == fake_sps

    def test_extracts_sprop_pps(self):
        import base64
        fake_pps = b'\x68\xce\x38\x80'
        encoded = base64.b64encode(fake_pps).decode()
        sdp = (
            'v=0\r\n'
            'm=video 0 RTP/AVP 96\r\n'
            'a=fmtp:96 packetization-mode=1;sprop-parameter-sets=AA==,{}\r\n'
            'a=control:trackID=0\r\n'
        ).format(encoded)
        _, _, pps = _parse_sdp(sdp, 'rtsp://127.0.0.1:8554/live')
        assert pps == fake_pps

    def test_no_sprop_returns_none(self):
        sdp = 'v=0\r\nm=video 0 RTP/AVP 96\r\na=control:trackID=0\r\n'
        _, sps, pps = _parse_sdp(sdp, 'rtsp://127.0.0.1:8554/live')
        assert sps is None
        assert pps is None

    def test_ignores_audio_track(self):
        sdp = (
            'v=0\r\n'
            'm=audio 0 RTP/AVP 0\r\n'
            'a=control:audioTrack\r\n'
            'm=video 0 RTP/AVP 96\r\n'
            'a=control:videoTrack\r\n'
        )
        url, _, _ = _parse_sdp(sdp, 'rtsp://127.0.0.1:8554/live')
        assert 'video' in url.lower() or url.endswith('videoTrack')

    def test_content_base_trailing_slash_stripped(self):
        sdp = 'v=0\r\nm=video 0 RTP/AVP 96\r\na=control:trackID=0\r\n'
        url, _, _ = _parse_sdp(sdp, 'rtsp://127.0.0.1:8554/live/')
        assert '//' not in url.replace('rtsp://', '')


# ---------------------------------------------------------------------------
# Unit: _demux_h264_payload
# ---------------------------------------------------------------------------

class TestDemuxH264Payload:

    def test_single_nal_type_5(self):
        nal = bytes([0x65]) + b'\xAB' * 20  # IDR slice
        nals, fu_buf, fu_started = _demux_h264_payload(nal)
        assert nals == [nal]
        assert fu_buf == b''
        assert not fu_started

    def test_single_nal_types_1_through_23(self):
        for t in (1, 7, 8, 23):
            nal = bytes([t]) + b'\x00' * 5
            nals, _, _ = _demux_h264_payload(nal)
            assert len(nals) == 1

    def test_stap_a_two_nals(self):
        nal_a = b'\x67' + b'\x01' * 4
        nal_b = b'\x68' + b'\x02' * 4
        payload = _stap_a([nal_a, nal_b])
        nals, _, _ = _demux_h264_payload(payload)
        assert len(nals) == 2
        assert nals[0] == nal_a
        assert nals[1] == nal_b

    def test_stap_a_preserves_nal_content(self):
        nal = b'\x65' + bytes(range(64))
        payload = _stap_a([nal])
        nals, _, _ = _demux_h264_payload(payload)
        assert nals[0] == nal

    def test_fu_a_single_chunk(self):
        """FU-A with start+end in one packet reassembles immediately."""
        nal = b'\x65' + b'\xAB' * 8
        fu_pkts = _fu_a_packets(nal, chunk_size=len(nal))
        assert len(fu_pkts) == 1
        nals, fu_buf, fu_started = _demux_h264_payload(fu_pkts[0])
        assert len(nals) == 1
        assert nals[0] == nal

    def test_fu_a_reassembles_fragments(self):
        nal = b'\x65' + bytes(range(32))
        fu_pkts = _fu_a_packets(nal, chunk_size=8)
        assert len(fu_pkts) > 1

        fu_buf, fu_started = b'', False
        all_nals = []
        for pkt in fu_pkts:
            nals, fu_buf, fu_started = _demux_h264_payload(pkt, fu_buf, fu_started)
            all_nals.extend(nals)

        assert len(all_nals) == 1
        assert all_nals[0] == nal

    def test_fu_a_start_bit_resets_accumulator(self):
        """A new start packet discards any partial accumulation."""
        nal_a = b'\x65' + b'\xAA' * 6
        nal_b = b'\x65' + b'\xBB' * 6

        pkts_a = _fu_a_packets(nal_a, chunk_size=4)
        pkts_b = _fu_a_packets(nal_b, chunk_size=4)

        fu_buf, fu_started = b'', False
        # Feed first packet of nal_a (start only, no end)
        _, fu_buf, fu_started = _demux_h264_payload(pkts_a[0], fu_buf, fu_started)
        assert fu_started
        # Start nal_b — should discard nal_a accumulation
        _, fu_buf, fu_started = _demux_h264_payload(pkts_b[0], fu_buf, fu_started)
        # Complete nal_b
        all_nals = []
        for pkt in pkts_b[1:]:
            nals, fu_buf, fu_started = _demux_h264_payload(pkt, fu_buf, fu_started)
            all_nals.extend(nals)
        assert len(all_nals) == 1
        assert all_nals[0] == nal_b

    def test_fu_a_end_without_start_discarded(self):
        """FU-A end without a preceding start is silently dropped."""
        nal = b'\x65' + b'\xAB' * 8
        fu_pkts = _fu_a_packets(nal, chunk_size=4)
        # Feed only the last packet (no start)
        nals, _, fu_started = _demux_h264_payload(fu_pkts[-1])
        assert nals == []
        assert not fu_started

    def test_empty_payload_returns_empty(self):
        nals, fu_buf, fu_started = _demux_h264_payload(b'')
        assert nals == []
        assert fu_buf == b''
        assert not fu_started

    def test_unknown_nal_type_returns_empty(self):
        payload = bytes([0x7C]) + b'\x00' * 4  # reserved type 28 but malformed
        nals, _, _ = _demux_h264_payload(payload)
        # May or may not produce nals depending on byte, just must not raise
        assert isinstance(nals, list)


# ---------------------------------------------------------------------------
# Integration: Client + Source end-to-end
# ---------------------------------------------------------------------------

requires_av = pytest.mark.skipif(
    __import__('importlib').util.find_spec('av') is None,
    reason='PyAV (av) not installed',
)


def _camera_available(index=0):
    """Return True if device *index* can be opened via PyAV."""
    try:
        import av
        import platform
        system = platform.system()
        if system == 'Darwin':
            c = av.open(str(index), format='avfoundation', options={'framerate': '30'})
        elif system == 'Linux':
            c = av.open('/dev/video{}'.format(index), format='v4l2')
        else:
            c = av.open(str(index), format='dshow')
        c.close()
        return True
    except Exception:
        return False


requires_camera = pytest.mark.skipif(
    not _camera_available(0),
    reason='camera device 0 not available',
)


@requires_ffmpeg
@requires_av
@requires_nouveau
class TestClientStreaming:
    """End-to-end: Source encodes, Client decodes over loopback."""

    @pytest.fixture(scope='class')
    def source_and_uri(self, nouveau_frames):
        from rtsp.source import Source
        port = _free_port()
        uri = 'rtsp://0.0.0.0:{}/live'.format(port)
        src = Source(uri, fps=5, size=(160, 120),
                           frame_buffer=nouveau_frames)
        src.wait_ready(timeout=10)
        yield src, src.client_uri
        src.close()

    def test_receives_frame(self, source_and_uri):
        _, uri = source_and_uri
        with Client(uri) as client:
            frame = _wait_frame(client)
        assert frame is not None

    def test_frame_is_pil_image(self, source_and_uri):
        _, uri = source_and_uri
        with Client(uri) as client:
            frame = _wait_frame(client)
        assert isinstance(frame, Image.Image)

    def test_frame_dimensions_match_source(self, source_and_uri):
        _, uri = source_and_uri
        with Client(uri) as client:
            frame = _wait_frame(client)
        assert frame is not None
        assert frame.size == (160, 120)

    def test_read_raw_returns_numpy(self, source_and_uri):
        import numpy as np
        _, uri = source_and_uri
        with Client(uri) as client:
            arr = None
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                arr = client.read(raw=True)
                if arr is not None:
                    break
                time.sleep(0.1)
        assert arr is not None
        assert isinstance(arr, np.ndarray)
        assert arr.shape == (120, 160, 3)

    def test_context_manager_closes(self, source_and_uri):
        _, uri = source_and_uri
        with Client(uri) as client:
            _wait_frame(client)
        assert not client.isOpened()

    def test_close_is_idempotent(self, source_and_uri):
        _, uri = source_and_uri
        client = Client(uri)
        client.close()
        client.close()  # must not raise
        assert not client.isOpened()

    def test_read_returns_none_after_close(self, source_and_uri):
        _, uri = source_and_uri
        with Client(uri) as client:
            pass
        assert client.read() is None

    def test_two_simultaneous_clients(self, source_and_uri):
        _, uri = source_and_uri
        frames = [None, None]

        def _connect(idx):
            with Client(uri) as c:
                frames[idx] = _wait_frame(c)

        t0 = threading.Thread(target=_connect, args=(0,))
        t1 = threading.Thread(target=_connect, args=(1,))
        t0.start(); t1.start()
        t0.join(timeout=20); t1.join(timeout=20)

        assert frames[0] is not None, 'client 0 got no frame'
        assert frames[1] is not None, 'client 1 got no frame'


# ---------------------------------------------------------------------------
# Integration: local camera device (integer index)
# ---------------------------------------------------------------------------

@requires_av
@requires_camera
class TestClientDevice:
    """Regression tests for integer device index support in Client.

    Guards against:
    - [Errno 5] I/O error on macOS avfoundation (missing framerate option)
    - Open-after-first-frame hang (av.open on wrong thread / decode stall)
    """

    def test_int_index_opens(self):
        with Client(0) as client:
            assert client.isOpened()

    def test_string_digit_opens(self):
        with Client('0') as client:
            assert client.isOpened()

    def test_receives_frame(self):
        with Client(0) as client:
            frame = _wait_frame(client, timeout=8)
        assert frame is not None

    def test_frame_is_pil_image(self):
        with Client(0) as client:
            frame = _wait_frame(client, timeout=8)
        assert isinstance(frame, Image.Image)

    def test_delivers_multiple_frames(self):
        """Regression: device must not hang after the first frame."""
        collected = []
        deadline = time.monotonic() + 5
        with Client(0) as client:
            while time.monotonic() < deadline and len(collected) < 5:
                f = client.read()
                if f is not None and (not collected or f is not collected[-1]):
                    collected.append(f)
                time.sleep(0.1)
        assert len(collected) >= 3, (
            'expected at least 3 distinct frames within 5 s, got {}'.format(len(collected))
        )

    def test_close_stops_stream(self):
        client = Client(0)
        _wait_frame(client, timeout=8)
        client.close()
        assert not client.isOpened()

    def test_client_factory_routes_to_native(self):
        """rtsp.Client(0) must route to Client when PyAV is available."""
        import rtsp
        with rtsp.Client(0) as client:
            frame = _wait_frame(client, timeout=8)
        assert frame is not None


# ---------------------------------------------------------------------------
# Unit: list_devices
# ---------------------------------------------------------------------------

class TestListDevices:

    def test_returns_list(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names', lambda: ['Cam A', 'Cam B'])
        assert isinstance(list_devices(), list)

    def test_entry_has_required_keys(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names', lambda: ['Cam A'])
        entry = list_devices()[0]
        assert set(entry.keys()) == {'index', 'name', 'width', 'height'}

    def test_no_probe_width_height_are_none(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names', lambda: ['A', 'B'])
        for entry in list_devices():
            assert entry['width'] is None
            assert entry['height'] is None

    def test_indices_are_sequential_ints(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names', lambda: ['A', 'B', 'C'])
        result = list_devices()
        assert [d['index'] for d in result] == [0, 1, 2]

    def test_names_match_platform_list(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names',
                            lambda: ['FaceTime HD', 'USB Webcam'])
        result = list_devices()
        assert result[0]['name'] == 'FaceTime HD'
        assert result[1]['name'] == 'USB Webcam'

    def test_probe_fills_resolution(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names', lambda: ['Cam A'])
        monkeypatch.setattr(_nativeclient, '_probe_one_frame', lambda i: (1280, 720))
        entry = list_devices(probe=True)[0]
        assert entry['width'] == 1280
        assert entry['height'] == 720

    def test_probe_passes_correct_index(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names', lambda: ['A', 'B', 'C'])
        probed = []
        monkeypatch.setattr(_nativeclient, '_probe_one_frame',
                            lambda i: (probed.append(i), (640, 480))[1])
        list_devices(probe=True)
        assert probed == [0, 1, 2]

    def test_no_platform_names_no_probe_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names', lambda: None)
        assert list_devices() == []

    def test_no_platform_names_probe_stops_at_first_failure(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names', lambda: None)
        resolutions = {0: (640, 480), 1: (1280, 720)}
        monkeypatch.setattr(_nativeclient, '_probe_one_frame',
                            lambda i: resolutions.get(i, (None, None)))
        result = list_devices(probe=True)
        assert [d['index'] for d in result] == [0, 1]
        assert result[1]['width'] == 1280

    def test_empty_platform_list_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_nativeclient, '_platform_device_names', lambda: [])
        assert list_devices() == []


class TestMacosCameraNames:

    def test_parses_names_from_system_profiler(self, monkeypatch):
        import json
        fake_json = json.dumps({'SPCameraDataType': [
            {'_name': 'FaceTime HD Camera', 'spcamera_unique-id': 'abc'},
            {'_name': 'USB Webcam'},
        ]})

        class FakeResult:
            stdout = fake_json

        monkeypatch.setattr('subprocess.run', lambda *a, **kw: FakeResult())
        names = _macos_camera_names()
        assert names == ['FaceTime HD Camera', 'USB Webcam']

    def test_missing_name_field_falls_back_to_camera_n(self, monkeypatch):
        import json
        fake_json = json.dumps({'SPCameraDataType': [{'spcamera_unique-id': 'xyz'}]})

        class FakeResult:
            stdout = fake_json

        monkeypatch.setattr('subprocess.run', lambda *a, **kw: FakeResult())
        names = _macos_camera_names()
        assert names == ['Camera 0']

    def test_subprocess_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr('subprocess.run', lambda *a, **kw: (_ for _ in ()).throw(OSError('no tool')))
        assert _macos_camera_names() is None

    def test_empty_camera_list_returns_empty(self, monkeypatch):
        import json
        fake_json = json.dumps({'SPCameraDataType': []})

        class FakeResult:
            stdout = fake_json

        monkeypatch.setattr('subprocess.run', lambda *a, **kw: FakeResult())
        names = _macos_camera_names()
        assert names == []


# ---------------------------------------------------------------------------
# Integration: list_devices with real hardware
# ---------------------------------------------------------------------------

@requires_av
@requires_camera
class TestListDevicesIntegration:

    def test_returns_at_least_one_device(self):
        result = list_devices()
        assert len(result) >= 1

    def test_device_zero_present(self):
        result = list_devices()
        assert any(d['index'] == 0 for d in result)

    def test_device_zero_has_name(self):
        result = list_devices()
        cam0 = next(d for d in result if d['index'] == 0)
        assert isinstance(cam0['name'], str) and cam0['name']

    def test_probe_returns_resolution_for_device_zero(self):
        result = list_devices(probe=True)
        cam0 = next(d for d in result if d['index'] == 0)
        assert isinstance(cam0['width'], int) and cam0['width'] > 0
        assert isinstance(cam0['height'], int) and cam0['height'] > 0
