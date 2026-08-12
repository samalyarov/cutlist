import pytest
from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.feedback.rate import parse_segment_marks

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_video(conn, video_hash="abc", display_name="fixture.mp4", duration_s=30.0)
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="sha", preset_json="{}",
        caption_text="TEST", seed=1, cutlist_version="0.1.0", video_hashes=["abc"],
    )
    store.record_clip(
        conn, run_id=run_id, ordinal=1, path="output/01.mp4", duration_s=4.0,
        segments=[
            store.SegmentRecord("abc", 1.0, 3.0, 0.5, 3.5, 0),
            store.SegmentRecord("abc", 8.0, 10.0, 7.5, 10.5, 1),
        ],
    )
    return tmp_path


def test_parse_segment_marks_reads_pairs():
    assert parse_segment_marks("1:good,3:veto") == [(1, "good"), (3, "veto")]


def test_parse_segment_marks_tolerates_spaces():
    assert parse_segment_marks(" 2 : bad ") == [(2, "bad")]


@pytest.mark.parametrize("text", ["1", "1:", ":good", "1:sideways", "x:good", "1:good,"])
def test_parse_segment_marks_rejects_malformed_input(text):
    with pytest.raises(ValueError):
        parse_segment_marks(text)


def test_rate_records_a_verdict(workspace):
    result = runner.invoke(app, [
        "rate", "output/01.mp4", "fire", "--root", str(workspace),
    ])
    assert result.exit_code == 0, result.output
    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT verdict FROM clip_rating").fetchone()[0] == "fire"


def test_rate_records_segment_marks(workspace):
    result = runner.invoke(app, [
        "rate", "output/01.mp4", "ok", "--segments", "1:good,2:veto",
        "--root", str(workspace),
    ])
    assert result.exit_code == 0, result.output
    conn = connect(workspace / "cutlist.sqlite")
    marks = [r["mark"] for r in conn.execute(
        "SELECT mark FROM shot_rating ORDER BY seg_start_s"
    )]
    assert marks == ["good", "veto"]


def test_rate_rejects_an_unknown_clip(workspace):
    result = runner.invoke(app, [
        "rate", "output/nope.mp4", "fire", "--root", str(workspace),
    ])
    assert result.exit_code == 1
    assert "nope.mp4" in result.output


def test_rate_rejects_an_out_of_range_segment(workspace):
    result = runner.invoke(app, [
        "rate", "output/01.mp4", "ok", "--segments", "9:good", "--root", str(workspace),
    ])
    assert result.exit_code == 1
    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM shot_rating").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clip_rating").fetchone()[0] == 0


def test_ratings_reports_counts(workspace):
    runner.invoke(app, ["rate", "output/01.mp4", "fire", "--root", str(workspace)])
    result = runner.invoke(app, ["ratings", "--root", str(workspace)])
    assert result.exit_code == 0
    assert "fire" in result.output


def test_ratings_json_is_machine_readable(workspace):
    import json
    runner.invoke(app, ["rate", "output/01.mp4", "no", "--root", str(workspace)])
    result = runner.invoke(app, ["ratings", "--json", "--root", str(workspace)])
    assert json.loads(result.output)["verdicts"]["no"] == 1


def test_handled_errors_does_not_swallow_bare_value_or_lookup_errors():
    """HANDLED_ERRORS is a curated allowlist, not bare ValueError/LookupError.

    Widening it to the bare builtins would also catch concat()'s "nothing to
    concatenate" invariant check and probe.py's unguarded ffprobe parsing --
    both real bugs that should traceback, not report as a clean one-liner.
    A future re-widening should fail this test loudly rather than pass by
    accident.
    """
    from cutlist.cli import HANDLED_ERRORS

    assert not issubclass(ValueError, HANDLED_ERRORS)
    assert not issubclass(LookupError, HANDLED_ERRORS)
