import pytest

from cutlist.media.caption import render_caption
from cutlist.media.probe import probe
from cutlist.media.render import Segment, concat, encode_segment, render_clip
from cutlist.presets import CaptionSpec, OutputSpec

OUTPUT = OutputSpec(width=854, height=480, fps=25, crf=20)


@pytest.fixture
def caption(tmp_path):
    return render_caption(CaptionSpec(text="ТЕСТ"), OUTPUT, tmp_path / "caption.png")


def test_segment_end(tmp_path):
    assert Segment(start=3.0, duration=2.0).end == 5.0


def test_encoded_segment_matches_output_spec(fixture_film, caption, tmp_path):
    dest = encode_segment(
        fixture_film, Segment(2.0, 2.0), caption, OUTPUT, tmp_path / "seg.mp4"
    )
    info = probe(dest)
    assert (info.width, info.height) == (854, 480)
    assert info.fps == pytest.approx(25.0)
    assert info.duration == pytest.approx(2.0, abs=0.2)


def test_encoded_segment_is_silent(fixture_film, caption, tmp_path):
    dest = encode_segment(
        fixture_film, Segment(2.0, 2.0), caption, OUTPUT, tmp_path / "seg.mp4"
    )
    assert probe(dest).has_audio is False


def test_concat_sums_durations(fixture_film, caption, tmp_path):
    parts = [
        encode_segment(
            fixture_film, Segment(start, 2.0), caption, OUTPUT, tmp_path / f"s{i}.mp4"
        )
        for i, start in enumerate([2.0, 7.0, 12.0])
    ]
    dest = concat(parts, tmp_path / "joined.mp4")
    assert probe(dest).duration == pytest.approx(6.0, abs=0.4)


def test_render_clip_end_to_end(fixture_film, caption, tmp_path):
    segments = [Segment(2.0, 2.0), Segment(7.0, 2.5), Segment(17.0, 2.0)]
    dest = render_clip(
        fixture_film, segments, caption, OUTPUT, tmp_path / "clip.mp4", tmp_path / "scratch"
    )
    info = probe(dest)
    assert info.has_audio is False
    assert (info.width, info.height) == (854, 480)
    assert info.duration == pytest.approx(6.5, abs=0.4)


def test_render_clip_cleans_up_scratch(fixture_film, caption, tmp_path):
    scratch = tmp_path / "scratch"
    render_clip(
        fixture_film, [Segment(2.0, 2.0)], caption, OUTPUT, tmp_path / "clip.mp4", scratch
    )
    assert not scratch.exists()
