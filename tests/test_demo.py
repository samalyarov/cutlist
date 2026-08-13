import pytest
from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.db.schema import connect
from cutlist.demo import DEMO_SHOTS, build_demo_source
from cutlist.media.probe import probe
from cutlist.media.shots import detect_shots


@pytest.fixture(scope="module")
def demo_source(tmp_path_factory):
    return build_demo_source(tmp_path_factory.mktemp("demo") / "demo-source.mp4")


def test_the_demo_source_is_a_playable_video(demo_source):
    info = probe(demo_source)
    assert info.duration == pytest.approx(sum(seconds for _, seconds in DEMO_SHOTS), abs=0.5)
    assert (info.width, info.height) == (640, 360)


def test_every_intended_cut_is_detectable(demo_source):
    """If this fails the colours are too close in luma, not the detector."""
    assert len(detect_shots(demo_source)) == len(DEMO_SHOTS)


def test_demo_produces_clips_with_no_input_file(tmp_path):
    result = CliRunner().invoke(
        app, ["demo", "--root", str(tmp_path), "--count", "2"]
    )
    assert result.exit_code == 0, result.output

    clips = sorted((tmp_path / "output").rglob("*.mp4"))
    assert len(clips) == 2
    assert all(clip.stat().st_size > 0 for clip in clips)


def test_demo_records_provenance_like_any_other_draft(tmp_path):
    result = CliRunner().invoke(app, ["demo", "--root", str(tmp_path), "--count", "1"])
    assert result.exit_code == 0, result.output

    conn = connect(tmp_path / "cutlist.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM clip").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM segment").fetchone()[0] >= 4
    assert conn.execute("SELECT COUNT(*) FROM segment_thumbnail").fetchone()[0] >= 4


def test_demo_reuses_the_source_it_already_built(tmp_path):
    runner = CliRunner()
    assert runner.invoke(app, ["demo", "--root", str(tmp_path), "--count", "1"]).exit_code == 0
    source = tmp_path / "input" / "demo-source.mp4"
    stamp = source.stat().st_mtime_ns

    assert runner.invoke(app, ["demo", "--root", str(tmp_path), "--count", "1"]).exit_code == 0
    assert source.stat().st_mtime_ns == stamp


def test_demo_tells_you_what_to_run_next(tmp_path):
    result = CliRunner().invoke(app, ["demo", "--root", str(tmp_path), "--count", "1"])
    assert "cutlist review" in result.output
