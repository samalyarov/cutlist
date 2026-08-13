import os
import shutil

from cutlist.paths import CHUNK, Workspace, video_id


def test_video_id_is_stable_across_rename(fixture_video, tmp_path):
    moved = tmp_path / "renamed.mp4"
    shutil.copy(fixture_video, moved)
    assert video_id(moved) == video_id(fixture_video)


def test_video_id_differs_for_different_content(fixture_video, tmp_path):
    other = tmp_path / "other.mp4"
    other.write_bytes(fixture_video.read_bytes() + b"padding")
    assert video_id(other) != video_id(fixture_video)


def test_video_id_handles_tiny_files(tmp_path):
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"abc")
    assert len(video_id(tiny)) == 32


def test_video_id_distinguishes_content_past_first_megabyte(tmp_path):
    # same length, identical first megabyte, differing only in the tail --
    # a size-only or head-only implementation would see these as the same video.
    # Both files land in the 1MB-2MB band, which also pins the gap between the
    # "just read the rest" and "seek to the tail" branches in video_id.
    head = os.urandom(CHUNK)
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(head + b"a" * (CHUNK // 2))
    b.write_bytes(head + b"b" * (CHUNK // 2))
    assert video_id(a) != video_id(b)


def test_video_id_hashes_tail_of_large_file(tmp_path):
    # over 2x CHUNK, so video_id takes the seek(-CHUNK, SEEK_END) branch
    head = os.urandom(CHUNK)
    middle = os.urandom(CHUNK)
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(head + middle + b"a" * CHUNK)
    b.write_bytes(head + middle + b"b" * CHUNK)
    assert video_id(a) != video_id(b)


def test_workspace_paths_are_created_on_demand(tmp_path, fixture_video):
    ws = Workspace(root=tmp_path)
    cache = ws.cache_for(fixture_video)
    assert cache.is_dir()
    assert cache.parent == ws.cache
    assert video_id(fixture_video) in cache.name


def test_output_dir_groups_by_video_then_preset_then_run(tmp_path, fixture_video):
    ws = Workspace(root=tmp_path)
    out = ws.output_for(fixture_video, "sample_preset", 7)
    assert out.is_dir()
    assert out.name == "7"
    assert out.parent.name == "sample_preset"
    assert out.parent.parent.name == fixture_video.stem


def test_workspace_library_is_a_root_level_sibling_of_output(tmp_path):
    ws = Workspace(root=tmp_path)
    assert ws.library == tmp_path / "library"


def test_output_dirs_of_two_runs_are_distinct(tmp_path, fixture_video):
    # Clips are named by ordinal, so a shared directory would mean run 2
    # silently overwriting run 1's files.
    ws = Workspace(root=tmp_path)
    first = ws.output_for(fixture_video, "sample_preset", 1)
    second = ws.output_for(fixture_video, "sample_preset", 2)
    assert first != second
    assert first.parent == second.parent
