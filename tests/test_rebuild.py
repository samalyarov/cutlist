import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.media.caption import render_caption
from cutlist.media.probe import probe
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


@pytest.fixture
def assembled_across_two_sources(tmp_path, fixture_video, cutfree_video):
    """One assembled clip whose segments come from two different videos.

    Before v1.2 this was unreachable from the CLI. `assemble` naming ids from
    two sources now builds one in a single command, which is what turned
    rerender's refusal from unreachable into a trap.
    """
    (tmp_path / "input").mkdir()
    for original, name in (
        (fixture_video, "fixture.mp4"), (cutfree_video, "cutfree.mp4")
    ):
        copied = tmp_path / "input" / name
        shutil.copy(original, copied)
        assert CliRunner().invoke(
            app, ["extract", str(copied), "--root", str(tmp_path)]
        ).exit_code == 0

    conn = connect(tmp_path / "cutlist.sqlite")
    first, second = (
        conn.execute(
            "SELECT library_clip.id FROM library_clip JOIN video "
            "ON video.video_hash = library_clip.video_hash "
            "WHERE video.display_name = ? ORDER BY library_clip.id",
            (name,),
        ).fetchone()[0]
        for name in ("fixture.mp4", "cutfree.mp4")
    )

    result = CliRunner().invoke(
        app,
        ["assemble", f"{first},{second},{first}",
         "--preset", "presets/sample_preset.yaml", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    clip = conn.execute("SELECT id, path FROM clip ORDER BY id DESC LIMIT 1").fetchone()
    spanned = conn.execute(
        "SELECT COUNT(DISTINCT video_hash) FROM segment WHERE clip_id = ?",
        (clip["id"],),
    ).fetchone()[0]
    assert spanned == 2, "the fixture has to actually span two sources"
    return tmp_path, conn, clip["id"], tmp_path / clip["path"]


def test_rerender_rebuilds_a_clip_drawn_from_more_than_one_video(
    assembled_across_two_sources,
):
    """Refusing this deadlocked the pair: rate sent a missing clip to
    rerender, rerender sent it back, and the clip was permanently unratable
    and unrecoverable. assemble is itself a multi-source renderer -- it
    encodes each part from a different file and concatenates -- so there was
    never anything here rerender could not rebuild the same way.
    """
    root, conn, clip_id, path = assembled_across_two_sources
    original = probe(path).duration
    path.unlink()

    rebuilt = rebuild_clip(conn, root=root, clip_id=clip_id)

    assert rebuilt == path
    assert path.exists()
    assert probe(path).duration == pytest.approx(original, abs=0.5)


def test_a_missing_multi_source_clip_can_be_rerendered_and_then_rated(
    assembled_across_two_sources,
):
    """The deadlock, walked end to end through the CLI that created it."""
    root, conn, clip_id, path = assembled_across_two_sources
    relative = path.relative_to(root).as_posix()
    path.unlink()

    refused = CliRunner().invoke(app, ["rate", relative, "fire", "--root", str(root)])
    assert refused.exit_code == 1
    assert "rerender it first" in refused.output

    rebuilt = CliRunner().invoke(app, ["rerender", relative, "--root", str(root)])
    assert rebuilt.exit_code == 0, rebuilt.output

    rated = CliRunner().invoke(app, ["rate", relative, "fire", "--root", str(root)])
    assert rated.exit_code == 0, rated.output
    assert store.clip_detail(conn, clip_id)["verdict"] == "fire"


def test_rerender_names_which_of_several_sources_is_missing(
    assembled_across_two_sources,
):
    """"a source is missing" is not actionable when a clip has two. Every
    source is resolved before anything is encoded, so nothing is written."""
    root, conn, clip_id, path = assembled_across_two_sources
    path.unlink()
    (root / "input" / "cutfree.mp4").unlink()

    with pytest.raises(RebuildError, match="cutfree.mp4"):
        rebuild_clip(conn, root=root, clip_id=clip_id)

    assert not path.exists()


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


def test_a_clip_another_program_holds_open_fails_as_one_error_line(drafted, monkeypatch):
    """The review page streams clips, including the one being rerendered.

    On Windows os.replace cannot overwrite a file another process has open,
    and PermissionError is an OSError but not a FileNotFoundError, so it used
    to reach the user as a traceback. It always failed safe -- this is about
    what the failure looks like, and about the staging file not being left
    behind in the user's output directory.

    Raised from a stub rather than a real open handle because POSIX replaces
    an open file happily: a real handle would prove nothing on the platform
    CI runs on. Narrowed to this one destination so nothing else in the
    process loses os.replace.
    """
    root, conn, clip_id, path = drafted
    before = path.read_bytes()
    relative = path.relative_to(root).as_posix()
    real_replace = os.replace

    def busy(src, dst):
        if Path(dst) == path:
            raise PermissionError(13, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr("cutlist.media.render.os.replace", busy)

    result = CliRunner().invoke(app, ["rerender", relative, "--root", str(root)])

    assert result.exit_code == 1
    assert not isinstance(result.exception, PermissionError), result.exception
    assert "another program has it open" in result.output
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".*.mp4"))
