from pathlib import Path

import pytest
from PIL import Image

from cutlist.media import render
from cutlist.media.caption import render_caption
from cutlist.media.probe import probe
from cutlist.media.render import Segment, concat, encode_segment, render_clip
from cutlist.presets import CaptionSpec, OutputSpec
from cutlist.shell import ToolError, run
from tests.conftest import FIXTURE_HEX_COLORS, FIXTURE_SHOT_SECONDS

OUTPUT = OutputSpec(width=854, height=480, fps=25, crf=20)


@pytest.fixture
def caption(tmp_path):
    return render_caption(CaptionSpec(text="ТЕСТ"), OUTPUT, tmp_path / "caption.png")


def test_segment_end(tmp_path):
    assert Segment(start=3.0, duration=2.0).end == 5.0


def test_encoded_segment_matches_output_spec(fixture_video, caption, tmp_path):
    dest = encode_segment(
        fixture_video, Segment(2.0, 2.0), caption, OUTPUT, tmp_path / "seg.mp4"
    )
    info = probe(dest)
    assert (info.width, info.height) == (854, 480)
    assert info.fps == pytest.approx(25.0)
    assert info.duration == pytest.approx(2.0, abs=0.2)


def test_encoded_segment_is_silent(fixture_video, caption, tmp_path):
    dest = encode_segment(
        fixture_video, Segment(2.0, 2.0), caption, OUTPUT, tmp_path / "seg.mp4"
    )
    assert probe(dest).has_audio is False


def test_concat_sums_durations(fixture_video, caption, tmp_path):
    parts = [
        encode_segment(
            fixture_video, Segment(start, 2.0), caption, OUTPUT, tmp_path / f"s{i}.mp4"
        )
        for i, start in enumerate([2.0, 7.0, 12.0])
    ]
    dest = concat(parts, tmp_path / "joined.mp4")
    assert probe(dest).duration == pytest.approx(6.0, abs=0.4)


def test_render_clip_end_to_end(fixture_video, caption, tmp_path):
    segments = [Segment(2.0, 2.0), Segment(7.0, 2.5), Segment(17.0, 2.0)]
    dest = render_clip(
        fixture_video, segments, caption, OUTPUT, tmp_path / "clip.mp4", tmp_path / "scratch"
    )
    info = probe(dest)
    assert info.has_audio is False
    assert (info.width, info.height) == (854, 480)
    assert info.duration == pytest.approx(6.5, abs=0.4)


def test_render_clip_cleans_up_scratch(fixture_video, caption, tmp_path):
    scratch = tmp_path / "scratch"
    render_clip(
        fixture_video, [Segment(2.0, 2.0)], caption, OUTPUT, tmp_path / "clip.mp4", scratch
    )
    assert not scratch.exists()


def test_render_clip_cleans_up_scratch_on_mid_list_encode_failure(
    fixture_video, caption, tmp_path, monkeypatch
):
    # A failure partway through the segment loop -- not just on the first
    # or last one -- should still clear scratch. (dest is untouched here:
    # encode_segment never writes to it, only to scratch; see
    # test_render_clip_preserves_dest_when_encode_fails for that guarantee.)
    scratch = tmp_path / "scratch"

    real_encode_segment = encode_segment
    calls = []

    def flaky_encode_segment(video, segment, caption_png, output, seg_dest):
        calls.append(segment)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real_encode_segment(video, segment, caption_png, output, seg_dest)

    monkeypatch.setattr("cutlist.media.render.encode_segment", flaky_encode_segment)

    segments = [Segment(2.0, 2.0), Segment(7.0, 2.0), Segment(12.0, 2.0)]
    with pytest.raises(RuntimeError):
        render_clip(fixture_video, segments, caption, OUTPUT, tmp_path / "clip.mp4", scratch)

    assert not scratch.exists()


def test_render_clip_preserves_dest_when_encode_fails(
    fixture_video, caption, tmp_path, monkeypatch
):
    # dest is only ever written by concat(), and only by an os.replace of a
    # complete file; encode_segment writes solely into scratch. A failure
    # before concat runs must leave a pre-existing dest (e.g. a valid clip
    # from a previous render) alone.
    dest = tmp_path / "clip.mp4"
    dest.write_bytes(b"a perfectly good previous clip")
    scratch = tmp_path / "scratch"

    def failing_encode_segment(video, segment, caption_png, output, seg_dest):
        raise RuntimeError("boom")

    monkeypatch.setattr("cutlist.media.render.encode_segment", failing_encode_segment)

    with pytest.raises(RuntimeError):
        render_clip(
            fixture_video, [Segment(2.0, 2.0)], caption, OUTPUT, dest, scratch
        )

    assert dest.read_bytes() == b"a perfectly good previous clip"


def test_render_clip_preserves_dest_when_concat_fails(
    fixture_video, caption, tmp_path, monkeypatch
):
    """`rerender` writes back over a clip that already carries a verdict.

    A failed rebuild that deletes it destroys the judgement too, and leaves
    the user with `rate`'s advice to rerender -- the operation that did it.
    """
    dest = tmp_path / "clip.mp4"
    dest.write_bytes(b"a perfectly good previous clip")
    scratch = tmp_path / "scratch"

    def failing_concat(parts, dest):
        raise RuntimeError("boom")

    monkeypatch.setattr("cutlist.media.render.concat", failing_concat)

    with pytest.raises(RuntimeError):
        render_clip(
            fixture_video, [Segment(2.0, 2.0)], caption, OUTPUT, dest, scratch
        )

    assert dest.read_bytes() == b"a perfectly good previous clip"


def _streamless_part(video: Path, tmp_path: Path) -> Path:
    """An .mp4 with no video stream, produced the way the real bug produced one.

    Seeking past the end of a source exits 0 and writes a container holding
    nothing. It is only when concat tries to open its output from that input
    that ffmpeg fails -- which is exactly the shape a `rerender` against a
    truncated or re-encoded source takes.
    """
    part = tmp_path / "empty_part.mp4"
    run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", "999.0", "-i", str(video), "-t", "2.0", "-an",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(part),
    ])
    return part


def test_concat_leaves_a_pre_existing_dest_byte_identical_when_it_fails(
    fixture_video, tmp_path
):
    """The failure has to happen inside concat, not before it.

    encode_segment writes only into scratch, so injecting there proves
    nothing about dest. This drives the real ffmpeg into the real failure.
    """
    dest = tmp_path / "clip.mp4"
    original = b"a rated clip nobody asked to lose"
    dest.write_bytes(original)

    with pytest.raises(ToolError):
        concat([_streamless_part(fixture_video, tmp_path)], dest)

    assert dest.read_bytes() == original


def test_concat_leaves_no_staging_file_behind(fixture_video, caption, tmp_path):
    """On both paths: the temporary is a sibling of dest, so litter is visible."""
    out = tmp_path / "out"
    out.mkdir()
    part = encode_segment(
        fixture_video, Segment(2.0, 2.0), caption, OUTPUT, tmp_path / "s0.mp4"
    )

    concat([part], out / "clip.mp4")
    assert sorted(p.name for p in out.iterdir()) == ["clip.mp4"]

    with pytest.raises(ToolError):
        concat([_streamless_part(fixture_video, tmp_path)], out / "clip.mp4")
    assert sorted(p.name for p in out.iterdir()) == ["clip.mp4"]


def test_concat_handles_apostrophe_in_path(fixture_video, caption, tmp_path):
    root = tmp_path / "Bob's Movies"
    dest = render_clip(
        fixture_video,
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


def test_render_clip_content_matches_source_segments(fixture_video, caption, tmp_path):
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
        fixture_video, segments, caption, OUTPUT, tmp_path / "clip.mp4", tmp_path / "scratch"
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


def test_concat_never_points_ffmpeg_at_dest_itself(
    fixture_video, caption, tmp_path, monkeypatch
):
    """Pin the mechanism, not only the outcome.

    Aiming ffmpeg straight at dest is what let a failed rerender destroy a
    rated clip, and it is the shape a later simplification would drift back
    to. The staging file has to be a sibling of dest: os.replace is atomic
    only within one filesystem, and the caller's scratch lives under cache/,
    which need not share one with output/.
    """
    dest = tmp_path / "out" / "clip.mp4"
    part = encode_segment(
        fixture_video, Segment(2.0, 2.0), caption, OUTPUT, tmp_path / "s0.mp4"
    )

    outputs = []
    real_run = render.run

    def spying(cmd, **kwargs):
        outputs.append(Path(cmd[-1]))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("cutlist.media.render.run", spying)
    concat([part], dest)

    assert len(outputs) == 1
    assert outputs[0] != dest
    assert outputs[0].parent == dest.parent
    assert dest.exists()
