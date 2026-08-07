import pytest

from cutlist.media.probe import probe
from cutlist.shell import ToolError
from tests.conftest import FIXTURE_DURATION


def test_probe_reads_dimensions_and_fps(fixture_film):
    info = probe(fixture_film)
    assert (info.width, info.height) == (320, 240)
    assert info.fps == pytest.approx(25.0)


def test_probe_reads_duration(fixture_film):
    info = probe(fixture_film)
    assert info.duration == pytest.approx(FIXTURE_DURATION, abs=0.5)


def test_probe_detects_absent_audio(fixture_film):
    assert probe(fixture_film).has_audio is False


def test_probe_raises_on_missing_file(tmp_path):
    with pytest.raises(ToolError):
        probe(tmp_path / "nope.mp4")
