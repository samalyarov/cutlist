import json

import pytest
from typer.testing import CliRunner

import cutlist.cli as cli
from cutlist.cli import app
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.shell import ToolError
from tests.conftest import FIXTURE_HEX_COLORS

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


def test_a_thumbnail_capture_failure_reports_the_ordinal_and_keeps_earlier_clips(
    fixture_video, tmp_path, monkeypatch
):
    """Capture sits inside the same failure boundary as the render now.

    Before, a ToolError from thumbnail_bytes escaped past the loop's
    try/except entirely (it was raised while building _record_picks' call,
    which sat outside the block) and was only caught by the outer
    @handle_errors boundary -- a bare "error: ..." with no ordinal and no
    partial-progress count. It never actually left a clip recorded without
    its thumbnails, because record_clip is only called once every thumbnail
    in a clip has already been captured successfully; what was missing was
    the same clear reporting a render failure already gets.
    """
    # segments.min == segments.max fixes the segment count per clip at 2,
    # regardless of the RNG draw, so "the Nth call to thumbnail_bytes" maps
    # to a known clip deterministically: calls 1-2 are the first clip's
    # thumbnails, call 3 is the second clip's first.
    preset_file = tmp_path / "fixed_segments.yaml"
    preset_file.write_text(
        """
name: test_preset
caption:
  text: "TEST"
rhythm:
  segments: {min: 2, max: 2}
  seg_duration: {min: 1.0, target: 1.5, max: 2.0}
  total: {min: 3.0, max: 6.0}
output:
  width: 160
  height: 120
  fps: 25
  crf: 30
""",
        encoding="utf-8",
    )

    original = cli.thumbnail_bytes
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 2:
            raise ToolError("ffmpeg blew up")
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "thumbnail_bytes", flaky)

    result = _draft(fixture_video, preset_file, tmp_path, extra=["--count", "3"])

    assert result.exit_code != 0
    assert "wrote 1 of 3 clips; failed on 02: ffmpeg blew up" in result.output

    conn = connect(tmp_path / "cutlist.sqlite")
    clips = conn.execute("SELECT * FROM clip").fetchall()
    assert len(clips) == 1
    assert clips[0]["ordinal"] == 1

    segments = conn.execute(
        "SELECT * FROM segment WHERE clip_id = ?", (clips[0]["id"],)
    ).fetchall()
    assert len(segments) == 2
    for segment in segments:
        assert store.segment_thumbnail(conn, segment["id"]) is not None


def test_draft_files_nothing_in_the_library_by_default(tmp_path, fixture_video):
    """The default path must be exactly what it was before the library existed."""
    result = runner.invoke(
        app,
        ["draft", str(fixture_video), "--preset", "presets/sample_preset.yaml",
         "--count", "1", "--seed", "5", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    conn = connect(tmp_path / "cutlist.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM library_clip").fetchone()[0] == 0
    assert not (tmp_path / "library").exists()


def test_keep_shots_files_the_whole_shots_not_the_trimmed_picks(tmp_path, fixture_video):
    """A shot is the same shot whichever run found it, so the library stores
    whole shots -- trimmed picks would be near-duplicates with unstable ids.

    Seed 1 draws four of the fixture's six shots. That matters: with a seed
    drawing all six, filing *every* shot rather than only the picks' would
    satisfy the subset check below and this test would prove nothing about
    which shots were chosen.
    """
    result = runner.invoke(
        app,
        ["draft", str(fixture_video), "--preset", "presets/sample_preset.yaml",
         "--count", "1", "--seed", "1", "--keep-shots", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    conn = connect(tmp_path / "cutlist.sqlite")
    library = conn.execute(
        "SELECT start_s, end_s FROM library_clip ORDER BY start_s"
    ).fetchall()
    assert library, "keep-shots should have filed something"

    # Every library entry matches a shot span, not a segment span. The segments
    # are centred trims strictly inside their shots, so a library row whose
    # bounds equalled a segment's would prove the wrong thing was stored.
    segments = conn.execute(
        "SELECT seg_start_s, seg_end_s, shot_start_s, shot_end_s FROM segment"
    ).fetchall()
    shot_spans = {(row["shot_start_s"], row["shot_end_s"]) for row in segments}
    segment_spans = {(row["seg_start_s"], row["seg_end_s"]) for row in segments}
    stored = {(row["start_s"], row["end_s"]) for row in library}

    # The draw has to be a strict subset, or "only the picks' shots" and
    # "every shot in the video" are the same set and nothing below can tell
    # them apart.
    assert len(shot_spans) < len(FIXTURE_HEX_COLORS), (
        f"seed drew {len(shot_spans)} of {len(FIXTURE_HEX_COLORS)} shots; this "
        f"test needs a strict subset to discriminate"
    )
    assert stored == shot_spans
    assert not (stored & segment_spans)


def test_a_keep_shots_failure_neither_aborts_the_draft_nor_blames_a_clip_that_landed(
    fixture_video, preset_file, tmp_path, monkeypatch
):
    """--keep-shots is a convenience, and an optional side-effect must not
    abort the primary work or lie about it.

    Sitting inside the render's try, an extraction failure reported
    "wrote 0 of 3 clips; failed on 01" while clip 01 had in fact landed --
    row, file, segments and thumbnails all present -- and swallowed the
    closing line that says where the clips were written. The ordinal in that
    message means "this one did not land"; here they all did.
    """
    def blow_up(*args, **kwargs):
        raise ToolError("ffmpeg blew up")

    monkeypatch.setattr("cutlist.library.extract_shot", blow_up)

    result = _draft(
        fixture_video, preset_file, tmp_path, extra=["--count", "3", "--keep-shots"]
    )

    assert result.exit_code == 0, result.output
    assert "wrote 3 clips to" in result.output
    assert "failed on" not in result.output
    # Reported rather than silent, and per ordinal: the user asked for the
    # library copies and did not get them.
    assert result.output.count("warning: ") == 3
    assert result.output.count("library copy skipped") == 3

    conn = connect(tmp_path / "cutlist.sqlite")
    clips = conn.execute("SELECT * FROM clip ORDER BY ordinal").fetchall()
    assert [clip["ordinal"] for clip in clips] == [1, 2, 3]
    for clip in clips:
        assert (tmp_path / clip["path"]).exists()
        segments = conn.execute(
            "SELECT id FROM segment WHERE clip_id = ?", (clip["id"],)
        ).fetchall()
        assert segments
        for segment in segments:
            assert store.segment_thumbnail(conn, segment["id"]) is not None

    # Nothing reached the library, which is exactly what was reported.
    assert conn.execute("SELECT COUNT(*) FROM library_clip").fetchone()[0] == 0


def test_keep_shots_reuses_what_extract_already_stored(tmp_path, fixture_video):
    """One way footage enters the library, so a draft after an extract adds
    nothing and re-encodes nothing."""
    assert runner.invoke(
        app, ["extract", str(fixture_video), "--root", str(tmp_path)]
    ).exit_code == 0

    conn = connect(tmp_path / "cutlist.sqlite")
    before = conn.execute("SELECT COUNT(*) FROM library_clip").fetchone()[0]
    stamps = {p: p.stat().st_mtime_ns for p in (tmp_path / "library").rglob("*.mp4")}

    result = runner.invoke(
        app,
        ["draft", str(fixture_video), "--preset", "presets/sample_preset.yaml",
         "--count", "1", "--seed", "5", "--keep-shots", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    assert conn.execute("SELECT COUNT(*) FROM library_clip").fetchone()[0] == before
    assert {p: p.stat().st_mtime_ns for p in (tmp_path / "library").rglob("*.mp4")} == stamps
