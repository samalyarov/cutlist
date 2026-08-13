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
    ws = Workspace(root=tmp_path)
    path = library_path(ws, fixture_video, 65.5, 4.25)
    assert path.name == "00h01m05.500s__4.25s.mp4"
    assert path.parent == ws.library / f"{fixture_video.stem}__{video_id(fixture_video)}"


def test_two_shots_starting_in_the_same_second_do_not_collide(tmp_path, fixture_video):
    ws = Workspace(root=tmp_path)
    first = library_path(ws, fixture_video, 1.100, 0.5)
    second = library_path(ws, fixture_video, 1.600, 0.5)
    assert first != second


def test_two_videos_sharing_a_filename_get_distinct_directories(tmp_path):
    """`library_path` must not key a video's directory on its stem alone.

    Two sources named `clip.mp4` in different projects used to collide on
    `library/clip/...`, so the second extraction silently overwrote the
    first's master file while the database went on claiming both rows'
    paths were fine.
    """
    ws = Workspace(root=tmp_path)
    project_a = tmp_path / "projA" / "clip.mp4"
    project_b = tmp_path / "projB" / "clip.mp4"
    project_a.parent.mkdir(parents=True)
    project_b.parent.mkdir(parents=True)
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", "color=c=0xFF0000:s=64x64:d=1:r=5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(project_a),
    ])
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", "color=c=0x0000FF:s=64x64:d=1:r=5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(project_b),
    ])
    assert project_a.stem == project_b.stem

    first = library_path(ws, project_a, 0.0, 1.0)
    second = library_path(ws, project_b, 0.0, 1.0)
    assert first != second
    assert first.parent != second.parent
    # Still browsable by eye: the stem leads, the hash only disambiguates.
    assert first.parent.name.startswith("clip__")
    assert second.parent.name.startswith("clip__")


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


def test_extract_all_keeps_two_same_named_sources_distinct(tmp_path):
    """Reproduces the review's scenario end to end, through extract_all and
    the store: two videos sharing a filename, each extracted into the same
    workspace, must both survive on disk with distinct paths, and each
    database row must point at footage that actually matches its own
    video_hash -- not merely at a path that still exists."""
    project_a = tmp_path / "projA" / "clip.mp4"
    project_b = tmp_path / "projB" / "clip.mp4"
    project_a.parent.mkdir(parents=True)
    project_b.parent.mkdir(parents=True)
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", "color=c=0xFF0000:s=64x64:d=2:r=5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(project_a),
    ])
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", "color=c=0x0000FF:s=64x64:d=2:r=5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(project_b),
    ])
    assert video_id(project_a) != video_id(project_b)

    workspace = Workspace(root=tmp_path / "ws")
    conn = connect(workspace.database)
    shot = Shot(index=0, start=0.0, end=2.0)

    added_a, _ = extract_all(conn, video=project_a, workspace=workspace, shots=[shot])
    added_b, _ = extract_all(conn, video=project_b, workspace=workspace, shots=[shot])
    assert (added_a, added_b) == (1, 1)

    clips = store.library_clips(conn)
    assert len(clips) == 2
    paths = {clip["path"] for clip in clips}
    assert len(paths) == 2, f"masters collided onto the same path: {paths}"

    def _sample(clip_path: Path) -> tuple[int, int, int]:
        frame = tmp_path / f"frame_{clip_path.name}.png"
        run([
            "ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(clip_path),
            "-frames:v", "1", str(frame),
        ])
        return Image.open(frame).convert("RGB").getpixel((10, 10))

    for clip in clips:
        resolved = workspace.root / clip["path"]
        assert resolved.exists()
        pixel = _sample(resolved)
        if clip["video_hash"] == video_id(project_a):
            assert pixel[0] > 150 and pixel[2] < 100, ("expected red", clip["path"], pixel)
        else:
            assert clip["video_hash"] == video_id(project_b)
            assert pixel[2] > 150 and pixel[0] < 100, ("expected blue", clip["path"], pixel)


def test_extract_all_accepts_precomputed_shots(conn, workspace, fixture_video, monkeypatch):
    """A caller that already ran detect_shots (to print a count or estimate
    before starting) must not pay for scene detection a second time."""
    import cutlist.library as library_module

    shots = detect_shots(fixture_video)

    def boom(*args, **kwargs):
        raise AssertionError("detect_shots re-run despite shots being provided")

    monkeypatch.setattr(library_module, "detect_shots", boom)

    added, skipped = extract_all(conn, video=fixture_video, workspace=workspace, shots=shots)
    assert (added, skipped) == (len(shots), 0)


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


def test_estimate_floors_tiny_sizes_instead_of_rounding_to_zero(tmp_path, monkeypatch):
    """:.0f rounds anything under half a megabyte down to a bare "0 MB",
    which reads as free rather than small."""
    import cutlist.library as library_module
    from cutlist.media.probe import VideoInfo

    video = tmp_path / "tiny.mp4"
    video.write_bytes(b"x" * 1024)  # 1 KB, so bitrate is tiny regardless of duration

    monkeypatch.setattr(
        library_module, "probe",
        lambda path: VideoInfo(
            path=path, duration=1000.0, width=1, height=1, fps=1.0, has_audio=False
        ),
    )

    text = estimate(video, [Shot(index=0, start=0.0, end=5.0)])
    assert "<1 MB" in text
    assert "~0 MB" not in text


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


def test_extract_command_detects_shots_only_once(fixture_video, tmp_path, monkeypatch):
    """Scene detection decodes the whole video; extract prints the count and
    estimate from one detect_shots call and must hand that same list to
    extract_all rather than paying for a second decode."""
    import cutlist.library as library_module

    def boom(*args, **kwargs):
        raise AssertionError("extract_all re-ran detect_shots instead of reusing the CLI's list")

    monkeypatch.setattr(library_module, "detect_shots", boom)

    result = runner.invoke(app, ["extract", str(fixture_video), "--root", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "6 added, 0 skipped" in result.stdout


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
