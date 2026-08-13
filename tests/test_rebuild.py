import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.media.caption import render_caption
from cutlist.media.render import Segment, render_clip
from cutlist.presets import OutputSpec, load_preset, preset_from_dict
from cutlist.rebuild import RebuildError, rebuild_clip
from cutlist.shell import ToolError


@pytest.fixture
def drafted(tmp_path, fixture_video):
    """A real draft: one clip on disk with its provenance recorded."""
    (tmp_path / "input").mkdir()
    shutil.copy(fixture_video, tmp_path / "input" / "fixture.mp4")
    result = CliRunner().invoke(
        app,
        ["draft", str(tmp_path / "input" / "fixture.mp4"),
         "--preset", "presets/sample_preset.yaml",
         "--count", "1", "--seed", "3", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    conn = connect(tmp_path / "cutlist.sqlite")
    clip = conn.execute("SELECT id, path FROM clip").fetchone()
    return tmp_path, conn, clip["id"], tmp_path / clip["path"]


def test_preset_from_dict_round_trips_a_serialised_preset():
    from dataclasses import asdict

    original = load_preset(Path("presets/sample_preset.yaml"))
    assert preset_from_dict(asdict(original)) == original


def test_rerender_recreates_a_deleted_clip_at_its_recorded_path(drafted):
    root, conn, clip_id, path = drafted
    original_size = path.stat().st_size
    path.unlink()

    rebuilt = rebuild_clip(conn, root=root, clip_id=clip_id)

    assert rebuilt == path
    assert path.exists()
    # Perceptually identical, not byte-identical: encoder builds differ.
    assert path.stat().st_size == pytest.approx(original_size, rel=0.25)


def test_rerender_reports_a_missing_source(drafted):
    root, conn, clip_id, path = drafted
    path.unlink()
    for source in (root / "input").iterdir():
        source.unlink()

    with pytest.raises(RebuildError, match="source video"):
        rebuild_clip(conn, root=root, clip_id=clip_id)


def test_rerender_refuses_a_clip_drawn_from_more_than_one_video(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_video(conn, video_hash="a", display_name="a.mp4")
    store.record_video(conn, video_hash="b", display_name="b.mp4")
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="s", preset_json="{}",
        caption_text="c", seed=1, cutlist_version="0.1.0", video_hashes=["a", "b"],
    )
    clip_id = store.record_clip(
        conn, run_id=run_id, ordinal=1, path="o/01.mp4", duration_s=4.0,
        segments=[
            store.SegmentRecord("a", 1.0, 3.0, 0.0, 4.0, 0),
            store.SegmentRecord("b", 1.0, 3.0, 0.0, 4.0, 1),
        ],
    )
    with pytest.raises(RebuildError, match="more than one source"):
        rebuild_clip(conn, root=tmp_path, clip_id=clip_id)


def test_the_rerendered_clip_keeps_its_ratings(drafted):
    """The point of writing back to the same path."""
    root, conn, clip_id, path = drafted
    store.rate_clip(conn, clip_id=clip_id, verdict="fire")
    path.unlink()

    rebuild_clip(conn, root=root, clip_id=clip_id)

    # Both halves, or the test passes on a rebuild that wrote nothing: the
    # verdict survives trivially if the file was never restored.
    assert path.exists()
    assert store.clip_detail(conn, clip_id)["verdict"] == "fire"


def test_the_rerender_command_reports_what_it_wrote(drafted):
    root, conn, clip_id, path = drafted
    relative = path.relative_to(root).as_posix()
    path.unlink()

    result = CliRunner().invoke(app, ["rerender", relative, "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert relative in result.output
    assert path.exists()


def test_the_rerender_command_reports_an_unknown_clip(tmp_path):
    result = CliRunner().invoke(
        app, ["rerender", "output/nope/1/01.mp4", "--root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "no recorded clip" in result.output


def _reencode_over(source: Path, seconds: float) -> None:
    """Replace a source in place with different bytes under the same name.

    Not contrived: the container's ffmpeg and the host's build the demo source
    to different bytes, so drafting in Docker and rerendering natively reaches
    this by following the README.
    """
    from cutlist.shell import run

    run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=0x0000FF:s=320x240:d={seconds}:r=25",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ])


def test_rerender_refuses_a_source_that_only_matches_by_name(drafted):
    """A same-named different video would put unwatched footage under a verdict.

    find_source will happily fall back to the display name -- useful for a
    thumbnail, wrong here -- so the strength of the match has to be checked.
    """
    root, conn, clip_id, path = drafted
    store.rate_clip(conn, clip_id=clip_id, verdict="fire")
    before = path.read_bytes()
    _reencode_over(root / "input" / "fixture.mp4", seconds=30.0)

    with pytest.raises(RebuildError, match="right name.*not the right content"):
        rebuild_clip(conn, root=root, clip_id=clip_id)

    assert path.read_bytes() == before


def test_a_refused_rerender_does_not_destroy_the_rated_clip(drafted):
    """C1: the rebuild fails deep in ffmpeg, after dest would once have been unlinked.

    A source too short for the recorded segments encodes to parts with no
    stream, which concat cannot open an output from. The rated clip has to
    survive that intact -- `rate`'s own advice on a missing clip is to
    rerender it, so destroying it here leaves no way back.
    """
    root, conn, clip_id, path = drafted
    store.rate_clip(conn, clip_id=clip_id, verdict="fire")
    before = path.read_bytes()

    # Same content hash is impossible to fake, so reach concat directly with
    # the source the record names, truncated to nothing useful.
    source = root / "input" / "fixture.mp4"
    detail = store.clip_detail(conn, clip_id)
    segments = [
        Segment(s["seg_start_s"], s["seg_end_s"] - s["seg_start_s"])
        for s in detail["segments"]
    ]
    _reencode_over(source, seconds=0.5)

    with pytest.raises(ToolError):
        render_clip(
            source, segments,
            render_caption(
                load_preset(Path("presets/sample_preset.yaml")).caption,
                OutputSpec(), root / "cap.png",
            ),
            OutputSpec(), path, root / "scratch",
        )

    assert path.read_bytes() == before
