import json

import pytest
from typer.testing import CliRunner

import cutlist.cli as cli
from cutlist.cli import app
from cutlist.db.schema import connect
from cutlist.shell import ToolError

runner = CliRunner()

PRESET = """
name: test_preset
caption:
  text: "TEST"
rhythm:
  segments: {min: 2, max: 3}
  seg_duration: {min: 1.0, target: 1.5, max: 2.0}
  total: {min: 3.0, max: 6.0}
output:
  width: 160
  height: 120
  fps: 25
  crf: 30
"""


@pytest.fixture
def preset_file(tmp_path):
    path = tmp_path / "test_preset.yaml"
    path.write_text(PRESET, encoding="utf-8")
    return path


def _draft(fixture_film, preset_file, root, extra=()):
    return runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", str(preset_file),
        "--count", "2",
        "--root", str(root),
        *extra,
    ])


def test_draft_records_a_run_with_its_source(fixture_film, preset_file, tmp_path):
    result = _draft(fixture_film, preset_file, tmp_path)
    assert result.exit_code == 0, result.output

    conn = connect(tmp_path / "cutlist.sqlite")
    run = conn.execute("SELECT * FROM run").fetchone()
    assert run["preset_name"] == "test_preset"
    assert run["caption_text"] == "TEST"
    assert conn.execute(
        "SELECT COUNT(*) FROM run_film WHERE run_id = ?", (run["id"],)
    ).fetchone()[0] == 1


def test_draft_always_records_a_seed_even_when_none_was_given(
    fixture_film, preset_file, tmp_path
):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    assert conn.execute("SELECT seed FROM run").fetchone()["seed"] is not None


def test_draft_records_the_supplied_seed(fixture_film, preset_file, tmp_path):
    _draft(fixture_film, preset_file, tmp_path, extra=["--seed", "1234"])
    conn = connect(tmp_path / "cutlist.sqlite")
    assert conn.execute("SELECT seed FROM run").fetchone()["seed"] == 1234


def test_draft_stores_the_resolved_preset(fixture_film, preset_file, tmp_path):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    stored = json.loads(conn.execute("SELECT preset_json FROM run").fetchone()[0])
    assert stored["name"] == "test_preset"
    assert stored["rhythm"]["min_segments"] == 2


def test_draft_records_a_clip_row_per_rendered_file(
    fixture_film, preset_file, tmp_path
):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    clips = conn.execute("SELECT * FROM clip ORDER BY ordinal").fetchall()
    assert [c["ordinal"] for c in clips] == [1, 2]
    for clip in clips:
        assert (tmp_path / clip["path"]).exists()


def test_every_recorded_segment_lies_inside_its_shot(
    fixture_film, preset_file, tmp_path
):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    rows = conn.execute("SELECT * FROM segment").fetchall()
    assert rows
    for row in rows:
        assert row["shot_start_s"] <= row["seg_start_s"]
        assert row["seg_end_s"] <= row["shot_end_s"]


def test_recorded_segment_durations_match_the_clip_duration(
    fixture_film, preset_file, tmp_path
):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    for clip in conn.execute("SELECT * FROM clip"):
        total = conn.execute(
            "SELECT SUM(seg_end_s - seg_start_s) FROM segment WHERE clip_id = ?",
            (clip["id"],),
        ).fetchone()[0]
        assert abs(total - clip["duration_s"]) < 0.05


def test_a_mid_loop_failure_still_records_the_run_and_the_clips_that_landed(
    fixture_film, preset_file, tmp_path, monkeypatch
):
    # Same flaky-render mechanism as
    # test_draft_reports_partial_progress_on_mid_loop_failure in
    # tests/test_cli.py: let the first render through, fail the second.
    original = cli.render_clip
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ToolError("ffmpeg blew up")
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "render_clip", flaky)

    result = _draft(fixture_film, preset_file, tmp_path, extra=["--count", "3"])

    # Confirm this is genuinely a mid-loop failure, not one at startup.
    assert result.exit_code != 0
    assert "wrote 1 of 3 clips; failed on 02: ffmpeg blew up" in result.output

    conn = connect(tmp_path / "cutlist.sqlite")

    runs = conn.execute("SELECT * FROM run").fetchall()
    assert len(runs) == 1
    run = runs[0]

    run_films = conn.execute(
        "SELECT * FROM run_film WHERE run_id = ?", (run["id"],)
    ).fetchall()
    assert len(run_films) == 1

    clips = conn.execute("SELECT * FROM clip").fetchall()
    assert len(clips) == 1
    clip = clips[0]

    segments = conn.execute(
        "SELECT * FROM segment WHERE clip_id = ?", (clip["id"],)
    ).fetchall()
    assert segments
