import shutil

import pytest
from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.presets import load_preset, preset_from_dict
from cutlist.rebuild import RebuildError, rebuild_clip


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

    original = load_preset("presets/sample_preset.yaml")
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
