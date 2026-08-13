import pytest

from cutlist.db import store
from cutlist.db.schema import connect


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_video(conn, video_hash="abc", display_name="fixture.mp4", duration_s=30.0)
    return conn


def test_recording_the_same_shot_twice_returns_the_same_id(conn):
    first = store.record_library_clip(
        conn, video_hash="abc", start_s=1.0, end_s=3.0,
        shot_index=0, path="library/abc/0.mp4", duration_s=2.0,
    )
    second = store.record_library_clip(
        conn, video_hash="abc", start_s=1.0, end_s=3.0,
        shot_index=0, path="library/abc/0.mp4", duration_s=2.0,
    )
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM library_clip").fetchone()[0] == 1


def test_timecodes_differing_below_a_millisecond_are_the_same_shot(conn):
    """Float equality is not identity. 1.0000001 and 1.0 are one shot.

    The noisy value is written first and the clean one second, not the other
    way round: a write path that forgets to round would still store the
    noisy value verbatim, and a *clean*-first ordering would insert that
    exact clean value, so a second write of the same clean value would find
    it regardless of whether rounding happened at all -- the bug would be
    invisible. Writing the noisy value first means the row on disk only
    matches a later clean-valued write if both the write and the lookup
    actually round.
    """
    first = store.record_library_clip(
        conn, video_hash="abc", start_s=1.0000001, end_s=3.0000001,
        shot_index=0, path="library/abc/0.mp4", duration_s=2.0,
    )
    second = store.record_library_clip(
        conn, video_hash="abc", start_s=1.0, end_s=3.0,
        shot_index=0, path="library/abc/0.mp4", duration_s=2.0,
    )
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM library_clip").fetchone()[0] == 1

    # library_clip_at is called directly by later tasks, not only through
    # record_library_clip's own dedup check -- it must round an unrounded
    # caller's arguments too, or it silently misses a shot recorded under
    # its rounded name.
    store.record_library_clip(
        conn, video_hash="abc", start_s=2.0, end_s=4.0,
        shot_index=1, path="library/abc/1.mp4", duration_s=2.0,
    )
    found = store.library_clip_at(conn, video_hash="abc", start_s=2.0000001, end_s=4.0000001)
    assert found is not None
    assert (found["start_s"], found["end_s"]) == (2.0, 4.0)


def test_library_clips_filters_by_video(conn):
    store.record_video(conn, video_hash="xyz", display_name="other.mp4", duration_s=30.0)
    store.record_library_clip(
        conn, video_hash="abc", start_s=1.0, end_s=3.0,
        shot_index=0, path="library/abc/0.mp4", duration_s=2.0,
    )
    store.record_library_clip(
        conn, video_hash="xyz", start_s=5.0, end_s=8.0,
        shot_index=0, path="library/xyz/0.mp4", duration_s=3.0,
    )

    assert [c["video_hash"] for c in store.library_clips(conn, video="abc")] == ["abc"]
    assert len(store.library_clips(conn)) == 2


def test_library_clip_at_finds_a_shot_by_its_timecodes(conn):
    clip_id = store.record_library_clip(
        conn, video_hash="abc", start_s=1.0, end_s=3.0,
        shot_index=0, path="library/abc/0.mp4", duration_s=2.0,
    )
    found = store.library_clip_at(conn, video_hash="abc", start_s=1.0, end_s=3.0)
    assert found["id"] == clip_id
    assert store.library_clip(conn, clip_id)["id"] == clip_id
    assert store.library_clip_at(conn, video_hash="abc", start_s=9.0, end_s=10.0) is None
    assert store.library_clip(conn, 999) is None


def test_start_run_rejects_an_unknown_kind(conn):
    with pytest.raises(ValueError, match="kind"):
        store.start_run(
            conn, preset_name="p", preset_sha256="sha", preset_json="{}",
            caption_text="cap", seed=1, cutlist_version="0.1.0",
            video_hashes=["abc"], kind="nope",
        )


def test_start_run_records_the_kind(conn):
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="sha", preset_json="{}",
        caption_text="cap", seed=1, cutlist_version="0.1.0",
        video_hashes=["abc"], kind="assemble",
    )
    assert conn.execute("SELECT kind FROM run WHERE id = ?", (run_id,)).fetchone()[0] == (
        "assemble"
    )

    default_run_id = store.start_run(
        conn, preset_name="p", preset_sha256="sha", preset_json="{}",
        caption_text="cap", seed=1, cutlist_version="0.1.0",
        video_hashes=["abc"],
    )
    assert conn.execute(
        "SELECT kind FROM run WHERE id = ?", (default_run_id,)
    ).fetchone()[0] == "draft"
