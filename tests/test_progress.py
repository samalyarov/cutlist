"""The spinner shown while ffmpeg works.

What it protects: that progress feedback never changes what a command does
-- it stays off stdout, animates only where something can watch it, and does
not swallow the failure it was drawn over.
"""

import io
import sys

import pytest
from rich.status import Status

from cutlist.cli import working


class _Tty(io.StringIO):
    """A stream that claims to be a terminal, which is all rich checks."""

    def isatty(self) -> bool:
        return True


def test_animates_when_stderr_is_a_terminal(monkeypatch):
    """A terminal gets the live Status, not a static line.

    If this regresses, ffmpeg goes back to running behind a silent prompt
    with nothing to say whether it is working or hung.
    """
    monkeypatch.setattr(sys, "stderr", _Tty())
    with working("cutting 01/03") as status:
        assert isinstance(status, Status)


def test_prints_one_line_when_nothing_can_watch(capsys):
    """Piped or redirected output gets the text once and no animation.

    Under pytest, stderr is captured and not a tty. If this regresses, a log
    or a CI run fills with thousands of overwritten spinner frames.
    """
    with working("detecting shots") as status:
        assert status is None

    captured = capsys.readouterr()
    assert captured.err == "detecting shots...\n"
    # stdout is where results go; a progress line there would land inside
    # `shots --json` and `library --json`.
    assert captured.out == ""


def test_a_failure_inside_still_escapes(monkeypatch):
    """The context manager re-raises whatever the work raised.

    If this regresses, a failed render is reported as a written clip.
    """
    monkeypatch.setattr(sys, "stderr", _Tty())
    with pytest.raises(RuntimeError, match="ffmpeg blew up"):
        with working("cutting 01/03"):
            raise RuntimeError("ffmpeg blew up")
