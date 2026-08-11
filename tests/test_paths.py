import os
import shutil

from cutlist.paths import CHUNK, Workspace, film_id


def test_film_id_is_stable_across_rename(fixture_film, tmp_path):
    moved = tmp_path / "renamed.mp4"
    shutil.copy(fixture_film, moved)
    assert film_id(moved) == film_id(fixture_film)


def test_film_id_differs_for_different_content(fixture_film, tmp_path):
    other = tmp_path / "other.mp4"
    other.write_bytes(fixture_film.read_bytes() + b"padding")
    assert film_id(other) != film_id(fixture_film)


def test_film_id_handles_tiny_files(tmp_path):
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"abc")
    assert len(film_id(tiny)) == 32


def test_film_id_distinguishes_content_past_first_megabyte(tmp_path):
    # same length, identical first megabyte, differing only in the tail --
    # a size-only or head-only implementation would see these as the same film.
    # Both files land in the 1MB-2MB band, which also pins the gap between the
    # "just read the rest" and "seek to the tail" branches in film_id.
    head = os.urandom(CHUNK)
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(head + b"a" * (CHUNK // 2))
    b.write_bytes(head + b"b" * (CHUNK // 2))
    assert film_id(a) != film_id(b)


def test_film_id_hashes_tail_of_large_file(tmp_path):
    # over 2x CHUNK, so film_id takes the seek(-CHUNK, SEEK_END) branch
    head = os.urandom(CHUNK)
    middle = os.urandom(CHUNK)
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(head + middle + b"a" * CHUNK)
    b.write_bytes(head + middle + b"b" * CHUNK)
    assert film_id(a) != film_id(b)


def test_workspace_paths_are_created_on_demand(tmp_path, fixture_film):
    ws = Workspace(root=tmp_path)
    cache = ws.cache_for(fixture_film)
    assert cache.is_dir()
    assert cache.parent == ws.cache
    assert film_id(fixture_film) in cache.name


def test_output_dir_groups_by_film_then_preset_then_run(tmp_path, fixture_film):
    ws = Workspace(root=tmp_path)
    out = ws.output_for(fixture_film, "real_saturday", 7)
    assert out.is_dir()
    assert out.name == "7"
    assert out.parent.name == "real_saturday"
    assert out.parent.parent.name == fixture_film.stem


def test_output_dirs_of_two_runs_are_distinct(tmp_path, fixture_film):
    # Clips are named by ordinal, so a shared directory would mean run 2
    # silently overwriting run 1's files.
    ws = Workspace(root=tmp_path)
    first = ws.output_for(fixture_film, "real_saturday", 1)
    second = ws.output_for(fixture_film, "real_saturday", 2)
    assert first != second
    assert first.parent == second.parent
