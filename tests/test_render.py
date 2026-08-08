from pathlib import Path

import pytest
from PIL import Image

from cutlist.media.caption import render_caption
from cutlist.media.probe import probe
from cutlist.media.render import Segment, concat, encode_segment, render_clip
from cutlist.presets import CaptionSpec, OutputSpec
from cutlist.shell import run
from tests.conftest import FIXTURE_HEX_COLORS, FIXTURE_SHOT_SECONDS

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


def test_render_clip_removes_partial_dest_on_failure(fixture_film, caption, tmp_path, monkeypatch):
    # Seed dest with a stale file, standing in for a truncated leftover from
    # an earlier crash. A failure partway through rendering should remove
    # it rather than leave something that looks like a finished clip.
    dest = tmp_path / "clip.mp4"
    dest.write_bytes(b"stale leftover from a previous crash")
    scratch = tmp_path / "scratch"

    real_encode_segment = encode_segment
    calls = []

    def flaky_encode_segment(film, segment, caption_png, output, seg_dest):
        calls.append(segment)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real_encode_segment(film, segment, caption_png, output, seg_dest)

    monkeypatch.setattr("cutlist.media.render.encode_segment", flaky_encode_segment)

    segments = [Segment(2.0, 2.0), Segment(7.0, 2.0), Segment(12.0, 2.0)]
    with pytest.raises(RuntimeError):
        render_clip(fixture_film, segments, caption, OUTPUT, dest, scratch)

    assert not scratch.exists()
    assert not dest.exists()


def test_concat_handles_apostrophe_in_path(fixture_film, caption, tmp_path):
    root = tmp_path / "Bob's Movies"
    dest = render_clip(
        fixture_film,
        [Segment(2.0, 2.0), Segment(7.0, 2.0)],
        caption,
        OUTPUT,
        root / "clip.mp4",
        root / "scratch",
    )
    info = probe(dest)
    assert info.duration == pytest.approx(4.0, abs=0.4)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    n = int(value, 16)
    return (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF


def _sample_frame(clip: Path, at: float, tmp_path: Path, tag: str) -> tuple[int, int, int]:
    """Grab one frame and read a pixel from the middle of the picture.

    The caption sits in a band at the top, so sampling the vertical centre
    (or lower) avoids it. The 320x240 fixture is pillarboxed into the wider
    854x480 output, so sampling the horizontal centre stays inside the
    picture rather than the black bars at the sides.
    """
    frame_path = tmp_path / f"frame_{tag}.png"
    run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{at:.3f}", "-i", str(clip),
        "-frames:v", "1", str(frame_path),
    ])
    img = Image.open(frame_path).convert("RGB")
    return img.getpixel((img.width // 2, img.height // 2))


def test_render_clip_content_matches_source_segments(fixture_film, caption, tmp_path):
    """A bug that ignored segment.start (e.g. always encoding from t=0)
    would still sum to the right total duration and pass every other test
    in this file. Sampling actual frame colour is what catches it.
    """
    # Non-adjacent blocks of the six-colour fixture: red, navy, dark grey.
    segments = [Segment(2.5, 2.0), Segment(12.5, 2.0), Segment(22.5, 2.0)]
    expected_colours = [
        FIXTURE_HEX_COLORS[int(segment.start // FIXTURE_SHOT_SECONDS)]
        for segment in segments
    ]

    dest = render_clip(
        fixture_film, segments, caption, OUTPUT, tmp_path / "clip.mp4", tmp_path / "scratch"
    )

    elapsed = 0.0
    for i, (segment, expected_hex) in enumerate(zip(segments, expected_colours)):
        midpoint = elapsed + segment.duration / 2
        elapsed += segment.duration

        pixel = _sample_frame(dest, midpoint, tmp_path, str(i))
        expected = _hex_to_rgb(expected_hex)
        assert all(abs(a - b) <= 40 for a, b in zip(pixel, expected)), (
            f"segment {i} (source start {segment.start}s) sampled {pixel} "
            f"at output {midpoint:.2f}s, expected ~{expected} ({expected_hex})"
        )
