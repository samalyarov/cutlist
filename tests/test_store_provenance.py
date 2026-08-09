import pytest

from cutlist.db import store
from cutlist.db.schema import connect


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "cutlist.sqlite")


def _film(conn, film_hash="abc123", name="fixture.mp4"):
    store.record_film(
        conn, film_hash=film_hash, display_name=name,
        duration_s=30.0, fps=25.0, width=320, height=240,
    )
    return film_hash


def _run(conn, film_hashes):
    return store.start_run(
        conn,
        preset_name="real_saturday",
        preset_sha256="deadbeef",
        preset_json='{"name": "real_saturday"}',
        caption_text="TOMORROW",
        seed=7,
        cutlist_version="0.1.0",
        film_hashes=film_hashes,
    )


def _segment(film_hash, position):
    start = 1.0 + position
    return store.SegmentRecord(
        film_hash=film_hash,
        seg_start_s=start,
        seg_end_s=start + 2.0,
        shot_start_s=start - 0.5,
        shot_end_s=start + 2.5,
        shot_index=position,
    )


def test_record_film_is_idempotent_and_updates_last_seen(conn):
    _film(conn)
    _film(conn)
    rows = conn.execute("SELECT * FROM film").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_seen_at"] <= rows[0]["last_seen_at"]


def test_start_run_records_every_source_it_was_pointed_at(conn):
    a, b = _film(conn, "aaa", "a.mp4"), _film(conn, "bbb", "b.mp4")
    run_id = _run(conn, [a, b])
    stored = {
        row["film_hash"]
        for row in conn.execute("SELECT film_hash FROM run_film WHERE run_id = ?", (run_id,))
    }
    assert stored == {"aaa", "bbb"}


def test_start_run_records_sources_even_with_no_clips(conn):
    run_id = _run(conn, [_film(conn)])
    assert conn.execute("SELECT COUNT(*) FROM clip").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM run_film WHERE run_id = ?", (run_id,)
    ).fetchone()[0] == 1


def test_record_clip_stores_segments_in_order(conn):
    film_hash = _film(conn)
    run_id = _run(conn, [film_hash])
    clip_id = store.record_clip(
        conn, run_id=run_id, ordinal=1, path="output/01.mp4", duration_s=6.0,
        segments=[_segment(film_hash, i) for i in range(3)],
    )
    rows = conn.execute(
        "SELECT position, seg_start_s, shot_index FROM segment "
        "WHERE clip_id = ? ORDER BY position", (clip_id,)
    ).fetchall()
    assert [row["position"] for row in rows] == [0, 1, 2]
    assert [row["shot_index"] for row in rows] == [0, 1, 2]


def test_every_segment_lies_inside_its_shot(conn):
    film_hash = _film(conn)
    run_id = _run(conn, [film_hash])
    store.record_clip(
        conn, run_id=run_id, ordinal=1, path="output/01.mp4", duration_s=6.0,
        segments=[_segment(film_hash, i) for i in range(3)],
    )
    for row in conn.execute("SELECT * FROM segment"):
        assert row["shot_start_s"] <= row["seg_start_s"]
        assert row["seg_end_s"] <= row["shot_end_s"]


def test_clip_film_view_reports_composition(conn):
    a, b = _film(conn, "aaa", "a.mp4"), _film(conn, "bbb", "b.mp4")
    run_id = _run(conn, [a, b])
    clip_id = store.record_clip(
        conn, run_id=run_id, ordinal=1, path="output/01.mp4", duration_s=6.0,
        segments=[_segment(a, 0), _segment(a, 1), _segment(b, 2)],
    )
    rows = conn.execute(
        "SELECT film_hash, segment_count FROM clip_film WHERE clip_id = ? "
        "ORDER BY film_hash", (clip_id,)
    ).fetchall()
    assert [(r["film_hash"], r["segment_count"]) for r in rows] == [("aaa", 2), ("bbb", 1)]


def test_deleting_a_clip_removes_its_segments(conn):
    film_hash = _film(conn)
    run_id = _run(conn, [film_hash])
    clip_id = store.record_clip(
        conn, run_id=run_id, ordinal=1, path="output/01.mp4", duration_s=6.0,
        segments=[_segment(film_hash, 0)],
    )
    conn.execute("DELETE FROM clip WHERE id = ?", (clip_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM segment").fetchone()[0] == 0
