import json
from pathlib import Path

from typer.testing import CliRunner

import cutlist.cli as cli
from cutlist.cli import app
from cutlist.media.caption import FontError
from cutlist.media.probe import probe
from cutlist.shell import ToolError

runner = CliRunner()


def test_probe_command_reports_dimensions(fixture_video):
    result = runner.invoke(app, ["probe", str(fixture_video)])
    assert result.exit_code == 0
    assert "320x240" in result.stdout


def test_shots_command_counts_shots(fixture_video):
    result = runner.invoke(app, ["shots", str(fixture_video)])
    assert result.exit_code == 0
    assert "6 shots" in result.stdout


def test_shots_command_can_emit_json(fixture_video):
    result = runner.invoke(app, ["shots", str(fixture_video), "--json"])
    assert result.exit_code == 0
    shots = json.loads(result.stdout)
    assert len(shots) == 6
    assert {"index", "start", "end"} <= shots[0].keys()


def test_shots_command_succeeds_on_a_cut_free_video(cutfree_video):
    # Used to IndexError on lengths[len(lengths) // 2] because detect_shots
    # returned [] for a video with no cuts -- an unhandled crash rather than
    # the clean `error:` line this command boundary exists to guarantee.
    result = runner.invoke(app, ["shots", str(cutfree_video)])
    assert result.exit_code == 0, result.output
    assert "1 shots" in result.stdout
    assert "Traceback" not in result.output


def test_draft_writes_playable_clips(fixture_video, tmp_path):
    result = runner.invoke(app, [
        "draft", str(fixture_video),
        "--preset", "presets/sample_preset.yaml",
        "--count", "2",
        "--root", str(tmp_path),
        "--seed", "0",
    ])
    assert result.exit_code == 0, result.stdout

    # "1" is the run id: a fresh workspace means this draft opens run 1, and
    # each run gets its own output directory.
    clips = sorted(
        (tmp_path / "output" / fixture_video.stem / "sample_preset" / "1").glob("*.mp4")
    )
    assert len(clips) == 2
    for clip in clips:
        info = probe(clip)
        assert info.has_audio is False
        assert (info.width, info.height) == (854, 480)
        assert 9.0 <= info.duration <= 15.5


def test_draft_clips_returns_the_run_scoped_output_directory(tmp_path, fixture_video):
    from cutlist.cli import _draft_clips
    from cutlist.db.schema import connect
    from cutlist.paths import Workspace
    from cutlist.presets import load_preset

    preset_path = Path("presets/sample_preset.yaml")
    spec = load_preset(preset_path)
    workspace = Workspace(root=tmp_path)
    conn = connect(workspace.database)

    destination = _draft_clips(
        conn, video=fixture_video, spec=spec, workspace=workspace,
        count=1, seed=7, preset_path=preset_path,
    )

    run_id = conn.execute("SELECT id FROM run").fetchone()[0]
    assert destination == workspace.output / fixture_video.stem / spec.name / str(run_id)
    assert (destination / "01.mp4").exists()


def test_draft_caption_override_is_accepted(fixture_video, tmp_path):
    result = runner.invoke(app, [
        "draft", str(fixture_video),
        "--preset", "presets/sample_preset.yaml",
        "--caption", "ДРУГОЙ ТЕКСТ",
        "--count", "1",
        "--root", str(tmp_path),
        "--seed", "0",
    ])
    assert result.exit_code == 0, result.stdout
    assert "ДРУГОЙ ТЕКСТ" in result.stdout


def test_draft_caption_pngs_do_not_collide_across_runs(fixture_video, tmp_path, monkeypatch):
    # caption.png is keyed per run, not per video: concurrent drafts of the
    # same video with different captions must not overwrite each other's PNG.
    seen_paths = []
    original = cli.render_caption

    def spying(spec, output, dest):
        seen_paths.append(dest)
        return original(spec, output, dest)

    monkeypatch.setattr(cli, "render_caption", spying)

    for text in ("FIRST CAPTION", "SECOND CAPTION"):
        result = runner.invoke(app, [
            "draft", str(fixture_video),
            "--preset", "presets/sample_preset.yaml",
            "--caption", text,
            "--count", "1",
            "--root", str(tmp_path),
            "--seed", "0",
        ])
        assert result.exit_code == 0, result.stdout

    assert len(seen_paths) == 2
    assert seen_paths[0] != seen_paths[1]
    shared_path = cli.Workspace(root=tmp_path).cache_for(fixture_video) / "caption.png"
    assert shared_path not in seen_paths


def test_missing_video_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["probe", str(tmp_path / "nope.mp4")])
    assert result.exit_code != 0
    assert "error: no such file" in result.output
    assert "Traceback" not in result.output


def test_shots_missing_video_exits_nonzero_with_clean_error(tmp_path):
    result = runner.invoke(app, ["shots", str(tmp_path / "nope.mp4")])
    assert result.exit_code != 0
    assert "error: no such file" in result.output
    assert "Traceback" not in result.output


def test_draft_missing_video_exits_nonzero_with_clean_error(tmp_path):
    result = runner.invoke(app, [
        "draft", str(tmp_path / "nope.mp4"),
        "--preset", "presets/sample_preset.yaml",
    ])
    assert result.exit_code != 0
    assert "error: no such file" in result.output
    assert "Traceback" not in result.output


def test_draft_missing_preset_reports_clean_error(fixture_video, tmp_path):
    result = runner.invoke(app, [
        "draft", str(fixture_video),
        "--preset", str(tmp_path / "nope.yaml"),
    ])
    assert result.exit_code != 0
    assert "error: no such file" in result.output
    assert "Traceback" not in result.output


def test_draft_malformed_preset_reports_clean_error(fixture_video, tmp_path):
    bad_preset = tmp_path / "bad.yaml"
    bad_preset.write_text("caption:\n  text: ''\n", encoding="utf-8")

    result = runner.invoke(app, ["draft", str(fixture_video), "--preset", str(bad_preset)])
    assert result.exit_code != 0
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_draft_font_error_reports_clean_error(fixture_video, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise FontError("no usable font found")

    monkeypatch.setattr(cli, "render_caption", boom)

    result = runner.invoke(app, [
        "draft", str(fixture_video),
        "--preset", "presets/sample_preset.yaml",
        "--root", str(tmp_path),
    ])
    assert result.exit_code != 0
    assert "error: no usable font found" in result.output
    assert "Traceback" not in result.output


def test_draft_reports_partial_progress_on_mid_loop_failure(fixture_video, tmp_path, monkeypatch):
    original = cli.render_clip
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ToolError("ffmpeg blew up")
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "render_clip", flaky)

    result = runner.invoke(app, [
        "draft", str(fixture_video),
        "--preset", "presets/sample_preset.yaml",
        "--count", "3",
        "--root", str(tmp_path),
        "--seed", "0",
    ])
    assert result.exit_code != 0
    assert "wrote 1 of 3 clips; failed on 02: ffmpeg blew up" in result.output
    assert "Traceback" not in result.output


def test_help_still_works_after_error_boundary():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "probe" in result.stdout
    assert "shots" in result.stdout
    assert "draft" in result.stdout


def test_draft_help_lists_all_options():
    result = runner.invoke(app, ["draft", "--help"])
    assert result.exit_code == 0
    for opt in ("--preset", "--count", "--caption", "--root", "--seed"):
        assert opt in result.stdout
