import asyncio
import socket
import struct
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

import rtsp
from rtsp import Source
from rtsp._utils import _to_pil
from rtsp.source import Publisher, _RTPPacketizer, _Session, _split_nals

from conftest import requires_ffmpeg, requires_nouveau

requires_av = pytest.mark.skipif(
    __import__('importlib').util.find_spec('av') is None,
    reason='PyAV (av) not installed',
)


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


@pytest.fixture
def no_open(monkeypatch):
    """Prevent Publisher.open() from connecting to a relay."""
    monkeypatch.setattr(Publisher, 'open', lambda self: self)


# --- _to_pil helper ---

class TestToPil:

    def test_pil_passthrough(self):
        img = Image.new('RGB', (64, 64), color=(255, 0, 0))
        assert _to_pil(img) is img

    def test_numpy_array(self):
        arr = np.zeros((32, 64, 3), dtype=np.uint8)
        result = _to_pil(arr)
        assert isinstance(result, Image.Image)
        assert result.size == (64, 32)  # PIL size is (width, height)

    def test_str_path(self, tmp_path):
        img = Image.new('RGB', (16, 16), color=(0, 128, 255))
        p = tmp_path / 'frame.png'
        img.save(str(p))
        result = _to_pil(str(p))
        assert isinstance(result, Image.Image)
        assert result.size == (16, 16)

    def test_pathlib_path(self, tmp_path):
        img = Image.new('RGB', (16, 16), color=(0, 128, 255))
        p = tmp_path / 'frame.png'
        img.save(p)
        result = _to_pil(p)
        assert isinstance(result, Image.Image)
        assert result.size == (16, 16)

    def test_invalid_type_raises(self):
        with pytest.raises(TypeError):
            _to_pil(42)


# --- Source URI validation ---

class TestSourceUri:

    def test_device_uri_raises(self):
        with pytest.raises(ValueError, match="network address"):
            Source(0, serve=False)

    def test_numeric_string_raises(self):
        with pytest.raises(ValueError, match="network address"):
            Source('0', serve=False)

    def test_valid_rtsp_uri(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        assert src._uri == 'rtsp://localhost:8554/live'

    def test_bare_host_defaults_to_rtsp(self):
        src = Source('localhost:8554/live', serve=False)
        assert src._uri.startswith('rtsp://')


# --- Source size parameter ---

class TestSourceSize:

    def test_size_sets_resolution(self, no_open):
        src = Source('rtsp://0.0.0.0:8554/live', size=(640, 480), serve=False)
        assert src._size == (640, 480)

    def test_size_snaps_odd_dimensions_to_even(self, no_open):
        src = Source('rtsp://0.0.0.0:8554/live', size=(641, 479), serve=False)
        assert src._size == (640, 478)

    def test_size_calls_open_immediately(self, no_open):
        called = []
        with patch.object(Publisher, 'open', lambda self: called.append(1) or self):
            src = Source('rtsp://0.0.0.0:8554/live', size=(320, 240), serve=False)
        assert called

    def test_size_resizes_frames_on_put(self, no_open):
        src = Source('rtsp://0.0.0.0:8554/live', size=(320, 240), serve=False)
        src.put(Image.new('RGB', (1920, 1080)))
        assert src._buffer[0].size == (320, 240)


# --- Source serve mode ---

class TestSourceServe:

    def test_serve_true_returns_source_impl(self):
        import rtsp.source as _src
        src = Source('rtsp://0.0.0.0:8554/live')
        assert isinstance(src, _src.Source)

    def test_serve_true_explicit_returns_source_impl(self):
        import rtsp.source as _src
        src = Source('rtsp://0.0.0.0:8554/live', serve=True)
        assert isinstance(src, _src.Source)

    def test_serve_false_returns_publisher(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        assert isinstance(src, Publisher)

    def test_serve_false_not_source_impl(self):
        import rtsp.source as _src
        src = Source('rtsp://localhost:8554/live', serve=False)
        assert not isinstance(src, _src.Source)


# --- Source frame rate ---

class TestSourceFrameRate:

    def test_fps_stored(self):
        src = Source('rtsp://0.0.0.0:8554/live', fps=15, serve=False)
        assert src._fps == 15

    def test_low_fps_stored(self):
        src = Source('rtsp://0.0.0.0:8554/live', fps=0.5, serve=False)
        assert src._fps == 0.5


# --- Source.put() ---

class TestSourcePut:

    def _make_source(self):
        src = Source('rtsp://localhost:8554/live', fps=25, serve=False)
        src.open = lambda: src
        return src

    def test_put_sets_size_from_first_frame(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        img = Image.new('RGB', (320, 240))
        src.put(img)
        assert src._size == (320, 240)

    def test_put_resizes_subsequent_frames(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        src.put(Image.new('RGB', (320, 240)))
        large = Image.new('RGB', (1920, 1080))
        src.put(large)
        assert src._buffer[-1].size == (320, 240)

    def test_put_accepts_numpy(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        arr = np.zeros((240, 320, 3), dtype=np.uint8)
        src.put(arr)
        assert src._size == (320, 240)

    def test_put_accepts_file_path(self, tmp_path):
        img = Image.new('RGB', (64, 64))
        p = tmp_path / 'f.png'
        img.save(str(p))
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        src.put(str(p))
        assert src._size == (64, 64)

    def test_buffer_grows_with_each_put(self):
        src = Source('rtsp://localhost:8554/live', serve=False)
        src.open = lambda: src
        for _ in range(5):
            src.put(Image.new('RGB', (64, 64)))
        assert len(src._buffer) == 5


# --- Source frame_buffer= constructor parameter ---

@requires_av
class TestSourceFramesParam:

    @pytest.fixture(autouse=True)
    def _no_open(self, monkeypatch):
        monkeypatch.setattr(Publisher, 'open', lambda self: self)

    def _make(self, frames):
        return Source('rtsp://0.0.0.0:8554/live', frame_buffer=frames, serve=False)

    def test_list_of_pil_images(self):
        imgs = [Image.new('RGB', (32, 32), color=(i * 80, 0, 0)) for i in range(3)]
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=imgs, serve=False)
        src.open = lambda: src
        # open is stubbed but put() already ran - check buffer directly
        src2 = Source('rtsp://0.0.0.0:8554/live', serve=False)
        src2.open = lambda: src2
        for img in imgs:
            src2.put(img)
        assert len(src._buffer) == len(src2._buffer) == 3

    def test_tuple_of_images(self):
        imgs = tuple(Image.new('RGB', (8, 8)) for _ in range(4))
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=imgs, serve=False)
        src.open = lambda: src
        assert len(src._buffer) == 4

    def test_generator_expression(self):
        src = Source('rtsp://0.0.0.0:8554/live', serve=False,
                     frame_buffer=(Image.new('RGB', (8, 8)) for _ in range(5)))
        src.open = lambda: src
        if src._loader:
            src._loader.join(timeout=5)
        assert len(src._buffer) == 5

    def test_pathlib_glob(self, tmp_path):
        for i in range(3):
            Image.new('RGB', (8, 8), color=(i * 80, 0, 0)).save(tmp_path / f'{i}.png')
        src = Source('rtsp://0.0.0.0:8554/live', serve=False,
                     frame_buffer=sorted(tmp_path.glob('*.png')))
        src.open = lambda: src
        if src._loader:
            src._loader.join(timeout=5)
        assert len(src._buffer) == 3

    def test_size_inferred_from_first_frame(self):
        imgs = [Image.new('RGB', (160, 120)) for _ in range(2)]
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=imgs, serve=False)
        src.open = lambda: src
        assert src._size == (160, 120)

    def test_none_leaves_buffer_empty(self):
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=None, serve=False)
        assert len(src._buffer) == 0

    def test_numpy_arrays_in_iterable(self):
        arrays = [np.zeros((16, 16, 3), dtype=np.uint8) for _ in range(2)]
        src = Source('rtsp://0.0.0.0:8554/live', frame_buffer=arrays, serve=False)
        src.open = lambda: src
        assert len(src._buffer) == 2


# --- Source.serve_forever() ---

@requires_av
class TestServeForever:

    def test_raises_for_push_mode(self):
        """serve_forever() is not supported for serve=False (push to relay)."""
        src = Source('rtsp://localhost:8554/live', serve=False)
        with pytest.raises(RuntimeError, match='serve=True'):
            src.serve_forever()


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

    def test_timestamp_near_rollover(self):
        p = _RTPPacketizer()
        ts = 0xFFFFFFFF - 900
        pkts = p.packetize(b'\x65' + b'\xAB' * 100, ts=ts, last_nal=True)
        ts_in_pkt = struct.unpack('!I', pkts[0][4:8])[0]
        assert ts_in_pkt == ts

    def test_timestamp_after_rollover(self):
        p = _RTPPacketizer()
        ts = (0xFFFFFFFF + 1800) & 0xFFFFFFFF
        pkts = p.packetize(b'\x65' + b'\xAB' * 100, ts=ts, last_nal=True)
        ts_in_pkt = struct.unpack('!I', pkts[0][4:8])[0]
        assert ts_in_pkt == ts


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

    @requires_av
    def test_size_snaps_to_even(self):
        src = Source('rtsp://0.0.0.0:19602/live', size=(641, 479))
        assert src._size == (640, 478)

    def test_not_opened_before_first_frame_without_size(self):
        src = Source('rtsp://0.0.0.0:19603/live')
        assert not src.isOpened()

    @requires_av
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

    def test_ten_simultaneous_clients(self, source_and_uri):
        N = 10
        src, uri = source_and_uri
        frames = [None] * N
        errors = []

        def _connect(idx):
            try:
                with rtsp.Client(uri) as c:
                    frames[idx] = _wait_frame(c, timeout=20)
            except Exception as e:
                errors.append((idx, e))

        threads = [threading.Thread(target=_connect, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, 'exceptions in {} client(s): {}'.format(len(errors), errors)
        missing = [i for i, f in enumerate(frames) if f is None]
        assert not missing, 'clients {} received no frame'.format(missing)

    def test_sdp_contains_h264(self, source_and_uri):
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
        with rtsp.Client(uri) as client:
            _wait_frame(client)
        sdp = src._sdp()
        assert 'sprop-parameter-sets=' in sdp

    def test_sps_is_constrained_baseline(self, source_and_uri):
        """SPS NAL must signal Constrained Baseline (profile_idc=66, set0+set1=1).

        Constrained Baseline is required by iOS AVPlayer and is the most
        broadly compatible H.264 profile.
        """
        src, uri = source_and_uri
        with rtsp.Client(uri) as client:
            _wait_frame(client)
        sps = src._sps
        assert sps, 'SPS not populated after frame received'
        profile_idc = sps[1]
        constraint_byte = sps[2]
        assert profile_idc == 66, \
            'expected Baseline profile_idc=66, got {}'.format(profile_idc)
        assert (constraint_byte >> 7) & 1, 'constraint_set0_flag not set'
        assert (constraint_byte >> 6) & 1, 'constraint_set1_flag not set'


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


# ---------------------------------------------------------------------------
# Unit: _Session protocol handling
# ---------------------------------------------------------------------------

def _session_run(coro_factory):
    """Run an async test that needs a fresh event loop with a _Session inside it."""
    async def _wrapper():
        writer = MagicMock()
        writer.get_extra_info.return_value = ('127.0.0.1', 12345)
        writer.is_closing.return_value = False
        server = MagicMock()
        server._sdp.return_value = 'v=0\r\nm=video 0 RTP/AVP 96\r\n'
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        session = _Session(reader, writer, server, loop)
        return await coro_factory(session, reader, writer, server)
    return asyncio.run(_wrapper())


class TestSessionReadRequest:

    def test_normal_request_parsed(self):
        async def _t(session, reader, writer, server):
            reader.feed_data(b'OPTIONS rtsp://x RTSP/1.0\r\n\r\n')
            result = await session._read_request()
            assert result is not None
            assert result[0] == 'OPTIONS'
            assert result[1] == 'rtsp://x'
        _session_run(_t)

    def test_blank_first_line_retries(self):
        async def _t(session, reader, writer, server):
            reader.feed_data(b'\r\nOPTIONS rtsp://x RTSP/1.0\r\n\r\n')
            result = await session._read_request()
            assert result is not None
            assert result[0] == 'OPTIONS'
        _session_run(_t)

    def test_both_lines_blank_returns_none(self):
        async def _t(session, reader, writer, server):
            reader.feed_data(b'\r\n\r\n')
            result = await session._read_request()
            assert result is None
        _session_run(_t)

    def test_single_token_line_returns_none(self):
        async def _t(session, reader, writer, server):
            reader.feed_data(b'BADLINE\r\n')
            result = await session._read_request()
            assert result is None
        _session_run(_t)

    def test_headers_parsed(self):
        async def _t(session, reader, writer, server):
            reader.feed_data(
                b'OPTIONS rtsp://x RTSP/1.0\r\n'
                b'CSeq: 1\r\nUser-Agent: test\r\n\r\n'
            )
            _, _, headers, _ = await session._read_request()
            assert headers['cseq'] == '1'
            assert headers['user-agent'] == 'test'
        _session_run(_t)

    def test_content_length_body_read(self):
        async def _t(session, reader, writer, server):
            body = b'hello body'
            reader.feed_data(
                b'ANNOUNCE rtsp://x RTSP/1.0\r\nContent-Length: 10\r\n\r\n' + body
            )
            _, _, _, parsed_body = await session._read_request()
            assert parsed_body == body
        _session_run(_t)

    def test_content_length_readexactly_failure_returns_empty_body(self):
        async def _t(session, reader, writer, server):
            reader.feed_data(
                b'ANNOUNCE rtsp://x RTSP/1.0\r\nContent-Length: 999\r\n\r\n'
            )
            reader.feed_eof()
            _, _, _, body = await session._read_request()
            assert body == b''
        _session_run(_t)


class TestSessionDispatch:

    def test_options(self):
        async def _t(session, reader, writer, server):
            await session._dispatch('OPTIONS', 'rtsp://x', {'cseq': '1'}, None)
            response = writer.write.call_args[0][0].decode()
            assert '200 OK' in response and 'OPTIONS' in response
        _session_run(_t)

    def test_describe(self):
        async def _t(session, reader, writer, server):
            await session._dispatch('DESCRIBE', 'rtsp://x', {'cseq': '2'}, None)
            response = writer.write.call_args[0][0].decode()
            assert '200 OK' in response and 'application/sdp' in response
        _session_run(_t)

    def test_setup_tcp(self):
        async def _t(session, reader, writer, server):
            headers = {'cseq': '3', 'transport': 'RTP/AVP/TCP;unicast;interleaved=0-1'}
            await session._dispatch('SETUP', 'rtsp://x/track0', headers, ('127.0.0.1', 5000))
            assert session._tcp_channel == 0
            assert '200 OK' in writer.write.call_args[0][0].decode()
        _session_run(_t)

    def test_setup_udp(self):
        async def _t(session, reader, writer, server):
            headers = {'cseq': '3', 'transport': 'RTP/AVP;unicast;client_port=10000-10001'}
            await session._dispatch('SETUP', 'rtsp://x/track0', headers, ('127.0.0.1', 5000))
            assert session._udp_sock is not None
            assert session._rtp_addr == ('127.0.0.1', 10000)
            session._udp_sock.close()
        _session_run(_t)

    def test_setup_unsupported_transport_replies_461(self):
        async def _t(session, reader, writer, server):
            headers = {'cseq': '3', 'transport': 'RTP/INVALID'}
            await session._dispatch('SETUP', 'rtsp://x', headers, None)
            assert '461' in writer.write.call_args[0][0].decode()
        _session_run(_t)

    def test_play_sets_playing(self):
        async def _t(session, reader, writer, server):
            assert not session._playing
            await session._dispatch('PLAY', 'rtsp://x', {'cseq': '4'}, None)
            assert session._playing
        _session_run(_t)

    def test_teardown_clears_playing(self):
        async def _t(session, reader, writer, server):
            session._playing = True
            await session._dispatch('TEARDOWN', 'rtsp://x', {'cseq': '5'}, None)
            assert not session._playing
        _session_run(_t)

    def test_get_parameter_clears_playing(self):
        async def _t(session, reader, writer, server):
            session._playing = True
            await session._dispatch('GET_PARAMETER', 'rtsp://x', {'cseq': '6'}, None)
            assert not session._playing
        _session_run(_t)

    def test_unknown_method_replies_501(self):
        async def _t(session, reader, writer, server):
            await session._dispatch('RECORD', 'rtsp://x', {'cseq': '7'}, None)
            assert '501' in writer.write.call_args[0][0].decode()
        _session_run(_t)


class TestSessionSetupTransport:

    def test_tcp_with_interleaved_channel(self):
        async def _t(session, reader, writer, server):
            result = session._setup_transport(
                'RTP/AVP/TCP;unicast;interleaved=2-3', ('127.0.0.1', 0))
            assert result is True
            assert session._tcp_channel == 2
            assert session._udp_sock is None
        _session_run(_t)

    def test_tcp_without_interleaved_defaults_to_zero(self):
        async def _t(session, reader, writer, server):
            result = session._setup_transport('RTP/AVP/TCP;unicast', ('127.0.0.1', 0))
            assert result is True
            assert session._tcp_channel == 0
        _session_run(_t)

    def test_udp_with_client_port(self):
        async def _t(session, reader, writer, server):
            result = session._setup_transport(
                'RTP/AVP;unicast;client_port=10000-10001', ('127.0.0.1', 0))
            assert result is True
            assert session._udp_sock is not None
            assert session._rtp_addr == ('127.0.0.1', 10000)
            assert session._tcp_channel is None
            session._udp_sock.close()
        _session_run(_t)

    def test_udp_without_client_port_returns_false(self):
        async def _t(session, reader, writer, server):
            result = session._setup_transport('RTP/AVP;unicast', ('127.0.0.1', 0))
            assert result is False
        _session_run(_t)


class TestSessionTransportResponse:

    def test_tcp_response(self):
        async def _t(session, reader, writer, server):
            session._tcp_channel = 4
            resp = session._transport_response('')
            assert 'RTP/AVP/TCP' in resp and 'interleaved=4-5' in resp
        _session_run(_t)

    def test_udp_response(self):
        async def _t(session, reader, writer, server):
            session._tcp_channel = None
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(('', 0))
            session._udp_sock = sock
            session._rtp_addr = ('127.0.0.1', 12345)
            resp = session._transport_response('')
            assert 'RTP/AVP' in resp and 'client_port=12345' in resp
            sock.close()
        _session_run(_t)


class TestSessionRun:

    def test_incomplete_read_error_exits_cleanly(self):
        async def _t(session, reader, writer, server):
            with patch.object(session, '_read_request',
                              side_effect=asyncio.IncompleteReadError(b'', None)):
                await session.run()
            assert not session._playing
            server._drop.assert_called_once_with(session)
        _session_run(_t)

    def test_connection_reset_error_exits_cleanly(self):
        async def _t(session, reader, writer, server):
            with patch.object(session, '_read_request', side_effect=ConnectionResetError):
                await session.run()
            assert not session._playing
            server._drop.assert_called_once_with(session)
        _session_run(_t)

    def test_broken_pipe_error_exits_cleanly(self):
        async def _t(session, reader, writer, server):
            with patch.object(session, '_read_request', side_effect=BrokenPipeError):
                await session.run()
            assert not session._playing
            server._drop.assert_called_once_with(session)
        _session_run(_t)

    def test_none_request_ends_loop(self):
        async def _t(session, reader, writer, server):
            with patch.object(session, '_read_request', return_value=None):
                await session.run()
            server._drop.assert_called_once_with(session)
        _session_run(_t)

    def test_writer_close_exception_is_swallowed(self):
        async def _t(session, reader, writer, server):
            writer.close.side_effect = RuntimeError('already closed')
            with patch.object(session, '_read_request', return_value=None):
                await session.run()
            server._drop.assert_called_once_with(session)
        _session_run(_t)


# ---------------------------------------------------------------------------
# Unit: _Session.send_rtp UDP OSError is swallowed
# ---------------------------------------------------------------------------

class TestSessionSendRtpUDP:

    def test_udp_oserror_is_swallowed(self):
        from rtsp.source import _Session, _RTSPServer
        loop = asyncio.new_event_loop()
        reader = MagicMock()
        writer = MagicMock()
        writer.get_extra_info.return_value = ('127.0.0.1', 12345)
        writer.is_closing.return_value = False
        server = MagicMock(spec=_RTSPServer)
        server._drop = MagicMock()

        session = _Session(reader, writer, server, loop)
        session._playing = True
        session._rtp_addr = ('127.0.0.1', 5004)
        session._udp_sock = MagicMock()
        session._udp_sock.sendto.side_effect = OSError('network unreachable')

        session.send_rtp(b'\x80\x60' + b'\x00' * 10)
        loop.close()


# ---------------------------------------------------------------------------
# Unit: Source raises ImportError when _av is None
# ---------------------------------------------------------------------------

class TestSourceNoAV:

    def test_raises_import_error_on_start(self):
        from rtsp.source import Source as _Source
        with patch('rtsp.source._av', None):
            with pytest.raises(ImportError, match='PyAV'):
                _Source('rtsp://0.0.0.0:8554/live', size=(64, 64))


# ---------------------------------------------------------------------------
# Unit: Source._start() idempotent
# ---------------------------------------------------------------------------

class TestSourceStartIdempotent:

    def test_start_twice_is_noop(self):
        from rtsp.source import Source as _Source
        from threading import Lock, Event
        src = _Source.__new__(_Source)
        src._bg_run = False
        src._loop = None
        src._rtsp = None
        src._ready = Event()
        src._encoding_started = Event()
        src._fps = 25
        src._port = 8554
        src._host = '0.0.0.0'
        src._path = '/live'
        src._size = (640, 480)
        src._buffer = []
        src._lock = Lock()
        src._sps = None
        src._pps = None
        src._verbose = False

        with patch('rtsp.source._RTSPServer'), \
             patch('rtsp.source.asyncio.new_event_loop', return_value=MagicMock()), \
             patch('rtsp.source.Thread') as mock_thread, \
             patch('rtsp.source._av', MagicMock()):
            mock_thread.return_value.start = MagicMock()
            src._start()
            src._start()

        assert mock_thread.call_count == 2


# ---------------------------------------------------------------------------
# Unit: Source.close() exception in rtsp.stop() is swallowed
# ---------------------------------------------------------------------------

class TestSourceCloseException:

    def test_stop_exception_is_swallowed(self):
        from rtsp.source import Source as _Source
        src = _Source.__new__(_Source)
        src._bg_run = True
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        src._loop = mock_loop
        src._rtsp = MagicMock()
        mock_future = MagicMock()
        mock_future.result.side_effect = TimeoutError('timed out')
        with patch('rtsp.source.asyncio.run_coroutine_threadsafe',
                   return_value=mock_future):
            src.close()
        assert src._bg_run is False


# ---------------------------------------------------------------------------
# Unit: Publisher raises ImportError when _av is None
# ---------------------------------------------------------------------------

class TestPublisherNoAV:

    def test_open_raises_import_error(self):
        with patch('rtsp.source._av', None):
            with pytest.raises(ImportError, match='PyAV'):
                Publisher('rtsp://localhost:8554/live', size=(64, 64))


# ---------------------------------------------------------------------------
# Unit: Publisher.open() verbose log and handshake exception cleanup
# ---------------------------------------------------------------------------

class TestPublisherOpen:

    def _make_publisher(self):
        with patch('rtsp.source._av', MagicMock()):
            p = Publisher.__new__(Publisher)
        p._verbose = False
        p._host = '127.0.0.1'
        p._port = 8554
        p._uri = 'rtsp://127.0.0.1:8554/live'
        p._fps = 25
        p._size = (640, 480)
        p._sock = None
        p._cseq = 0
        p._session_id = None
        p._bg_run = False
        p._encode_thread = None
        p._loader = None
        from threading import Lock
        p._lock = Lock()
        p._buffer = []
        return p

    def test_verbose_logs_publishing(self):
        p = self._make_publisher()
        p._verbose = True
        mock_sock = MagicMock()
        with patch('rtsp.source.socket.create_connection', return_value=mock_sock), \
             patch.object(p, '_rtsp_announce'), \
             patch.object(p, '_rtsp_setup'), \
             patch.object(p, '_rtsp_record'), \
             patch('rtsp.source.Thread') as mock_thread, \
             patch('rtsp.source._av', MagicMock()), \
             patch('rtsp.source.log') as mock_log:
            mock_thread.return_value.start = MagicMock()
            p.open()
        mock_log.info.assert_any_call('publishing to %s', p._uri)

    def test_handshake_exception_cleans_up_socket(self):
        p = self._make_publisher()
        mock_sock = MagicMock()
        with patch('rtsp.source.socket.create_connection', return_value=mock_sock), \
             patch.object(p, '_rtsp_announce', side_effect=RuntimeError('bad handshake')):
            with pytest.raises(RuntimeError):
                p.open()
        assert p._sock is None
        mock_sock.close.assert_called_once()

    def test_handshake_exception_swallows_sock_close_oserror(self):
        p = self._make_publisher()
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError('already closed')
        with patch('rtsp.source.socket.create_connection', return_value=mock_sock), \
             patch.object(p, '_rtsp_announce', side_effect=RuntimeError('bad handshake')):
            with pytest.raises(RuntimeError):
                p.open()
        assert p._sock is None


# ---------------------------------------------------------------------------
# Unit: Publisher.close() swallows OSError from sock.close()
# ---------------------------------------------------------------------------

class TestPublisherCloseOSError:

    def test_sock_close_oserror_is_swallowed(self):
        with patch('rtsp.source._av', MagicMock()):
            p = Publisher.__new__(Publisher)
        p._bg_run = True
        p._loader = None
        p._encode_thread = None
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError('already gone')
        p._sock = mock_sock
        p.close()
        assert p._sock is None


# ---------------------------------------------------------------------------
# Unit: Publisher._send_rtp sock=None exits cleanly
# ---------------------------------------------------------------------------

class TestPublisherSendRtp:

    def test_sock_none_clears_bg_run(self):
        with patch('rtsp.source._av', MagicMock()):
            p = Publisher.__new__(Publisher)
        p._bg_run = True
        p._sock = None
        p._send_rtp(b'\x80\x60' + b'\x00' * 10)
        assert p._bg_run is False

    def test_oserror_on_sendall_clears_bg_run(self):
        with patch('rtsp.source._av', MagicMock()):
            p = Publisher.__new__(Publisher)
        p._bg_run = True
        mock_sock = MagicMock()
        mock_sock.sendall.side_effect = OSError('broken pipe')
        p._sock = mock_sock
        p._send_rtp(b'\x80\x60' + b'\x00' * 10)
        assert p._bg_run is False


# ---------------------------------------------------------------------------
# Unit: source._Reader EOF paths
# ---------------------------------------------------------------------------

class TestSourceReaderEOF:

    def test_read_until_raises_on_eof(self):
        from rtsp.source import _Reader
        sock = MagicMock()
        sock.recv.return_value = b''
        r = _Reader(sock)
        with pytest.raises(ConnectionError):
            r.read_until(b'\r\n\r\n')

    def test_read_exact_raises_on_eof(self):
        from rtsp.source import _Reader
        sock = MagicMock()
        sock.recv.return_value = b''
        r = _Reader(sock)
        with pytest.raises(ConnectionError):
            r.read_exact(10)


# ---------------------------------------------------------------------------
# Unit: Source.wait_encoding_started()
# ---------------------------------------------------------------------------

class TestWaitEncodingStarted:

    def _make(self):
        from threading import Event
        from rtsp.source import Source as _Source
        src = _Source.__new__(_Source)
        src._encoding_started = Event()
        return src

    def test_returns_true_when_already_set(self):
        src = self._make()
        src._encoding_started.set()
        assert src.wait_encoding_started(timeout=1) is True

    def test_returns_false_on_timeout(self):
        src = self._make()
        assert src.wait_encoding_started(timeout=0.01) is False


# ---------------------------------------------------------------------------
# Integration: _encoding_started event fires after first encoded frame
# ---------------------------------------------------------------------------

@requires_av
class TestEncodingStartedFires:

    def test_event_set_after_putting_first_frame(self):
        port = _free_port()
        src = Source('rtsp://0.0.0.0:{}/live'.format(port), size=(64, 64))
        src.put(Image.new('RGB', (64, 64)))
        result = src.wait_encoding_started(timeout=10)
        src.close()
        assert result, '_encoding_started never fired after encoding a frame'
