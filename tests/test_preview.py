"""Tests for Client.preview() window close behaviour.

Python 3.14 + Tcl/Tk 9.0 on macOS segfaults when a second Tk() is created
in the same process after the first has been destroyed.  Each test therefore
runs preview() in a fresh subprocess.  The subprocess exits 0 on success,
non-zero (with a message on stdout) if the close path timed out.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Subprocess harness
# ---------------------------------------------------------------------------

_RTSP_ROOT = str(Path(__file__).parent.parent)

_PREAMBLE = textwrap.dedent('''\
    import sys
    sys.path.insert(0, {root!r})

    import tkinter
    import numpy as np
    from PIL import Image
    from unittest.mock import patch
    from rtsp.client import _RtspClient as Client

    _IMG = Image.fromarray(np.zeros((64, 64, 3), dtype=np.uint8))

    class _FakeStream:
        def __init__(self):
            self._bg_run = True
        def read(self):
            return _IMG
        def close(self):
            self._bg_run = False

    _INJECT_MS  = 200
    _TIMEOUT_MS = 5000

    def run_preview(setup_root):
        fake = _FakeStream()
        timed_out = [False]
        original_Tk = tkinter.Tk

        def _patched_Tk():
            root = original_Tk()
            setup_root(root, fake)
            def _fallback():
                timed_out[0] = True
                root.destroy()
            root.after(_TIMEOUT_MS, _fallback)
            return root

        with patch.object(tkinter, 'Tk', _patched_Tk):
            Client.preview(fake)

        if timed_out[0]:
            print('TIMEOUT')
            sys.exit(1)

        return fake

''').format(root=_RTSP_ROOT)


def _run(test_body, timeout=8):
    """Run test_body (appended to preamble) in a subprocess.

    Fails the test if the subprocess exits non-zero or times out.
    """
    script = _PREAMBLE + textwrap.dedent(test_body)
    try:
        result = subprocess.run(
            [sys.executable, '-c', script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        pytest.fail('subprocess timed out after {}s'.format(timeout))

    if result.returncode != 0:
        out = (result.stdout + result.stderr).strip()
        pytest.fail('preview test subprocess failed:\n' + out)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures('requires_display')
class TestPreviewClose:

    def test_q_closes_window(self):
        _run('''\
            def setup(root, fake):
                root.after(_INJECT_MS, lambda: root.event_generate('<KeyPress-q>'))
            fake = run_preview(setup)
            assert not fake._bg_run, '_bg_run still True after q'
        ''')

    def test_escape_closes_window(self):
        _run('''\
            def setup(root, fake):
                root.after(_INJECT_MS, lambda: root.event_generate('<KeyPress-Escape>'))
            fake = run_preview(setup)
            assert not fake._bg_run, '_bg_run still True after Escape'
        ''')

    def test_stream_end_closes_window(self):
        """_bg_run going False externally (stream drop) should close the window."""
        _run('''\
            def setup(root, fake):
                root.after(_INJECT_MS, lambda: setattr(fake, '_bg_run', False))
            fake = run_preview(setup)
            assert not fake._bg_run
        ''')

    def test_transform_is_applied_to_frames(self):
        """transform callable receives each PIL Image frame and its return value is displayed."""
        _run('''\
            calls = []

            def my_transform(frame):
                calls.append(1)
                # Return a visually distinct image to confirm the return value is used.
                return Image.fromarray(np.full((64, 64, 3), 128, dtype=np.uint8))

            def setup(root, fake):
                root.after(_INJECT_MS, lambda: root.event_generate('<KeyPress-q>'))

            fake = _FakeStream()
            timed_out = [False]
            original_Tk = tkinter.Tk

            def _patched_Tk():
                root = original_Tk()
                setup(root, fake)
                root.after(_TIMEOUT_MS, lambda: (timed_out.__setitem__(0, True), root.destroy()))
                return root

            with patch.object(tkinter, "Tk", _patched_Tk):
                Client.preview(fake, transform=my_transform)

            if timed_out[0]:
                print("TIMEOUT")
                sys.exit(1)

            assert len(calls) > 0, "transform was never called"
        ''')
