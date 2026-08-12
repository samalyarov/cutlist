import json

import pytest
from typer.testing import CliRunner

import cutlist.cli as cli
from cutlist.cli import app
from cutlist.db import store
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


def _draft(fixture_video, preset_file, root, extra=()):
    return runner.invoke(app, [
        "draft", str(fixture_video),
        "--preset", str(preset_file),
        "--count", "2",
        "--root", str(root),
        *extra,
    ])


def test_draft_records_a_run_with_its_source(fixture_video, preset_file, tmp_path):
    result = _draft(fixture_video, preset_file, tmp_path)
    assert result.exit_code == 0, result.output

    conn = connect(tmp_path / "cutlist.sqlite")
    run = conn.execute("SELECT * FROM run").fetchone()
    assert run["preset_name"] == "test_preset"
    assert run["caption_text"] == "TEST"
    assert conn.execute(
        "SELECT COUNT(*) FROM run_video WHERE run_id = ?", (run["id"],)
    ).fetchone()[0] == 1


def test_draft_always_records_a_seed_even_when_none_was_given(
    fixture_video, preset_file, tmp_path
):
    _draft(fixture_video, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    assert conn.execute("SELECT seed FROM run").fetchone()["seed"] is not None


def test_draft_records_the_supplied_seed(fixture_video, preset_file, tmp_path):
    _draft(fixture_video, preset_file, tmp_path, extra=["--seed", "1234"])
    conn = connect(tmp_path / "cutlist.sqlite")
    assert conn.execute("SELECT seed FROM run").fetchone()["seed"] == 1234


def test_draft_stores_the_resolved_preset(fixture_video, preset_file, tmp_path):
    _draft(fixture_video, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    stored = json.loads(conn.execute("SELECT preset_json FROM run").fetchone()[0])
    assert stored["name"] == "test_preset"
    assert stored["rhythm"]["min_segments"] == 2


def test_draft_records_a_clip_row_per_rendered_file(
    fixture_video, preset_file, tmp_path
):
    _draft(fixture_video, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    clips = conn.execute("SELECT * FROM clip ORDER BY ordinal").fetchall()
    assert [c["ordinal"] for c in clips] == [1, 2]
    for clip in clips:
        assert (tmp_path / clip["path"]).exists()


def test_two_drafts_of_the_same_video_and_preset_do_not_share_files(
    fixture_video, preset_file, tmp_path
):
    # Clips are named by ordinal: two runs sharing one output directory would
    # overwrite each other's files while their `clip` rows still claimed them.
    assert _draft(fixture_video, preset_file, tmp_path).exit_code == 0
    assert _draft(fixture_video, preset_file, tmp_path).exit_code == 0

    conn = connect(tmp_path / "cutlist.sqlite")
    rows = conn.execute("SELECT run_id, path FROM clip").fetchall()
    assert len({row["run_id"] for row in rows}) == 2

    paths = [row["path"] for row in rows]
    assert len(paths) == 4
    assert len(set(paths)) == 4
    for path in paths:
        assert (tmp_path / path).is_file()


def test_after_two_drafts_each_path_resolves_to_its_own_run(
    fixture_video, preset_file, tmp_path
):
    # clip_by_path must resolve to the run that actually owns the path: an
    # ambiguous path would let a verdict land on a different run's segments.
    assert _draft(fixture_video, preset_file, tmp_path).exit_code == 0
    assert _draft(fixture_video, preset_file, tmp_path).exit_code == 0

    conn = connect(tmp_path / "cutlist.sqlite")
    for expected in conn.execute("SELECT * FROM clip").fetchall():
        found = store.clip_by_path(conn, expected["path"])
        assert found["id"] == expected["id"]
        assert found["run_id"] == expected["run_id"]

        # ...and the segments reached through that path are this clip's own.
        segments = conn.execute(
            "SELECT clip_id FROM segment WHERE clip_id = ?", (found["id"],)
        ).fetchall()
        assert segments
        assert {row["clip_id"] for row in segments} == {expected["id"]}


def test_every_recorded_segment_lies_inside_its_shot(
    fixture_video, preset_file, tmp_path
):
    _draft(fixture_video, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    rows = conn.execute("SELECT * FROM segment").fetchall()
    assert rows
    for row in rows:
        assert row["shot_start_s"] <= row["seg_start_s"]
        assert row["seg_end_s"] <= row["shot_end_s"]


def test_recorded_segment_durations_match_the_clip_duration(
    fixture_video, preset_file, tmp_path
):
    _draft(fixture_video, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    for clip in conn.execute("SELECT * FROM clip"):
        total = conn.execute(
            "SELECT SUM(seg_end_s - seg_start_s) FROM segment WHERE clip_id = ?",
            (clip["id"],),
        ).fetchone()[0]
        assert abs(total - clip["duration_s"]) < 0.05


def test_a_mid_loop_failure_still_records_the_run_and_the_clips_that_landed(
    fixture_video, preset_file, tmp_path, monkeypatch
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

    result = _draft(fixture_video, preset_file, tmp_path, extra=["--count", "3"])

    # Confirm this is genuinely a mid-loop failure, not one at startup.
    assert result.exit_code != 0
    assert "wrote 1 of 3 clips; failed on 02: ffmpeg blew up" in result.output

    conn = connect(tmp_path / "cutlist.sqlite")

    runs = conn.execute("SELECT * FROM run").fetchall()
    assert len(runs) == 1
    run = runs[0]

    run_videos = conn.execute(
        "SELECT * FROM run_video WHERE run_id = ?", (run["id"],)
    ).fetchall()
    assert len(run_videos) == 1

    clips = conn.execute("SELECT * FROM clip").fetchall()
    assert len(clips) == 1
    clip = clips[0]

    segments = conn.execute(
        "SELECT * FROM segment WHERE clip_id = ?", (clip["id"],)
    ).fetchall()
    assert segments
