import shutil
from pathlib import Path

from cutlist.media.sources import SourceMatch, find_source
from cutlist.paths import resolve_within, video_id


def _workspace(tmp_path, fixture_video, name="fixture.mp4"):
    (tmp_path / "input").mkdir()
    shutil.copy(fixture_video, tmp_path / "input" / name)
    return tmp_path


def test_finds_a_source_by_its_content_hash(tmp_path, fixture_video):
    root = _workspace(tmp_path, fixture_video)
    found = find_source(root, video_id(fixture_video))
    assert found == SourceMatch(root / "input" / "fixture.mp4", by_hash=True)


def test_finds_a_source_that_has_been_renamed(tmp_path, fixture_video):
    """The whole point: display_name is what was recorded, not where it is."""
    root = _workspace(tmp_path, fixture_video, name="renamed-later.mp4")
    found = find_source(root, video_id(fixture_video), display_name="fixture.mp4")
    assert found == SourceMatch(root / "input" / "renamed-later.mp4", by_hash=True)


def test_searches_subdirectories(tmp_path, fixture_video):
    nested = tmp_path / "input" / "2026" / "clips"
    nested.mkdir(parents=True)
    shutil.copy(fixture_video, nested / "fixture.mp4")
    assert find_source(tmp_path, video_id(fixture_video)) == SourceMatch(
        nested / "fixture.mp4", by_hash=True
    )


def test_falls_back_to_the_display_name_when_no_hash_matches(tmp_path, fixture_video):
    """A re-encoded source keeps its name and loses its hash.

    Reported as the weaker answer it is: `by_hash` False is what stops a
    caller that needs the real footage -- rerender, or anything writing to
    the database -- from taking this for the video that was rated.
    """
    root = _workspace(tmp_path, fixture_video)
    found = find_source(root, "a-hash-nothing-has", display_name="fixture.mp4")
    assert found == SourceMatch(root / "input" / "fixture.mp4", by_hash=False)


def test_returns_none_when_the_source_is_gone(tmp_path):
    (tmp_path / "input").mkdir()
    assert find_source(tmp_path, "missing", display_name="gone.mp4") is None


def test_returns_none_when_there_is_no_input_directory(tmp_path):
    assert find_source(tmp_path, "missing") is None


def test_ignores_files_that_are_not_video(tmp_path, fixture_video):
    root = _workspace(tmp_path, fixture_video)
    (root / "input" / "notes.txt").write_text("not a video", encoding="utf-8")
    assert find_source(root, video_id(fixture_video)) == SourceMatch(
        root / "input" / "fixture.mp4", by_hash=True
    )


def test_resolve_within_rejects_a_path_that_escapes_the_root(tmp_path):
    assert resolve_within(tmp_path, "../../etc/passwd") is None


def test_resolve_within_joins_a_workspace_relative_path(tmp_path):
    assert resolve_within(tmp_path, "output/x/1/01.mp4") == (
        tmp_path / "output" / "x" / "1" / "01.mp4"
    ).resolve()


def test_resolve_within_rejects_an_absolute_path(tmp_path):
    outside = str(Path(tmp_path.anchor) / "etc" / "passwd")
    assert resolve_within(tmp_path, outside) is None


def test_resolve_within_rejects_a_prefix_sibling_path(tmp_path):
    """`.../bc` must not pass as inside `.../b` just because it starts with it."""
    root = tmp_path / "b"
    assert resolve_within(root, "../bc/clip.mp4") is None


def test_a_cached_hit_is_still_reported_as_a_hash_match(tmp_path, fixture_video):
    """The cache short-circuits the walk, not the strength of the claim.

    It returns before the loop that sets by_hash, so a cached hit is the one
    place the flag could silently come back wrong.
    """
    root = _workspace(tmp_path, fixture_video)
    wanted = video_id(fixture_video)
    assert find_source(root, wanted).by_hash is True
    assert find_source(root, wanted) == SourceMatch(
        root / "input" / "fixture.mp4", by_hash=True
    )
