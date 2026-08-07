import pytest

from cutlist.shell import ToolError, run


def test_run_returns_stdout():
    assert "ffprobe version" in run(["ffprobe", "-version"])


def test_run_raises_with_stderr_tail():
    with pytest.raises(ToolError) as excinfo:
        run(["ffprobe", "-i", "no-such-file.mp4"])
    message = str(excinfo.value)
    assert "no-such-file.mp4" in message
    assert "ffprobe" in message


def test_run_raises_when_binary_missing():
    with pytest.raises(ToolError):
        run(["definitely-not-a-real-binary"])
