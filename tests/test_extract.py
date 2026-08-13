import json
import re
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.library import estimate, extract_all, extract_shot, library_path
from cutlist.media.probe import probe
from cutlist.media.shots import Shot, detect_shots
from cutlist.paths import Workspace, video_id
from cutlist.shell import run

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path):
    return Workspace(root=tmp_path)


@pytest.fixture
def conn(workspace):
    return connect(workspace.database)


@pytest.fixture(scope="module")
def video_with_audio(tmp_path_factory):
    """A short clip with a real audio stream, to prove extraction keeps it.

    None of the shared fixtures in conftest.py carry audio -- fixture_video
    is built with `a=0` specifically so the render pipeline's own silence can
    be asserted against something. Library extraction needs the opposite: a
    source that has audio, to prove it survives.
    """
    out = tmp_path_factory.mktemp("media") / "with_audio.mp4"
    run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "color=c=0xFF0000:s=320x240:d=3:r=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(out),
    ])
    return out


# --- library.extract_shot / library.library_path -----------------------


def test_extracted_clips_keep_the_source_dimensions(tmp_path, fixture_video):
    shot = Shot(index=0, start=5.0, end=10.0)
    dest = extract_shot(fixture_video, shot, tmp_path / "shot.mp4")

    source_info = probe(fixture_video)
    clip_info = probe(dest)
    assert (clip_info.width, clip_info.height) == (source_info.width, source_info.height)


def test_extracted_clips_have_no_caption(tmp_path, fixture_video):
    """A library clip is a master. Compare a frame against one cut directly
    from the source at the same timecode."""
    shot = Shot(index=0, start=5.0, end=10.0)
    dest = extract_shot(fixture_video, shot, tmp_path / "shot.mp4")

    extracted_frame = tmp_path / "extracted.png"
    run([
        "ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(dest),
        "-frames:v", "1", str(extracted_frame),
    ])
    source_frame = tmp_path / "source.png"
    run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{shot.start + 0.5:.3f}", "-i", str(fixture_video),
        "-frames:v", "1", str(source_frame),
    ])

    extracted_img = Image.open(extracted_frame).convert("RGB")
    source_img = Image.open(source_frame).convert("RGB")
    assert extracted_img.size == source_img.size

    # Corners as well as the centre: a burned-in caption or letterbox pad
    # would show up at the edges even on a fixture flat enough that the
    # centre alone would not catch it.
    w, h = extracted_img.size
    for xy in [(2, 2), (w - 3, 2), (w // 2, h // 2), (2, h - 3), (w - 3, h - 3)]:
        a, b = extracted_img.getpixel(xy), source_img.getpixel(xy)
        assert all(abs(x - y) <= 20 for x, y in zip(a, b)), (xy, a, b)


def test_extract_shot_preserves_audio_when_source_has_it(tmp_path, video_with_audio):
    shot = Shot(index=0, start=0.0, end=2.0)
    dest = extract_shot(video_with_audio, shot, tmp_path / "shot.mp4")
    assert probe(dest).has_audio is True


def test_extract_shot_is_silent_when_source_has_no_audio(tmp_path, fixture_video):
    shot = Shot(index=0, start=0.0, end=2.0)
    dest = extract_shot(fixture_video, shot, tmp_path / "shot.mp4")
    assert probe(dest).has_audio is False


def test_extract_names_files_by_timecode(tmp_path, fixture_video):
    path = library_path(Workspace(root=tmp_path), fixture_video, 65.5, 4.25)
    assert path.name == "00h01m05.500s__4.25s.mp4"
    assert path.parent == Workspace(root=tmp_path).library / fixture_video.stem


def test_two_shots_starting_in_the_same_second_do_not_collide(tmp_path):
    ws = Workspace(root=tmp_path)
    video = Path("source.mp4")
    first = library_path(ws, video, 1.100, 0.5)
    second = library_path(ws, video, 1.600, 0.5)
    assert first != second


# --- library.extract_all -------------------------------------------------


def test_extract_stores_every_shot(conn, workspace, fixture_video):
    shots = detect_shots(fixture_video)
    added, skipped = extract_all(conn, video=fixture_video, workspace=workspace)

    assert (added, skipped) == (len(shots), 0)
    clips = store.library_clips(conn)
    assert len(clips) == len(shots)
    for clip in clips:
        resolved = workspace.root / clip["path"]
        assert resolved.exists()


def test_extract_is_idempotent(conn, workspace, fixture_video):
    """Second run adds nothing and rewrites no file (compare mtimes)."""
    extract_all(conn, video=fixture_video, workspace=workspace)
    clips = store.library_clips(conn)
    mtimes_before = {c["path"]: (workspace.root / c["path"]).stat().st_mtime_ns for c in clips}

    added, skipped = extract_all(conn, video=fixture_video, workspace=workspace)

    assert (added, skipped) == (0, len(clips))
    assert store.library_clips(conn) == clips
    mtimes_after = {c["path"]: (workspace.root / c["path"]).stat().st_mtime_ns for c in clips}
    assert mtimes_after == mtimes_before


def test_extract_all_re_encodes_a_row_whose_file_was_deleted(conn, workspace, fixture_video):
    extract_all(conn, video=fixture_video, workspace=workspace)
    clips = store.library_clips(conn)
    victim = Path(workspace.root / clips[0]["path"])
    victim.unlink()

    added, skipped = extract_all(conn, video=fixture_video, workspace=workspace)

    assert added == 1
    assert skipped == len(clips) - 1
    assert victim.exists()
    # The row is not duplicated: record_library_clip's own conflict handling
    # settles back on the id that was already assigned.
    assert len(store.library_clips(conn)) == len(clips)


def test_extract_all_records_the_source_video(conn, workspace, fixture_video):
    extract_all(conn, video=fixture_video, workspace=workspace)
    assert store.video_display_name(conn, video_id(fixture_video)) == fixture_video.name


def test_extract_all_reports_progress_per_shot(conn, workspace, fixture_video):
    seen = []
    extract_all(
        conn, video=fixture_video, workspace=workspace,
        on_progress=lambda index, total, shot, status: seen.append((index, total, status)),
    )
    shots = detect_shots(fixture_video)
    assert len(seen) == len(shots)
    assert [index for index, _, _ in seen] == list(range(1, len(shots) + 1))
    assert all(total == len(shots) for _, total, _ in seen)
    assert all(status == "added" for _, _, status in seen)


# --- library.estimate ------------------------------------------------------


def test_estimate_grows_with_more_footage(fixture_video):
    shots = detect_shots(fixture_video)
    small = estimate(fixture_video, shots[:1])
    large = estimate(fixture_video, shots)

    number = re.compile(r"[\d.]+")

    def minutes(text: str) -> float:
        return float(number.search(text).group())

    assert minutes(large) > minutes(small)


def test_estimate_mentions_a_size(fixture_video):
    shots = detect_shots(fixture_video)
    assert "MB" in estimate(fixture_video, shots)


# --- CLI: cutlist extract --------------------------------------------------


def test_extract_command_reports_shot_count_and_progress(fixture_video, tmp_path):
    result = runner.invoke(app, ["extract", str(fixture_video), "--root", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "6 shots" in result.stdout
    assert "6 added, 0 skipped" in result.stdout


def test_extract_command_second_run_skips_everything(fixture_video, tmp_path):
    runner.invoke(app, ["extract", str(fixture_video), "--root", str(tmp_path)])
    result = runner.invoke(app, ["extract", str(fixture_video), "--root", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "0 added, 6 skipped" in result.stdout


def test_extract_missing_video_exits_nonzero_with_clean_error(tmp_path):
    result = runner.invoke(app, ["extract", str(tmp_path / "nope.mp4")])
    assert result.exit_code != 0
    assert "error: no such file" in result.output
    assert "Traceback" not in result.output


# --- CLI: cutlist library ---------------------------------------------------


def test_library_command_lists_ids_and_paths(fixture_video, tmp_path):
    runner.invoke(app, ["extract", str(fixture_video), "--root", str(tmp_path)])
    result = runner.invoke(app, ["library", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    conn = connect(Workspace(root=tmp_path).database)
    clips = store.library_clips(conn)
    assert len(clips) == 6
    for clip in clips:
        assert str(clip["id"]) in result.stdout
        assert clip["path"] in result.stdout


def test_library_command_json_is_machine_readable(fixture_video, tmp_path):
    runner.invoke(app, ["extract", str(fixture_video), "--root", str(tmp_path)])
    result = runner.invoke(app, ["library", "--root", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert len(payload) == 6
    for row in payload:
        assert {"id", "path", "start_s", "end_s", "duration_s", "display_name"} <= row.keys()


def test_library_command_filters_by_video(fixture_video, cutfree_video, tmp_path):
    runner.invoke(app, ["extract", str(fixture_video), "--root", str(tmp_path)])
    runner.invoke(app, ["extract", str(cutfree_video), "--root", str(tmp_path)])

    result = runner.invoke(app, [
        "library", "--root", str(tmp_path), "--json",
        "--video", video_id(fixture_video),
    ])
    payload = json.loads(result.stdout)
    assert len(payload) == 6
    assert all(row["display_name"] == fixture_video.name for row in payload)


def test_library_command_reports_when_empty(tmp_path):
    result = runner.invoke(app, ["library", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "no library clips" in result.stdout
