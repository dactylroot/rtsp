import pytest
from rtsp._utils import _parse_uri, _redact_uri


class TestParseUri:

    # --- device ---

    def test_int_is_device(self):
        assert _parse_uri(0) == ('device', 0)

    def test_numeric_string_is_device(self):
        assert _parse_uri('0') == ('device', 0)
        assert _parse_uri('2') == ('device', 2)

    def test_numeric_string_with_whitespace(self):
        assert _parse_uri('  1  ') == ('device', 1)

    # --- network: explicit schemes ---

    @pytest.mark.parametrize('scheme', ['rtsp', 'rtsps', 'rtmp', 'rtmps', 'http', 'https'])
    def test_supported_schemes(self, scheme):
        kind, uri = _parse_uri('{}://192.168.1.1/stream'.format(scheme))
        assert kind == 'network'
        assert uri.startswith(scheme + '://')

    def test_rtsp_uri_preserved(self):
        raw = 'rtsp://admin:pass@192.168.1.100:554/ch1'
        kind, uri = _parse_uri(raw)
        assert kind == 'network'
        assert uri == raw

    # --- network: scheme defaulting ---

    def test_bare_host_defaults_to_rtsp(self):
        kind, uri = _parse_uri('192.168.1.1/stream')
        assert kind == 'network'
        assert uri == 'rtsp://192.168.1.1/stream'

    def test_bare_host_no_path(self):
        kind, uri = _parse_uri('192.168.1.1')
        assert kind == 'network'
        assert uri.startswith('rtsp://')

    # --- validation errors ---

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match='Unsupported URI scheme'):
            _parse_uri('ftp://192.168.1.1/stream')

    def test_missing_hostname_raises(self):
        with pytest.raises(ValueError, match='missing a hostname'):
            _parse_uri('rtsp://')

    def test_wrong_type_raises(self):
        with pytest.raises(TypeError):
            _parse_uri(3.14)


class TestRedactUri:

    def test_no_password_unchanged(self):
        uri = 'rtsp://192.168.1.1/stream'
        assert _redact_uri(uri) == uri

    def test_password_redacted(self):
        uri = 'rtsp://admin:secret@192.168.1.1:554/ch1'
        redacted = _redact_uri(uri)
        assert 'secret' not in redacted
        assert '***' in redacted
        assert 'admin' in redacted
        assert '192.168.1.1' in redacted
        assert '554' in redacted

    def test_redacted_scheme_preserved(self):
        uri = 'rtsp://user:pw@cam.local/live'
        assert _redact_uri(uri).startswith('rtsp://')
