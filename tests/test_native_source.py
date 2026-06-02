"""Tests for rtsp.source.Source - Python-native RTSP/RTP server.

Integration tests that do real encoding and RTSP negotiation are marked
requires_ffmpeg and requires_nouveau; they run real FFmpeg for encoding and
connect via rtsp.Client over the loopback interface.
"""

import socket
import threading
import time

import pytest
from PIL import Image

import rtsp
from rtsp.source import Source, _RTPPacketizer, _split_nals

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


# ---------------------------------------------------------------------------
# Unit: NAL splitter
# ---------------------------------------------------------------------------

class TestSplitNals:

    def _annex_b(self, *payloads):
        """Join payloads with 4-byte start codes."""
        out = b''
        for p in payloads:
            out += b'\x00\x00\x00\x01' + p
        return out

    def test_two_nals_extracts_first(self):
        data = self._annex_b(b'\x67hello', b'\x68world')
        nals, rem = _split_nals(data)
        assert len(nals) == 1
        assert nals[0] == b'\x67hello'

    def test_remainder_starts_at_last_start_code(self):
        data = self._annex_b(b'\x67hello', b'\x68world')
        _, rem = _split_nals(data)
        assert rem.startswith(b'\x00\x00\x00\x01')

    def test_single_nal_returns_empty(self):
        data = self._annex_b(b'\x67payload')
        nals, rem = _split_nals(data)
        assert nals == []
        assert rem == data

    def test_three_byte_start_code_accepted(self):
        data = b'\x00\x00\x01' + b'\x67abc' + b'\x00\x00\x00\x01' + b'\x68def'
        nals, rem = _split_nals(data)
        assert len(nals) == 1
        assert nals[0] == b'\x67abc'


# ---------------------------------------------------------------------------
# Unit: RTP packetizer
# ---------------------------------------------------------------------------

class TestRTPPacketizer:

    def test_small_nal_is_single_packet(self):
        p = _RTPPacketizer()
        pkts = p.packetize(b'\x65' + b'\xAB' * 100, ts=1000, last_nal=True)
        assert len(pkts) == 1

    def test_large_nal_is_fragmented(self):
        p = _RTPPacketizer()
        payload = b'\x65' + b'\xAB' * 2000
        pkts = p.packetize(payload, ts=1000, last_nal=True)
        assert len(pkts) > 1

    def test_rtp_header_version(self):
        p = _RTPPacketizer()
        pkt = p.packetize(b'\x65' + b'\x00' * 10, ts=0, last_nal=True)[0]
        assert pkt[0] == 0x80   # V=2, P=0, X=0, CC=0

    def test_marker_bit_set_on_last_packet(self):
        p = _RTPPacketizer()
        payload = b'\x65' + b'\xAB' * 2000
        pkts = p.packetize(payload, ts=0, last_nal=True)
        # last packet: M bit = 0x80 in byte 1
        assert pkts[-1][1] & 0x80

    def test_marker_bit_clear_on_non_last(self):
        p = _RTPPacketizer()
        payload = b'\x65' + b'\xAB' * 2000
        pkts = p.packetize(payload, ts=0, last_nal=True)
        for pkt in pkts[:-1]:
            assert not (pkt[1] & 0x80)

    def test_sequence_numbers_increment(self):
        p = _RTPPacketizer()
        seqs = []
        for _ in range(5):
            pkt = p.packetize(b'\x65' + b'\x00' * 10, ts=0, last_nal=True)[0]
            seqs.append(int.from_bytes(pkt[2:4], 'big'))
        assert seqs == sorted(set(seqs))
        assert all(seqs[i+1] - seqs[i] == 1 for i in range(len(seqs)-1))

    def test_fu_a_indicator_type_28(self):
        p = _RTPPacketizer()
        payload = b'\x65' + b'\xAB' * 2000
        pkts = p.packetize(payload, ts=0, last_nal=True)
        # FU-A indicator: low 5 bits = 28
        fu_ind = pkts[0][12]
        assert fu_ind & 0x1F == 28

    def test_fu_a_start_bit_first_packet(self):
        p = _RTPPacketizer()
        pkts = p.packetize(b'\x65' + b'\xAB' * 2000, ts=0, last_nal=True)
        fu_hdr_first = pkts[0][13]
        assert fu_hdr_first & 0x80   # S bit

    def test_fu_a_end_bit_last_packet(self):
        p = _RTPPacketizer()
        pkts = p.packetize(b'\x65' + b'\xAB' * 2000, ts=0, last_nal=True)
        fu_hdr_last = pkts[-1][13]
        assert fu_hdr_last & 0x40   # E bit


# ---------------------------------------------------------------------------
# Unit: Source construction and URI
# ---------------------------------------------------------------------------

class TestSourceConstruct:

    def test_client_uri_is_rtsp(self):
        src = Source('rtsp://0.0.0.0:19600/live')
        assert src.client_uri.startswith('rtsp://')
        assert '127.0.0.1' in src.client_uri

    def test_client_uri_uses_configured_port(self):
        src = Source('rtsp://0.0.0.0:19601/live')
        assert ':19601' in src.client_uri

    def test_size_snaps_to_even(self):
        src = Source('rtsp://0.0.0.0:19602/live', size=(641, 479))
        assert src._size == (640, 478)

    def test_not_opened_before_first_frame_without_size(self):
        src = Source('rtsp://0.0.0.0:19603/live')
        assert not src.isOpened()

    def test_opened_immediately_when_size_given(self):
        port = _free_port()
        src = Source('rtsp://0.0.0.0:{}/live'.format(port), size=(64, 64))
        assert src.isOpened()
        src.close()


# ---------------------------------------------------------------------------
# Integration: real encoding + RTSP negotiation via rtsp.Client
# ---------------------------------------------------------------------------

@requires_ffmpeg
@requires_nouveau
class TestSourceStreaming:
    """End-to-end tests: Source encodes nouveau images, Client decodes."""

    @pytest.fixture(scope='class')
    def source_and_uri(self, nouveau_frames):
        port = _free_port()
        uri = 'rtsp://0.0.0.0:{}/live'.format(port)
        src = Source(uri, fps=5, size=(160, 200),
                           frame_buffer=nouveau_frames)
        src.wait_ready(timeout=10)
        yield src, src.client_uri
        src.close()

    def test_client_receives_frame(self, source_and_uri):
        src, uri = source_and_uri
        with rtsp.Client(uri) as client:
            frame = _wait_frame(client)
        assert frame is not None

    def test_frame_dimensions_match_size(self, source_and_uri):
        src, uri = source_and_uri
        with rtsp.Client(uri) as client:
            frame = _wait_frame(client)
        assert frame is not None
        assert frame.size == (160, 200)

    def test_two_simultaneous_clients(self, source_and_uri):
        src, uri = source_and_uri
        frames = [None, None]

        def _connect(idx):
            with rtsp.Client(uri) as c:
                frames[idx] = _wait_frame(c)

        t0 = threading.Thread(target=_connect, args=(0,))
        t1 = threading.Thread(target=_connect, args=(1,))
        t0.start(); t1.start()
        t0.join(timeout=15); t1.join(timeout=15)

        assert frames[0] is not None, 'client 0 got no frame'
        assert frames[1] is not None, 'client 1 got no frame'

    def test_sdp_contains_h264(self, source_and_uri):
        """SDP should advertise H264/90000 after encoding has started."""
        src, _ = source_and_uri
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if src._sps and src._pps:
                break
            time.sleep(0.05)
        sdp = src._sdp()
        assert 'H264/90000' in sdp
        assert 'a=rtpmap:96' in sdp

    def test_sdp_includes_sprop_after_first_frame(self, source_and_uri):
        src, uri = source_and_uri
        # Connect a client so encoding actually starts and SPS/PPS flow
        with rtsp.Client(uri) as client:
            _wait_frame(client)
        sdp = src._sdp()
        assert 'sprop-parameter-sets=' in sdp


@requires_ffmpeg
@requires_nouveau
class TestSourceWithNouveau:
    """Verify Source loads and streams real nouveau images."""

    def test_context_manager_opens_and_closes(self, nouveau_frames):
        port = _free_port()
        with Source('rtsp://0.0.0.0:{}/live'.format(port),
                          fps=2, size=(80, 100),
                          frame_buffer=nouveau_frames) as src:
            assert src.isOpened()
            assert src.wait_ready(timeout=10)
        assert not src.isOpened()

    def test_all_frames_loaded_into_buffer(self, nouveau_frames):
        port = _free_port()
        src = Source('rtsp://0.0.0.0:{}/live'.format(port),
                           fps=2, size=(80, 100),
                           frame_buffer=nouveau_frames)
        if src._loader:
            src._loader.join(timeout=10)
        assert len(src._buffer) == len(nouveau_frames)
        src.close()

    def test_single_frame_accepted(self, nouveau_frame):
        port = _free_port()
        src = Source('rtsp://0.0.0.0:{}/live'.format(port),
                           fps=5, frame_buffer=[nouveau_frame])
        src.wait_ready(timeout=10)
        assert len(src._buffer) == 1
        src.close()
