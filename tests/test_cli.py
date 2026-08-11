import json

from typer.testing import CliRunner

import cutlist.cli as cli
from cutlist.cli import app
from cutlist.media.caption import FontError
from cutlist.media.probe import probe
from cutlist.shell import ToolError

runner = CliRunner()


def test_probe_command_reports_dimensions(fixture_film):
    result = runner.invoke(app, ["probe", str(fixture_film)])
    assert result.exit_code == 0
    assert "320x240" in result.stdout


def test_shots_command_counts_shots(fixture_film):
    result = runner.invoke(app, ["shots", str(fixture_film)])
    assert result.exit_code == 0
    assert "6 shots" in result.stdout


def test_shots_command_can_emit_json(fixture_film):
    result = runner.invoke(app, ["shots", str(fixture_film), "--json"])
    assert result.exit_code == 0
    shots = json.loads(result.stdout)
    assert len(shots) == 6
    assert {"index", "start", "end"} <= shots[0].keys()


def test_shots_command_succeeds_on_a_cut_free_film(cutfree_film):
    # Used to IndexError on lengths[len(lengths) // 2] because detect_shots
    # returned [] for a video with no cuts -- an unhandled crash rather than
    # the clean `error:` line this command boundary exists to guarantee.
    result = runner.invoke(app, ["shots", str(cutfree_film)])
    assert result.exit_code == 0, result.output
    assert "1 shots" in result.stdout
    assert "Traceback" not in result.output


def test_draft_writes_playable_clips(fixture_film, tmp_path):
    result = runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", "presets/real_saturday.yaml",
        "--count", "2",
        "--root", str(tmp_path),
        "--seed", "0",
    ])
    assert result.exit_code == 0, result.stdout

    # "1" is the run id: a fresh workspace means this draft opens run 1, and
    # each run gets its own output directory.
    clips = sorted(
        (tmp_path / "output" / fixture_film.stem / "real_saturday" / "1").glob("*.mp4")
    )
    assert len(clips) == 2
    for clip in clips:
        info = probe(clip)
        assert info.has_audio is False
        assert (info.width, info.height) == (854, 480)
        assert 9.0 <= info.duration <= 15.5


def test_draft_caption_override_is_accepted(fixture_film, tmp_path):
    result = runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", "presets/real_saturday.yaml",
        "--caption", "ДРУГОЙ ТЕКСТ",
        "--count", "1",
        "--root", str(tmp_path),
        "--seed", "0",
    ])
    assert result.exit_code == 0, result.stdout
    assert "ДРУГОЙ ТЕКСТ" in result.stdout


def test_draft_caption_pngs_do_not_collide_across_runs(fixture_film, tmp_path, monkeypatch):
    # caption.png used to live at cache_for(film) / "caption.png", keyed on
    # the film alone. Two runs of the same film with different captions
    # would both reach for that one path, and the later render_caption call
    # would overwrite the PNG the earlier run's still-encoding segments were
    # reading from. Scoping it under each run's own (uuid-tagged) scratch
    # root means the two runs never touch the same file.
    seen_paths = []
    original = cli.render_caption

    def spying(spec, output, dest):
        seen_paths.append(dest)
        return original(spec, output, dest)

    monkeypatch.setattr(cli, "render_caption", spying)

    for text in ("FIRST CAPTION", "SECOND CAPTION"):
        result = runner.invoke(app, [
            "draft", str(fixture_film),
            "--preset", "presets/real_saturday.yaml",
            "--caption", text,
            "--count", "1",
            "--root", str(tmp_path),
            "--seed", "0",
        ])
        assert result.exit_code == 0, result.stdout

    assert len(seen_paths) == 2
    assert seen_paths[0] != seen_paths[1]
    shared_path = cli.Workspace(root=tmp_path).cache_for(fixture_film) / "caption.png"
    assert shared_path not in seen_paths


def test_missing_film_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["probe", str(tmp_path / "nope.mp4")])
    assert result.exit_code != 0
    assert "error: no such file" in result.output
    assert "Traceback" not in result.output


def test_shots_missing_film_exits_nonzero_with_clean_error(tmp_path):
    result = runner.invoke(app, ["shots", str(tmp_path / "nope.mp4")])
    assert result.exit_code != 0
    assert "error: no such file" in result.output
    assert "Traceback" not in result.output


def test_draft_missing_film_exits_nonzero_with_clean_error(tmp_path):
    result = runner.invoke(app, [
        "draft", str(tmp_path / "nope.mp4"),
        "--preset", "presets/real_saturday.yaml",
    ])
    assert result.exit_code != 0
    assert "error: no such file" in result.output
    assert "Traceback" not in result.output


def test_draft_missing_preset_reports_clean_error(fixture_film, tmp_path):
    result = runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", str(tmp_path / "nope.yaml"),
    ])
    assert result.exit_code != 0
    assert "error: no such file" in result.output
    assert "Traceback" not in result.output


def test_draft_malformed_preset_reports_clean_error(fixture_film, tmp_path):
    bad_preset = tmp_path / "bad.yaml"
    bad_preset.write_text("caption:\n  text: ''\n", encoding="utf-8")

    result = runner.invoke(app, ["draft", str(fixture_film), "--preset", str(bad_preset)])
    assert result.exit_code != 0
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_draft_font_error_reports_clean_error(fixture_film, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise FontError("no usable font found")

    monkeypatch.setattr(cli, "render_caption", boom)

    result = runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", "presets/real_saturday.yaml",
        "--root", str(tmp_path),
    ])
    assert result.exit_code != 0
    assert "error: no usable font found" in result.output
    assert "Traceback" not in result.output


def test_draft_reports_partial_progress_on_mid_loop_failure(fixture_film, tmp_path, monkeypatch):
    original = cli.render_clip
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ToolError("ffmpeg blew up")
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "render_clip", flaky)

    result = runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", "presets/real_saturday.yaml",
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
