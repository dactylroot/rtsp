"""Tests for rtsp.Client.

Unit tests cover the pure-function helpers (_parse_sdp, _demux_h264_payload)
with no socket or PyAV required.  Integration tests spin up a Source and
connect a Client over the loopback interface; they require ffmpeg (for
encoding) and av (for decoding).
"""

import base64
import errno
import logging
import socket
import struct
import threading
import time
from fractions import Fraction
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

try:
    import av as _av
except ImportError:
    _av = None
import rtsp
import rtsp.client as _client
from rtsp.client import (
    Client, _demux_h264_payload, _parse_sdp,
    _device_open_kwargs,
    list_devices, _macos_camera_names, _probe_one_frame,
)
from rtsp._utils import _enable_verbose
from rtsp.source import _RTPPacketizer, _split_nals

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

    def test_malformed_sps_base64_returns_none(self):
        sdp = (
            'v=0\r\n'
            'm=video 0 RTP/AVP 96\r\n'
            'a=fmtp:96 sprop-parameter-sets=AAAA,BBBB\r\n'
            'a=control:trackID=0\r\n'
        )
        with patch('rtsp.client.base64.b64decode', side_effect=ValueError('bad')):
            _, sps, pps = _parse_sdp(sdp, '')
        assert sps is None
        assert pps is None

    def test_malformed_pps_base64_leaves_sps_intact(self):
        valid_sps = b'\x67\x42\x80\x1e'
        sdp = (
            'v=0\r\n'
            'm=video 0 RTP/AVP 96\r\n'
            'a=fmtp:96 sprop-parameter-sets={},{}\r\n'
            'a=control:trackID=0\r\n'
        ).format(base64.b64encode(valid_sps).decode(), 'BBBB')

        call_count = [0]
        real_b64decode = base64.b64decode

        def selective_raise(s, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 2:
                raise ValueError('bad pps')
            return real_b64decode(s, *a, **kw)

        with patch('rtsp.client.base64.b64decode', side_effect=selective_raise):
            _, sps, pps = _parse_sdp(sdp, '')
        assert sps == valid_sps
        assert pps is None


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
        # Starting nal_b should discard the partial nal_a accumulation.
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

    def test_stap_a_zero_length_nal_skipped(self):
        nal = b'\x65' + b'\xAB' * 4
        payload = (
            bytes([0x78])
            + struct.pack('!H', 0)
            + struct.pack('!H', len(nal)) + nal
        )
        nals, _, _ = _demux_h264_payload(payload)
        assert nals == [nal]

    def test_stap_a_truncated_nal_skipped(self):
        payload = (
            bytes([0x78])
            + struct.pack('!H', 100)
            + b'\x65\xAB\xAB'
        )
        nals, _, _ = _demux_h264_payload(payload)
        assert nals == []


# ---------------------------------------------------------------------------
# Integration: Client + Source end-to-end
# ---------------------------------------------------------------------------

requires_av = pytest.mark.skipif(
    __import__('importlib').util.find_spec('av') is None,
    reason='PyAV (av) not installed',
)


def _camera_available(index=0, timeout=3):
    """Return True if device *index* can be opened via PyAV within *timeout* seconds.

    Called at collection time for the requires_camera marker, so it must never block.
    """
    import threading
    result = [False]

    def _probe():
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
            result[0] = True
        except Exception:
            pass

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result[0]


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
# Integration: streaming with synthetic frames (CI-compatible)
# ---------------------------------------------------------------------------

@requires_av
class TestClientStreamingCI:
    """End-to-end streaming tests using synthetic frames — no external data needed."""

    @pytest.fixture(scope='class')
    def source_and_uri(self, synthetic_frames):
        from rtsp.source import Source
        port = _free_port()
        uri = 'rtsp://0.0.0.0:{}/live'.format(port)
        src = Source(uri, fps=5, size=(160, 120), frame_buffer=synthetic_frames)
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

    def test_read_returns_none_after_close(self, source_and_uri):
        _, uri = source_and_uri
        with Client(uri) as client:
            pass
        assert client.read() is None

    def test_close_is_idempotent(self, source_and_uri):
        _, uri = source_and_uri
        client = Client(uri)
        client.close()
        client.close()
        assert not client.isOpened()

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

    def test_client_factory_routes_to_rtsp_client(self):
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
        monkeypatch.setattr(_client, '_platform_device_names', lambda: ['Cam A', 'Cam B'])
        assert isinstance(list_devices(), list)

    def test_entry_has_required_keys(self, monkeypatch):
        monkeypatch.setattr(_client, '_platform_device_names', lambda: ['Cam A'])
        entry = list_devices()[0]
        assert set(entry.keys()) == {'index', 'name', 'width', 'height'}

    def test_no_probe_width_height_are_none(self, monkeypatch):
        monkeypatch.setattr(_client, '_platform_device_names', lambda: ['A', 'B'])
        for entry in list_devices():
            assert entry['width'] is None
            assert entry['height'] is None

    def test_indices_are_sequential_ints(self, monkeypatch):
        monkeypatch.setattr(_client, '_platform_device_names', lambda: ['A', 'B', 'C'])
        result = list_devices()
        assert [d['index'] for d in result] == [0, 1, 2]

    def test_names_match_platform_list(self, monkeypatch):
        monkeypatch.setattr(_client, '_platform_device_names',
                            lambda: ['FaceTime HD', 'USB Webcam'])
        result = list_devices()
        assert result[0]['name'] == 'FaceTime HD'
        assert result[1]['name'] == 'USB Webcam'

    def test_probe_fills_resolution(self, monkeypatch):
        monkeypatch.setattr(_client, '_platform_device_names', lambda: ['Cam A'])
        monkeypatch.setattr(_client, '_probe_one_frame', lambda i: (1280, 720))
        entry = list_devices(probe=True)[0]
        assert entry['width'] == 1280
        assert entry['height'] == 720

    def test_probe_passes_correct_index(self, monkeypatch):
        monkeypatch.setattr(_client, '_platform_device_names', lambda: ['A', 'B', 'C'])
        probed = []
        monkeypatch.setattr(_client, '_probe_one_frame',
                            lambda i: (probed.append(i), (640, 480))[1])
        list_devices(probe=True)
        assert probed == [0, 1, 2]

    def test_no_platform_names_no_probe_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_client, '_platform_device_names', lambda: None)
        assert list_devices() == []

    def test_no_platform_names_probe_stops_at_first_failure(self, monkeypatch):
        monkeypatch.setattr(_client, '_platform_device_names', lambda: None)
        resolutions = {0: (640, 480), 1: (1280, 720)}
        monkeypatch.setattr(_client, '_probe_one_frame',
                            lambda i: resolutions.get(i, (None, None)))
        result = list_devices(probe=True)
        assert [d['index'] for d in result] == [0, 1]
        assert result[1]['width'] == 1280

    def test_empty_platform_list_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_client, '_platform_device_names', lambda: [])
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

    def test_malformed_json_returns_none(self, monkeypatch):
        class FakeResult:
            stdout = 'not valid json {'

        monkeypatch.setattr('subprocess.run', lambda *a, **kw: FakeResult())
        assert _macos_camera_names() is None

    def test_unexpected_json_structure_returns_none(self, monkeypatch):
        # json.loads succeeds but result is a list, not a dict. .get() raises AttributeError.
        class FakeResult:
            stdout = '[]'

        monkeypatch.setattr('subprocess.run', lambda *a, **kw: FakeResult())
        assert _macos_camera_names() is None


# ---------------------------------------------------------------------------
# Unit: _probe_one_frame EAGAIN retry logic
# ---------------------------------------------------------------------------

class TestProbeOneFrame:

    @staticmethod
    def _mock_container():
        mock_vs = MagicMock()
        mock_vs.type = 'video'
        mock_container = MagicMock()
        mock_container.streams = [mock_vs]
        return mock_container

    def test_eagain_retries_up_to_30_times(self):
        mock_container = self._mock_container()
        sleeps = []

        def demux(vs_arg):
            def gen():
                e = OSError()
                e.errno = errno.EAGAIN
                raise e
                yield
            return gen()

        mock_container.demux.side_effect = demux

        with patch('rtsp.client._av') as mock_av, \
             patch('rtsp.client.time.sleep', side_effect=sleeps.append):
            mock_av.open.return_value = mock_container
            result = _probe_one_frame(0)

        assert result == (None, None)
        assert len(sleeps) == 30

    def test_eagain_then_success_returns_dimensions(self):
        mock_container = self._mock_container()
        mock_frame = MagicMock()
        mock_frame.width = 1280
        mock_frame.height = 720
        mock_packet = MagicMock()
        mock_packet.decode.return_value = [mock_frame]

        calls = [0]

        def demux(vs_arg):
            calls[0] += 1
            count = calls[0]

            def gen():
                if count == 1:
                    e = OSError()
                    e.errno = errno.EAGAIN
                    raise e
                yield mock_packet

            return gen()

        mock_container.demux.side_effect = demux

        with patch('rtsp.client._av') as mock_av, \
             patch('rtsp.client.time.sleep'):
            mock_av.open.return_value = mock_container
            result = _probe_one_frame(0)

        assert result == (1280, 720)

    def test_non_eagain_oserror_breaks_immediately(self):
        mock_container = self._mock_container()
        sleeps = []

        def demux(vs_arg):
            def gen():
                e = OSError()
                e.errno = errno.EIO
                raise e
                yield
            return gen()

        mock_container.demux.side_effect = demux

        with patch('rtsp.client._av') as mock_av, \
             patch('rtsp.client.time.sleep', side_effect=sleeps.append):
            mock_av.open.return_value = mock_container
            result = _probe_one_frame(0)

        assert result == (None, None)
        assert sleeps == []

    def test_generic_exception_breaks_immediately(self):
        mock_container = self._mock_container()

        def demux(vs_arg):
            def gen():
                raise ValueError('unexpected codec error')
                yield
            return gen()

        mock_container.demux.side_effect = demux

        with patch('rtsp.client._av') as mock_av:
            mock_av.open.return_value = mock_container
            result = _probe_one_frame(0)

        assert result == (None, None)


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


# ---------------------------------------------------------------------------
# GET_PARAMETER keep-alive: minimal mock RTSP server
# ---------------------------------------------------------------------------

def _encode_test_h264(n_frames=12, width=160, height=120, fps=5):
    """Encode solid-colour frames with libx264 via PyAV.

    Returns (sps_bytes, pps_bytes, frame_groups) where frame_groups is a list
    of NAL unit lists, one per encoded output packet.
    """
    codec = _av.CodecContext.create('libx264', 'w')
    codec.width = width
    codec.height = height
    codec.pix_fmt = 'yuv420p'
    codec.framerate = Fraction(fps, 1)
    codec.time_base = Fraction(1, fps)
    codec.gop_size = fps
    codec.options = {'preset': 'ultrafast', 'tune': 'zerolatency',
                     'forced-idr': '1', 'profile': 'baseline'}
    codec.open()

    sps = pps = None
    groups = []

    for i in range(n_frames):
        rgb = np.zeros((height, width, 3), dtype=np.uint8)
        rgb[:, :, 0] = (i * 30) % 256
        av_frame = _av.VideoFrame.from_ndarray(rgb, format='rgb24')
        av_frame = av_frame.reformat(format='yuv420p')
        av_frame.pts = i
        for pkt in codec.encode(av_frame):
            raw = bytes(pkt)
            nals, _ = _split_nals(raw + b'\x00\x00\x00\x01')
            group = []
            for nal in nals:
                t = nal[0] & 0x1F
                if t == 7:
                    sps = nal
                elif t == 8:
                    pps = nal
                group.append(nal)
            if group:
                groups.append(group)

    for pkt in codec.encode(None):
        raw = bytes(pkt)
        nals, _ = _split_nals(raw + b'\x00\x00\x00\x01')
        if nals:
            groups.append(nals)

    return sps, pps, groups


class _MockRtspServer(threading.Thread):
    """In-process RTSP server that injects GET_PARAMETER mid-stream.

    After ``inject_after`` frame groups have been sent, one RTSP
    GET_PARAMETER request is written directly into the TCP stream.  The
    client must skip it and continue decoding the subsequent RTP frames.
    """

    _GET_PARAM = (
        'GET_PARAMETER rtsp://127.0.0.1/live RTSP/1.0\r\n'
        'CSeq: 99\r\nSession: mock\r\n\r\n'
    ).encode()

    def __init__(self, sps, pps, frame_groups, fps=5, inject_after=3):
        super().__init__(daemon=True)
        self._sps = sps
        self._pps = pps
        self._groups = frame_groups
        self._fps = fps
        self._inject_after = inject_after

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(('127.0.0.1', 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.injected = threading.Event()
        self.setup_error = None

    @property
    def uri(self):
        return 'rtsp://127.0.0.1:{}/live'.format(self.port)

    def _sdp(self):
        sps64 = base64.b64encode(self._sps).decode()
        pps64 = base64.b64encode(self._pps).decode()
        lines = [
            'v=0',
            'o=- 0 0 IN IP4 127.0.0.1',
            's=mock',
            't=0 0',
            'm=video 0 RTP/AVP 96',
            'c=IN IP4 127.0.0.1',
            'a=rtpmap:96 H264/90000',
            'a=fmtp:96 packetization-mode=1;sprop-parameter-sets={},{}'.format(
                sps64, pps64),
            'a=control:trackID=0',
        ]
        return '\r\n'.join(lines) + '\r\n'

    def _send_response(self, conn, status, headers, body=b''):
        resp = 'RTSP/1.0 {}\r\n'.format(status)
        for k, v in headers.items():
            resp += '{}: {}\r\n'.format(k, v)
        resp += '\r\n'
        conn.sendall(resp.encode() + body)

    def _handshake(self, conn):
        """Handle OPTIONS/DESCRIBE/SETUP/PLAY; return True when PLAY received."""
        buf = b''
        while True:
            if b'\r\n\r\n' not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return False
                buf += chunk
                continue

            end = buf.index(b'\r\n\r\n') + 4
            req = buf[:end].decode(errors='replace')
            buf = buf[end:]

            lines = req.strip().splitlines()
            method = lines[0].split()[0] if lines else ''
            cseq = next(
                (l.split(':', 1)[1].strip() for l in lines[1:]
                 if l.lower().startswith('cseq')), '0')

            if method == 'OPTIONS':
                self._send_response(conn, '200 OK', {
                    'CSeq': cseq,
                    'Public': 'OPTIONS, DESCRIBE, SETUP, TEARDOWN, PLAY',
                })
            elif method == 'DESCRIBE':
                sdp = self._sdp().encode()
                self._send_response(conn, '200 OK', {
                    'CSeq': cseq,
                    'Content-Type': 'application/sdp',
                    'Content-Length': str(len(sdp)),
                    'Content-Base': 'rtsp://127.0.0.1:{}/live'.format(self.port),
                }, sdp)
            elif method == 'SETUP':
                self._send_response(conn, '200 OK', {
                    'CSeq': cseq,
                    'Session': 'mock1234',
                    'Transport': 'RTP/AVP/TCP;unicast;interleaved=0-1',
                })
            elif method == 'PLAY':
                self._send_response(conn, '200 OK', {
                    'CSeq': cseq,
                    'Session': 'mock1234',
                })
                return True

    def _stream(self, conn):
        packetizer = _RTPPacketizer()
        ts = 0
        ts_inc = 90000 // self._fps
        interval = 1.0 / self._fps

        try:
            for i, group in enumerate(self._groups):
                if i == self._inject_after:
                    conn.sendall(self._GET_PARAM)
                    self.injected.set()
                for j, nal in enumerate(group):
                    last = (j == len(group) - 1)
                    for rtp_pkt in packetizer.packetize(nal, ts=ts, last_nal=last):
                        conn.sendall(b'$\x00' + struct.pack('!H', len(rtp_pkt)) + rtp_pkt)
                ts = (ts + ts_inc) & 0xFFFFFFFF
                time.sleep(interval)
            time.sleep(5)  # stay open so client can drain remaining frames
        except OSError:
            pass  # normal client disconnect at end of test

    def run(self):
        conn = None
        try:
            self._srv.settimeout(15)
            conn, _ = self._srv.accept()
            conn.settimeout(30)
            if self._handshake(conn):
                self._stream(conn)
            else:
                self.setup_error = RuntimeError('RTSP handshake did not reach PLAY')
        except Exception as e:
            self.setup_error = e
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            try:
                self._srv.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Tests: GET_PARAMETER keep-alive
# ---------------------------------------------------------------------------

@requires_av
class TestGetParameterKeepAlive:
    """Client silently ignores server-initiated GET_PARAMETER mid-stream.

    RFC 2326 allows servers to send GET_PARAMETER as a keep-alive.  Some
    cameras (e.g. Hikvision) use this instead of OPTIONS.  The client must
    not disconnect or crash when it sees the non-$ bytes.
    """

    @pytest.fixture(scope='class')
    def encoded(self):
        sps, pps, groups = _encode_test_h264(n_frames=12, width=160, height=120, fps=5)
        assert sps and pps, 'pre-encode produced no SPS/PPS'
        return sps, pps, groups

    def test_client_survives_get_parameter(self, encoded):
        sps, pps, groups = encoded
        server = _MockRtspServer(sps, pps, groups, fps=5, inject_after=3)
        server.start()

        with rtsp.Client(server.uri) as client:
            frame = _wait_frame(client, timeout=12)
            assert frame is not None, 'no frame received before GET_PARAMETER injection'

            assert server.injected.wait(timeout=12), 'GET_PARAMETER was never injected'

            time.sleep(0.5)  # let the recv loop process the injection and continue

            assert client._bgt is not None and client._bgt.is_alive(), \
                'recv loop exited after GET_PARAMETER injection'

        assert server.setup_error is None, \
            'mock server error during handshake: {}'.format(server.setup_error)


# ---------------------------------------------------------------------------
# Unit: _enable_verbose
# ---------------------------------------------------------------------------

class TestEnableVerbose:

    def setup_method(self):
        self._logger = logging.getLogger('rtsp')
        self._orig_handlers = self._logger.handlers[:]
        self._orig_level = self._logger.level

    def teardown_method(self):
        self._logger.handlers[:] = self._orig_handlers
        self._logger.setLevel(self._orig_level)

    def test_adds_stream_handler(self):
        _enable_verbose()
        non_null = [h for h in self._logger.handlers
                    if not isinstance(h, logging.NullHandler)]
        assert len(non_null) == 1
        assert isinstance(non_null[0], logging.StreamHandler)

    def test_sets_debug_level(self):
        _enable_verbose()
        assert self._logger.level == logging.DEBUG

    def test_idempotent(self):
        _enable_verbose()
        _enable_verbose()
        non_null = [h for h in self._logger.handlers
                    if not isinstance(h, logging.NullHandler)]
        assert len(non_null) == 1

    def test_skips_handler_if_non_null_already_present(self):
        existing = logging.StreamHandler()
        self._logger.addHandler(existing)
        count_before = len(self._logger.handlers)
        _enable_verbose()
        assert len(self._logger.handlers) == count_before


# ---------------------------------------------------------------------------
# Unit: _device_open_kwargs
# ---------------------------------------------------------------------------

class TestDeviceOpenKwargs:

    def test_both_none_returns_empty_dict(self):
        assert _device_open_kwargs(None, None) == {}

    def test_fmt_only(self):
        assert _device_open_kwargs('v4l2', None) == {'format': 'v4l2'}

    def test_options_only(self):
        assert _device_open_kwargs(None, {'framerate': '30'}) == {'options': {'framerate': '30'}}

    def test_both_provided(self):
        result = _device_open_kwargs('avfoundation', {'framerate': '30'})
        assert result == {'format': 'avfoundation', 'options': {'framerate': '30'}}

    def test_empty_string_fmt_excluded(self):
        assert _device_open_kwargs('', None) == {}

    def test_empty_dict_options_excluded(self):
        assert _device_open_kwargs(None, {}) == {}


# ---------------------------------------------------------------------------
# Unit: _recv_response error codes
# ---------------------------------------------------------------------------

class TestRecvResponse:

    def _reader(self, data: bytes):
        sock = MagicMock()
        sock.recv.return_value = data
        return _client._Reader(sock)

    def _bare(self):
        return object.__new__(_client._RtspClient)

    def test_404_raises_runtime_error(self):
        reader = self._reader(b'RTSP/1.0 404 Not Found\r\n\r\n')
        with pytest.raises(RuntimeError, match='RTSP error'):
            self._bare()._recv_response(reader)

    def test_401_error_message_includes_status_line(self):
        reader = self._reader(
            b'RTSP/1.0 401 Unauthorized\r\nWWW-Authenticate: Basic\r\n\r\n'
        )
        with pytest.raises(RuntimeError, match='401'):
            self._bare()._recv_response(reader)

    def test_301_redirect_accepted(self):
        reader = self._reader(
            b'RTSP/1.0 301 Moved Permanently\r\nLocation: rtsp://new.host/live\r\n\r\n'
        )
        code, headers, _ = self._bare()._recv_response(reader)
        assert code == 301
        assert headers['location'] == 'rtsp://new.host/live'

    def test_302_redirect_accepted(self):
        reader = self._reader(b'RTSP/1.0 302 Found\r\n\r\n')
        code, _, _ = self._bare()._recv_response(reader)
        assert code == 302

    def test_200_ok_accepted(self):
        reader = self._reader(b'RTSP/1.0 200 OK\r\n\r\n')
        code, _, _ = self._bare()._recv_response(reader)
        assert code == 200

    def test_body_empty_when_no_content_length(self):
        reader = self._reader(b'RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n')
        _, _, body = self._bare()._recv_response(reader)
        assert body == b''

    def test_reads_body_by_content_length(self):
        payload = b'v=0\r\nm=video 0 RTP/AVP 96\r\n'
        resp = (
            b'RTSP/1.0 200 OK\r\n'
            b'Content-Type: application/sdp\r\n'
            b'Content-Length: ' + str(len(payload)).encode() + b'\r\n'
            b'\r\n'
        ) + payload
        sock = MagicMock()
        sock.recv.return_value = resp
        reader = _client._Reader(sock)
        _, _, body = self._bare()._recv_response(reader)
        assert body == payload

    def test_headers_accessible_after_body_consumed(self):
        payload = b'v=0\r\n'
        resp = (
            b'RTSP/1.0 200 OK\r\n'
            b'Content-Base: rtsp://127.0.0.1:8554/live/\r\n'
            b'Content-Length: ' + str(len(payload)).encode() + b'\r\n'
            b'\r\n'
        ) + payload
        sock = MagicMock()
        sock.recv.return_value = resp
        reader = _client._Reader(sock)
        _, headers, _ = self._bare()._recv_response(reader)
        assert headers['content-base'] == 'rtsp://127.0.0.1:8554/live/'


# ---------------------------------------------------------------------------
# Unit: _recv_loop RTP header edge cases
# ---------------------------------------------------------------------------

def _interleaved(rtp: bytes, channel: int = 0) -> bytes:
    return b'$' + bytes([channel]) + struct.pack('!H', len(rtp)) + rtp


def _rtp_packet(payload: bytes, *, cc=0, ext=False, padding=False,
                marker=False, ext_words=0) -> bytes:
    b0 = 0x80
    if ext:
        b0 |= 0x10
    if padding:
        b0 |= 0x20
    b0 |= cc & 0x0F
    b1 = 0x60 | (0x80 if marker else 0)
    hdr = bytes([b0, b1]) + b'\x00\x01' + b'\x00\x00\x00\x00' + b'\x00\x00\x00\x01'
    hdr += b'\x00\x00\x00\x00' * cc
    if ext:
        hdr += b'\xAB\xCD' + struct.pack('!H', ext_words)
        hdr += b'\x00\x00\x00\x00' * ext_words
    return hdr + payload


def _run_recv_loop(frames):
    c = object.__new__(_client._RtspClient)
    c._verbose = False
    c._queue = None
    c._bg_run = True
    c._width = None
    c._height = None
    c._lock = threading.Lock()
    data = b''.join(frames)
    sock = MagicMock()
    sock.recv.side_effect = [data, b'']
    c._sock = sock
    c._recv_loop(None, None, b'')
    return c


@requires_av
class TestRtpHeaderEdgeCases:

    def test_wrong_channel_packet_skipped(self):
        rtp = _rtp_packet(b'\x65' + b'\xAB' * 4)
        c = _run_recv_loop([_interleaved(rtp, channel=1)])
        assert c._queue is None

    def test_rtp_shorter_than_12_bytes_skipped(self):
        c = _run_recv_loop([_interleaved(b'\x80\x60\x00\x01')])
        assert c._queue is None

    def test_extension_bit_set_but_packet_too_short_skipped(self):
        rtp = bytes([0x90, 0x60]) + b'\x00\x01' + b'\x00' * 4 + b'\x00' * 4
        assert len(rtp) == 12
        c = _run_recv_loop([_interleaved(rtp)])
        assert c._queue is None

    def test_empty_payload_after_csrc_skipped(self):
        rtp = bytes([0x83, 0x60]) + b'\x00\x01' + b'\x00' * 4 + b'\x00' * 4
        rtp += b'\x00\x00\x00\x00' * 3
        assert len(rtp) == 24
        c = _run_recv_loop([_interleaved(rtp)])
        assert c._queue is None

    def test_padding_stripped_before_demux(self):
        inner = bytes([0x41]) + b'\xAB' * 8
        padding_count = 3
        padded = inner + b'\x00\x00' + bytes([padding_count])
        rtp = _rtp_packet(padded, padding=True)
        c = _run_recv_loop([_interleaved(rtp)])
        assert not c._bg_run

    def test_valid_extension_header_payload_extracted(self):
        # ext_words=2 inserts 8 bytes of extension data before the NAL payload.
        # The client must skip those bytes to find the real NAL type.
        idr = bytes([0x65]) + b'\xAB' * 16  # NAL type 5 (IDR)
        rtp = _rtp_packet(idr, ext=True, ext_words=2, marker=True)
        pkt = _interleaved(rtp)

        mock_codec = MagicMock()
        mock_codec.decode.side_effect = lambda p: iter([])

        with patch('rtsp.client._av') as mock_av:
            mock_av.CodecContext.create.return_value = mock_codec
            mock_av.Packet.side_effect = lambda data: data
            _run_recv_loop([pkt])

        assert mock_codec.decode.called
        annex_b = mock_codec.decode.call_args_list[0].args[0]
        types = {p[0] & 0x1F for p in annex_b.split(b'\x00\x00\x00\x01') if p}
        assert 5 in types  # IDR reached the decoder, not the extension padding bytes


# ---------------------------------------------------------------------------
# Unit: pre-IDR NAL filtering
# ---------------------------------------------------------------------------

class TestPreIdrNalFiltering:
    """Verify the gate at client.py:721-726 that holds NALs until an IDR arrives.

    Before an IDR:
    - SPS (type 7) and PPS (type 8) accumulate and decode normally.
    - IDR (type 5) sets idr_seen and accumulates.
    - Everything else (SEI type 6, AU delimiter type 9, non-IDR slices type 1)
      is silently dropped via ``continue`` and never reaches ``au_nals``.

    After an IDR, all NAL types accumulate unconditionally.
    """

    @staticmethod
    def _run(pkts):
        """Run _recv_loop with a controlled mock codec. Returns (client, mock_codec)."""
        mock_codec = MagicMock()
        mock_codec.decode.side_effect = lambda pkt: iter([])

        with patch('rtsp.client._av') as mock_av:
            mock_av.CodecContext.create.return_value = mock_codec
            mock_av.Packet.side_effect = lambda data: data  # pass bytes through

            c = object.__new__(_client._RtspClient)
            c._verbose = False
            c._queue = None
            c._bg_run = True
            c._width = None
            c._height = None
            c._lock = threading.Lock()
            raw = b''.join(pkts)
            sock = MagicMock()
            sock.recv.side_effect = [raw, b'']
            c._sock = sock
            c._recv_loop(None, None, b'')

        return c, mock_codec

    @staticmethod
    def _decode_calls(mock_codec):
        """Return the list of Annex-B byte buffers passed to codec.decode()."""
        return [call.args[0] for call in mock_codec.decode.call_args_list]

    @staticmethod
    def _nal_types_in(annex_b: bytes):
        """Return the set of NAL unit types found in an Annex-B buffer."""
        types = set()
        for part in annex_b.split(b'\x00\x00\x00\x01'):
            if part:
                types.add(part[0] & 0x1F)
        return types

    def test_sei_before_idr_dropped(self):
        sei = bytes([0x06]) + b'\x05\x04' + b'\x00' * 8   # NAL type 6
        pkt = _interleaved(_rtp_packet(sei, marker=True))
        _, codec = self._run([pkt])
        assert not codec.decode.called

    def test_au_delimiter_before_idr_dropped(self):
        aud = bytes([0x09, 0xF0])                           # NAL type 9
        pkt = _interleaved(_rtp_packet(aud, marker=True))
        _, codec = self._run([pkt])
        assert not codec.decode.called

    def test_non_idr_slice_before_idr_dropped(self):
        slice_nal = bytes([0x41]) + b'\xAB' * 8            # NAL type 1
        pkt = _interleaved(_rtp_packet(slice_nal, marker=True))
        _, codec = self._run([pkt])
        assert not codec.decode.called

    def test_multiple_dropped_types_before_idr(self):
        pkt1 = _interleaved(_rtp_packet(bytes([0x09, 0xF0])))              # type 9
        pkt2 = _interleaved(_rtp_packet(bytes([0x06]) + b'\x00' * 4))     # type 6
        pkt3 = _interleaved(_rtp_packet(bytes([0x41]) + b'\xAB' * 4, marker=True))  # type 1
        _, codec = self._run([pkt1, pkt2, pkt3])
        assert not codec.decode.called

    def test_sps_pps_before_idr_pass_through(self):
        sps = bytes([0x67]) + b'\x42\xc0\x1e' + b'\x00' * 4   # NAL type 7
        pps = bytes([0x68]) + b'\xce\x38\x80'                  # NAL type 8
        pkt = _interleaved(_rtp_packet(_stap_a([sps, pps]), marker=True))
        _, codec = self._run([pkt])
        assert len(self._decode_calls(codec)) == 1
        types = self._nal_types_in(self._decode_calls(codec)[0])
        assert 7 in types
        assert 8 in types

    def test_idr_opens_gate_and_is_decoded(self):
        idr = bytes([0x65]) + b'\xAB' * 16                     # NAL type 5
        pkt = _interleaved(_rtp_packet(idr, marker=True))
        _, codec = self._run([pkt])
        assert codec.decode.called
        assert 5 in self._nal_types_in(self._decode_calls(codec)[0])

    def test_sei_after_idr_passes_through(self):
        idr = bytes([0x65]) + b'\xAB' * 16
        sei = bytes([0x06]) + b'\x00' * 8
        pkt_idr = _interleaved(_rtp_packet(idr, marker=True))
        pkt_sei = _interleaved(_rtp_packet(sei, marker=True))
        _, codec = self._run([pkt_idr, pkt_sei])
        calls = self._decode_calls(codec)
        assert len(calls) == 2
        assert 6 in self._nal_types_in(calls[1])

    def test_non_idr_slice_after_idr_passes_through(self):
        idr = bytes([0x65]) + b'\xAB' * 16
        slice_nal = bytes([0x41]) + b'\xAB' * 8
        pkt_idr = _interleaved(_rtp_packet(idr, marker=True))
        pkt_slice = _interleaved(_rtp_packet(slice_nal, marker=True))
        _, codec = self._run([pkt_idr, pkt_slice])
        calls = self._decode_calls(codec)
        assert len(calls) == 2
        assert 1 in self._nal_types_in(calls[1])


# ---------------------------------------------------------------------------
# Unit: Client factory routing
# ---------------------------------------------------------------------------

class TestClientFactory:

    def test_rtmp_uri_returns_rtmp_client(self):
        from rtsp.rtmp import RTMPClient
        with patch.object(RTMPClient, '__init__', return_value=None):
            result = Client('rtmp://127.0.0.1:1935/live')
        assert isinstance(result, RTMPClient)

    def test_rtmps_uri_returns_rtmp_client(self):
        from rtsp.rtmp import RTMPClient
        with patch.object(RTMPClient, '__init__', return_value=None):
            result = Client('rtmps://127.0.0.1:1935/live')
        assert isinstance(result, RTMPClient)

    def test_rtsp_uri_returns_rtsp_client(self):
        with patch.object(_client._RtspClient, '__init__', return_value=None):
            result = Client('rtsp://127.0.0.1:8554/live')
        assert isinstance(result, _client._RtspClient)

    def test_http_uri_returns_rtsp_client(self):
        with patch.object(_client._RtspClient, '__init__', return_value=None):
            result = Client('http://127.0.0.1:8080/stream')
        assert isinstance(result, _client._RtspClient)


# ---------------------------------------------------------------------------
# Unit: _RtspClient.__init__ device and http/tcp branches
# ---------------------------------------------------------------------------

class TestRtspClientInit:

    def _make(self, uri):
        with patch('rtsp.client._av', MagicMock()), \
             patch('rtsp.client._local_device_args', return_value=('/dev/video0', 'v4l2', {})), \
             patch.object(_client._RtspClient, 'open'):
            return _client._RtspClient(uri)

    def test_int_index_is_local(self):
        c = self._make(0)
        assert c._is_local
        assert c._av_open_args == ('/dev/video0',)

    def test_string_digit_is_local(self):
        c = self._make('0')
        assert c._is_local
        assert c._av_open_args == ('/dev/video0',)

    def test_http_scheme_is_local(self):
        with patch('rtsp.client._av', MagicMock()), \
             patch.object(_client._RtspClient, 'open'):
            c = _client._RtspClient('http://192.168.1.1/stream')
        assert c._is_local
        assert c._av_open_args == ('http://192.168.1.1/stream',)

    def test_tcp_scheme_is_local(self):
        with patch('rtsp.client._av', MagicMock()), \
             patch.object(_client._RtspClient, 'open'):
            c = _client._RtspClient('tcp://192.168.1.1:8554')
        assert c._is_local


# ---------------------------------------------------------------------------
# Unit: _device_loop (local camera path)
# ---------------------------------------------------------------------------

def _make_device_client(verbose=False):
    c = object.__new__(_client._RtspClient)
    c._verbose = verbose
    c._queue = None
    c._bg_run = True
    c._width = None
    c._height = None
    c._lock = threading.Lock()
    c._av_container = None
    c._av_open_args = ('0',)
    c._av_open_kwargs = {'format': 'v4l2'}
    return c


class TestDeviceLoop:

    def _run(self, mock_av, c=None, verbose=False):
        if c is None:
            c = _make_device_client(verbose=verbose)
        ready = threading.Event()
        err = []
        with patch('rtsp.client._av', mock_av):
            c._device_loop(ready, err)
        return c, ready, err

    def test_open_error_sets_err_and_ready(self):
        mock_av = MagicMock()
        mock_av.open.side_effect = OSError('device not found')
        _, ready, err = self._run(mock_av)
        assert ready.is_set()
        assert len(err) == 1
        assert 'device not found' in str(err[0])

    def test_no_video_stream_sets_err(self):
        mock_av = MagicMock()
        mock_container = MagicMock()
        mock_container.streams = []
        mock_av.open.return_value = mock_container
        _, ready, err = self._run(mock_av)
        assert ready.is_set()
        assert len(err) == 1

    def test_happy_path_stores_frame(self):
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        mock_frame = MagicMock()
        mock_frame.to_ndarray.return_value = arr
        mock_packet = MagicMock()
        mock_packet.decode.return_value = [mock_frame]
        mock_stream = MagicMock()
        mock_stream.type = 'video'
        mock_container = MagicMock()
        mock_container.streams = [mock_stream]
        mock_container.demux.return_value = [mock_packet]
        mock_av = MagicMock()
        mock_av.open.return_value = mock_container
        c, ready, err = self._run(mock_av)
        assert err == []
        assert ready.is_set()
        assert c._queue is not None

    def test_verbose_logs_resolution(self):
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        mock_frame = MagicMock()
        mock_frame.to_ndarray.return_value = arr
        mock_packet = MagicMock()
        mock_packet.decode.return_value = [mock_frame]
        mock_stream = MagicMock()
        mock_stream.type = 'video'
        mock_container = MagicMock()
        mock_container.streams = [mock_stream]
        mock_container.demux.return_value = [mock_packet]
        mock_av = MagicMock()
        mock_av.open.return_value = mock_container
        c, _, err = self._run(mock_av, verbose=True)
        assert err == []
        assert c._width == 160
        assert c._height == 120

    def test_eagain_retries(self):
        calls = []
        def demux_side_effect(stream):
            if not calls:
                calls.append(1)
                raise OSError(errno.EAGAIN, 'Resource temporarily unavailable')
            return iter([])
        mock_stream = MagicMock()
        mock_stream.type = 'video'
        mock_container = MagicMock()
        mock_container.streams = [mock_stream]
        mock_container.demux.side_effect = demux_side_effect
        mock_av = MagicMock()
        mock_av.open.return_value = mock_container
        with patch('rtsp.client.time.sleep'):
            c, ready, err = self._run(mock_av)
        assert err == []
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Unit: _open_local error path and close() device branch
# ---------------------------------------------------------------------------

def _make_local_client():
    """Return a _RtspClient configured as a local device without calling open()."""
    c = object.__new__(_client._RtspClient)
    c._verbose = False
    c._queue = None
    c._bg_run = False
    c._width = None
    c._height = None
    c._lock = threading.Lock()
    c._bgt = None
    c._av_container = None
    c._is_local = True
    c._av_open_args = ('0',)
    c._av_open_kwargs = {}
    return c


class TestOpenLocalAndClose:

    def test_open_local_raises_on_device_error(self):
        c = _make_local_client()
        mock_av = MagicMock()
        mock_av.open.side_effect = OSError('no device')
        with patch('rtsp.client._av', mock_av):
            with pytest.raises(RuntimeError, match='no device'):
                c.open()

    def test_open_is_idempotent(self):
        c = _make_local_client()
        mock_av = MagicMock()
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.type = 'video'
        mock_container.streams = [mock_stream]
        mock_container.demux.return_value = iter([])
        mock_av.open.return_value = mock_container
        with patch('rtsp.client._av', mock_av):
            c.open()
            result = c.open()
        assert result is c

    def test_close_shuts_down_device_container(self):
        c = _make_local_client()
        mock_container = MagicMock()
        c._av_container = mock_container
        c.close()
        mock_container.close.assert_called_once()

    def test_close_ignores_container_exception(self):
        c = _make_local_client()
        mock_container = MagicMock()
        mock_container.close.side_effect = Exception('already closed')
        c._av_container = mock_container
        c.close()  # must not raise


# ---------------------------------------------------------------------------
# Unit: preview() — tkinter mocked so tests run without a display
# ---------------------------------------------------------------------------

def _preview_client(frame=None):
    """Return a _RtspClient whose read() yields frame (or None)."""
    c = _make_local_client()
    c._bg_run = True
    c.read = MagicMock(return_value=frame)
    c.close = MagicMock()
    return c


def _run_preview(c):
    """Run c.preview() with tkinter and PIL.ImageTk fully mocked."""
    mock_tk = MagicMock()
    mock_imagetk = MagicMock()
    with patch.dict('sys.modules', {'tkinter': mock_tk, 'PIL.ImageTk': mock_imagetk}):
        c.preview()
    return mock_tk, mock_imagetk


class TestPreview:

    def test_mainloop_is_called(self):
        c = _preview_client()
        mock_tk, _ = _run_preview(c)
        mock_tk.Tk.return_value.mainloop.assert_called_once()

    def test_close_called_after_mainloop(self):
        c = _preview_client()
        _run_preview(c)
        c.close.assert_called_once()

    def test_tick_with_frame_updates_label(self):
        from PIL import Image
        frame = Image.new('RGB', (4, 4))
        c = _preview_client(frame=frame)
        mock_tk, mock_imagetk = _run_preview(c)
        mock_imagetk.PhotoImage.assert_called_once_with(frame)

    def test_tick_when_not_running_destroys_root(self):
        c = _preview_client()
        c._bg_run = False
        mock_tk, _ = _run_preview(c)
        mock_tk.Tk.return_value.destroy.assert_called()

    def test_stop_via_delete_window_sets_bg_run_false(self):
        c = _preview_client()
        mock_tk, _ = _run_preview(c)
        mock_root = mock_tk.Tk.return_value
        _stop = mock_root.protocol.call_args[0][1]
        c._bg_run = True
        _stop()
        assert c._bg_run is False

    def test_stop_cancels_pending_after(self):
        c = _preview_client()
        mock_tk, _ = _run_preview(c)
        mock_root = mock_tk.Tk.return_value
        _stop = mock_root.protocol.call_args[0][1]
        # simulate a pending after_id
        _stop.__closure__  # just access it; after_id is set by _tick
        _stop()  # must not raise even with after_id set

    def test_q_key_stops_preview(self):
        c = _preview_client()
        mock_tk, _ = _run_preview(c)
        mock_root = mock_tk.Tk.return_value
        key_handler = mock_root.bind_all.call_args[0][1]
        event = MagicMock()
        event.keysym = 'q'
        c._bg_run = True
        key_handler(event)
        assert c._bg_run is False

    def test_non_quit_key_does_not_stop(self):
        c = _preview_client()
        mock_tk, _ = _run_preview(c)
        mock_root = mock_tk.Tk.return_value
        key_handler = mock_root.bind_all.call_args[0][1]
        event = MagicMock()
        event.keysym = 'a'
        c._bg_run = True
        key_handler(event)
        assert c._bg_run is True


# ---------------------------------------------------------------------------
# Unit: _recv_loop verbose resolution log
# ---------------------------------------------------------------------------

@requires_av
class TestRecvLoopVerbose:

    def test_verbose_logs_resolution_on_first_frame(self):
        nal = b'\x65' + b'\x00' * 10
        rtp = _rtp_packet(nal, marker=True)
        frame = _interleaved(rtp)
        c = _run_recv_loop([frame])
        c._verbose = True
        c._width = None
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        mock_frame = MagicMock()
        mock_frame.to_ndarray.return_value = arr
        mock_codec = MagicMock()
        mock_codec.decode.return_value = [mock_frame]
        with patch('rtsp.client._av') as mock_av:
            mock_av.CodecContext.create.return_value = mock_codec
            sock = MagicMock()
            sock.recv.side_effect = [_interleaved(_rtp_packet(b'\x65' + b'\x00' * 4, marker=True)), b'']
            c._sock = sock
            c._bg_run = True
            c._recv_loop(None, None, b'')


# ---------------------------------------------------------------------------
# Unit: _RtspClient raises ImportError when _av is None
# ---------------------------------------------------------------------------

class TestRtspClientNoAV:

    def test_raises_import_error_when_av_missing(self):
        with patch('rtsp.client._av', None):
            with pytest.raises(ImportError, match='PyAV'):
                _client._RtspClient('rtsp://127.0.0.1/live')


# ---------------------------------------------------------------------------
# Unit: _Reader EOF paths
# ---------------------------------------------------------------------------

class TestReaderEOF:

    def test_read_until_raises_on_eof(self):
        sock = MagicMock()
        sock.recv.return_value = b''
        r = _client._Reader(sock)
        with pytest.raises(ConnectionError):
            r.read_until(b'\r\n\r\n')

    def test_read_exact_raises_on_eof(self):
        sock = MagicMock()
        sock.recv.return_value = b''
        r = _client._Reader(sock)
        with pytest.raises(ConnectionError):
            r.read_exact(10)


# ---------------------------------------------------------------------------
# Unit: _local_device_args platform branches
# ---------------------------------------------------------------------------

class TestLocalDeviceArgsPlatform:

    def test_linux_branch(self):
        with patch('rtsp.client._platform') as mock_plat:
            mock_plat.system.return_value = 'Linux'
            device_str, fmt, opts = _client._local_device_args(1)
        assert device_str == '/dev/video1'
        assert fmt == 'v4l2'
        assert opts == {}

    def test_windows_branch(self):
        with patch('rtsp.client._platform') as mock_plat:
            mock_plat.system.return_value = 'Windows'
            device_str, fmt, opts = _client._local_device_args(0)
        assert device_str == '0'
        assert fmt == 'dshow'

    def test_unknown_platform_branch(self):
        with patch('rtsp.client._platform') as mock_plat:
            mock_plat.system.return_value = 'FreeBSD'
            device_str, fmt, opts = _client._local_device_args(2)
        assert device_str == '2'
        assert fmt is None


# ---------------------------------------------------------------------------
# Unit: _linux_camera_names
# ---------------------------------------------------------------------------

class TestLinuxCameraNames:

    def test_returns_names_from_sysfs(self):
        from pathlib import Path
        paths_that_exist = {
            '/dev/video0': True,
            '/dev/video1': True,
            '/dev/video2': False,
            '/sys/class/video4linux/video0/name': True,
            '/sys/class/video4linux/video1/name': False,
        }
        name_texts = {
            '/sys/class/video4linux/video0/name': 'USB Camera\n',
        }

        def _fake_exists(self):
            return paths_that_exist.get(str(self), False)

        def _fake_read_text(self):
            return name_texts[str(self)]

        with patch.object(Path, 'exists', _fake_exists), \
             patch.object(Path, 'read_text', _fake_read_text):
            result = _client._linux_camera_names()

        assert result == ['USB Camera', 'Camera 1']

    def test_returns_none_when_no_devices(self):
        from pathlib import Path
        with patch.object(Path, 'exists', lambda self: False):
            result = _client._linux_camera_names()
        assert result is None


# ---------------------------------------------------------------------------
# Unit: _platform_device_names Linux branch
# ---------------------------------------------------------------------------

class TestPlatformDeviceNamesLinux:

    def test_dispatches_to_linux(self):
        with patch('rtsp.client._platform') as mock_plat, \
             patch('rtsp.client._linux_camera_names', return_value=['Cam0']) as mock_linux:
            mock_plat.system.return_value = 'Linux'
            result = _client._platform_device_names()
        mock_linux.assert_called_once()
        assert result == ['Cam0']


# ---------------------------------------------------------------------------
# Unit: _probe_one_frame paths
# ---------------------------------------------------------------------------

class TestProbeOneFrameExtra:

    def test_returns_none_none_when_av_missing(self):
        with patch('rtsp.client._av', None):
            w, h = _client._probe_one_frame(0)
        assert w is None and h is None

    def test_returns_none_none_when_no_video_stream(self):
        mock_av = MagicMock()
        container = MagicMock()
        container.streams = []
        mock_av.open.return_value = container
        with patch('rtsp.client._av', mock_av), \
             patch('rtsp.client._local_device_args', return_value=('0', None, {})):
            w, h = _client._probe_one_frame(0)
        assert w is None and h is None

    def test_container_close_exception_is_swallowed(self):
        mock_av = MagicMock()
        container = MagicMock()
        container.streams = []
        container.close.side_effect = RuntimeError('boom')
        mock_av.open.return_value = container
        with patch('rtsp.client._av', mock_av), \
             patch('rtsp.client._local_device_args', return_value=('0', None, {})):
            w, h = _client._probe_one_frame(0)
        assert w is None and h is None


# ---------------------------------------------------------------------------
# Unit: _open_rtsp ConnectionRefusedError backoff + exception cleanup
# ---------------------------------------------------------------------------

class TestOpenRtspErrors:

    def _make_rtsp_client_no_open(self):
        """Create an _RtspClient stub with open() patched out."""
        with patch('rtsp.client._RtspClient.open'):
            c = _client._RtspClient.__new__(_client._RtspClient)
        c._verbose = False
        c._is_local = False
        c._host = '127.0.0.1'
        c._port = 9
        c._uri = 'rtsp://127.0.0.1:9/live'
        c._sock = None
        c._session_id = None
        c._cseq = 0
        c._bg_run = False
        c._bgt = None
        c._queue = None
        c._width = None
        c._height = None
        import threading
        c._lock = threading.Lock()
        c._av_container = None
        return c

    def test_connection_refused_raises_after_timeout(self):
        c = self._make_rtsp_client_no_open()
        with patch('rtsp.client.socket.create_connection',
                   side_effect=ConnectionRefusedError), \
             patch('rtsp.client.time.monotonic', side_effect=[0.0, 100.0, 100.0]), \
             patch('rtsp.client.time.sleep'):
            with pytest.raises(ConnectionRefusedError):
                c._open_rtsp()

    def test_handshake_exception_cleans_up_socket(self):
        c = self._make_rtsp_client_no_open()
        mock_sock = MagicMock()
        with patch('rtsp.client.socket.create_connection', return_value=mock_sock), \
             patch.object(c, '_rtsp_options', side_effect=RuntimeError('handshake fail')):
            with pytest.raises(RuntimeError):
                c._open_rtsp()
        assert c._sock is None
        mock_sock.close.assert_called_once()

    def test_handshake_exception_swallows_sock_close_oserror(self):
        c = self._make_rtsp_client_no_open()
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError('already closed')
        with patch('rtsp.client.socket.create_connection', return_value=mock_sock), \
             patch.object(c, '_rtsp_options', side_effect=RuntimeError('handshake fail')):
            with pytest.raises(RuntimeError):
                c._open_rtsp()
        assert c._sock is None

    def test_verbose_logs_connected(self):
        c = self._make_rtsp_client_no_open()
        c._verbose = True
        mock_sock = MagicMock()
        with patch('rtsp.client.socket.create_connection', return_value=mock_sock), \
             patch.object(c, '_rtsp_options'), \
             patch.object(c, '_rtsp_describe', return_value=('', 'rtsp://127.0.0.1:9/live')), \
             patch('rtsp.client._parse_sdp', return_value=(None, None, None)), \
             patch.object(c, '_rtsp_setup'), \
             patch.object(c, '_rtsp_play'), \
             patch('rtsp.client.threading.Thread') as mock_thread:
            mock_thread.return_value.start = MagicMock()
            import logging
            with patch('rtsp.client.log') as mock_log:
                c._open_rtsp()
        mock_log.info.assert_any_call('connected to %s', c._uri)


# ---------------------------------------------------------------------------
# Unit: close() swallows OSError from sock.close()
# ---------------------------------------------------------------------------

class TestCloseSwallowsOSError:

    def test_sock_close_oserror_is_swallowed(self):
        with patch('rtsp.client._RtspClient.open'):
            c = _client._RtspClient.__new__(_client._RtspClient)
        c._is_local = False
        c._bg_run = True
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError('already gone')
        c._sock = mock_sock
        c._av_container = None
        c._bgt = None
        c._session_id = None
        c.close()
        assert c._sock is None


# ---------------------------------------------------------------------------
# Unit: _recv_loop OSError exits cleanly
# ---------------------------------------------------------------------------

class TestRecvLoopOSError:

    def test_oserror_on_recv_exits_loop(self):
        mock_av = MagicMock()
        mock_codec = MagicMock()
        mock_codec.decode.return_value = []
        mock_av.CodecContext.create.return_value = mock_codec

        sock = MagicMock()
        sock.recv.side_effect = OSError('connection reset')

        with patch('rtsp.client._RtspClient.open'):
            c = _client._RtspClient.__new__(_client._RtspClient)
        c._bg_run = True
        c._sock = sock
        c._queue = None
        c._width = None
        c._height = None
        c._verbose = False
        import threading
        c._lock = threading.Lock()

        with patch('rtsp.client._av', mock_av):
            c._recv_loop(None, None, b'')

        assert c._bg_run is False


# ---------------------------------------------------------------------------
# Unit: _decode_access_unit verbose resolution log
# ---------------------------------------------------------------------------

class TestDecodeAccessUnitVerbose:

    def test_logs_resolution_on_first_frame(self):
        mock_av = MagicMock()
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
        mock_frame = MagicMock()
        mock_frame.to_ndarray.return_value = arr
        mock_codec = MagicMock()
        mock_codec.decode.return_value = [mock_frame]
        mock_av.Packet.return_value = MagicMock()

        with patch('rtsp.client._RtspClient.open'):
            c = _client._RtspClient.__new__(_client._RtspClient)
        c._verbose = True
        c._queue = None
        c._width = None
        c._height = None
        import threading
        c._lock = threading.Lock()

        with patch('rtsp.client._av', mock_av), \
             patch('rtsp.client.log') as mock_log:
            c._decode_access_unit(mock_codec, b'\x00\x00\x00\x01\x65')

        mock_log.info.assert_any_call('stream resolution: %dx%d', 160, 120)
        assert c._width == 160
        assert c._height == 120
