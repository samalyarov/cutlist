import shutil

from cutlist.paths import Workspace, film_id


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


def test_workspace_paths_are_created_on_demand(tmp_path, fixture_film):
    ws = Workspace(root=tmp_path)
    cache = ws.cache_for(fixture_film)
    assert cache.is_dir()
    assert cache.parent == ws.cache
    assert film_id(fixture_film) in cache.name


def test_output_dir_groups_by_film_then_preset(tmp_path, fixture_film):
    ws = Workspace(root=tmp_path)
    out = ws.output_for(fixture_film, "real_saturday")
    assert out.is_dir()
    assert out.name == "real_saturday"
    assert out.parent.name == fixture_film.stem
