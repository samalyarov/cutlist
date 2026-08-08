import json

from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.media.probe import probe

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


def test_draft_writes_playable_clips(fixture_film, tmp_path):
    result = runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", "presets/real_saturday.yaml",
        "--count", "2",
        "--root", str(tmp_path),
        "--seed", "0",
    ])
    assert result.exit_code == 0, result.stdout

    clips = sorted((tmp_path / "output" / fixture_film.stem / "real_saturday").glob("*.mp4"))
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


def test_missing_film_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["probe", str(tmp_path / "nope.mp4")])
    assert result.exit_code != 0
