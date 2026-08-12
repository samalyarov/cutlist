from pathlib import Path

import pytest

from cutlist.media.probe import _pick_video_stream, probe
from cutlist.shell import ToolError, run
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


@pytest.fixture
def film_with_cover_art(tmp_path):
    """A tiny film muxed with a larger, disposition-flagged cover thumbnail.

    The cover is bigger than the real video on purpose: without the
    attached_pic filter it would also win a plain max-by-area comparison,
    so this reproduces the exact shape that trips up a naive pick.
    """
    base = tmp_path / "base.mp4"
    cover = tmp_path / "cover.png"
    out = tmp_path / "with_cover.mp4"

    run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", "color=c=0xFF0000:s=320x240:d=1:r=25",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base),
    ])
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", "color=c=0x00FF00:s=640x480", "-frames:v", "1", str(cover),
    ])
    run([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(base), "-i", str(cover),
        "-map", "0:v", "-map", "1:v",
        # Per-stream pixel formats: MJPEG needs full-range yuvj420p, and a global
        # -pix_fmt silently applies to all output streams including the cover art,
        # which stricter ffmpeg builds reject as non-compliant.
        "-c:v:0", "libx264", "-pix_fmt:v:0", "yuv420p",
        "-c:v:1", "mjpeg", "-pix_fmt:v:1", "yuvj420p",
        "-disposition:v:1", "attached_pic",
        str(out),
    ])
    return out


def test_probe_ignores_attached_cover_art(film_with_cover_art):
    info = probe(film_with_cover_art)
    assert (info.width, info.height) == (320, 240)


def test_pick_video_stream_prefers_largest_unflagged_stream():
    # A stray thumbnail with no disposition flag should still lose to the
    # real picture, purely on size.
    streams = [
        {"codec_type": "video", "width": 64, "height": 48, "disposition": {"attached_pic": 0}},
        {"codec_type": "video", "width": 1920, "height": 1080, "disposition": {"attached_pic": 0}},
    ]
    picked = _pick_video_stream(streams, Path("irrelevant.mp4"))
    assert (picked["width"], picked["height"]) == (1920, 1080)


def test_pick_video_stream_raises_toolerror_when_no_video_streams():
    streams = [{"codec_type": "audio"}]
    with pytest.raises(ToolError):
        _pick_video_stream(streams, Path("no_video.mp4"))
