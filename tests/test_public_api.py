"""Verify that only the intended names are exposed in the rtsp package public API."""
import rtsp

_EXPECTED_PUBLIC = {'Client', 'Source', 'list_devices'}


class TestPublicAPI:

    def test_expected_names_present(self):
        missing = _EXPECTED_PUBLIC - set(rtsp.__all__)
        assert not missing, 'Missing from __all__: {}'.format(missing)

    def test_no_unexpected_names(self):
        unexpected = set(rtsp.__all__) - _EXPECTED_PUBLIC
        assert not unexpected, 'Unexpected names in __all__: {}'.format(unexpected)

    def test_internal_classes_not_exported(self):
        for name in ('Publisher', 'RTMPClient', 'RTMPPublisher'):
            assert name not in rtsp.__all__, '{!r} should not be in __all__'.format(name)

    def test_internal_modules_not_exported(self):
        for name in ('_utils', 'client', 'source', 'rtmp'):
            assert name not in rtsp.__all__, '{!r} should not be in __all__'.format(name)
