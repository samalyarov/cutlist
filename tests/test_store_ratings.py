import pytest

from cutlist.db import store
from cutlist.db.schema import connect


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "cutlist.sqlite")


@pytest.fixture
def clip_id(conn):
    store.record_film(conn, film_hash="abc", display_name="fixture.mp4", duration_s=30.0)
    run_id = store.start_run(
        conn, preset_name="sample_preset", preset_sha256="sha", preset_json="{}",
        caption_text="TOMORROW", seed=7, cutlist_version="0.1.0", film_hashes=["abc"],
    )
    return store.record_clip(
        conn, run_id=run_id, ordinal=1, path="output/01.mp4", duration_s=6.0,
        segments=[
            store.SegmentRecord("abc", 1.0, 3.0, 0.5, 3.5, 0),
            store.SegmentRecord("abc", 8.0, 10.0, 7.5, 10.5, 1),
        ],
    )


def _segment_ids(conn, clip_id):
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM segment WHERE clip_id = ? ORDER BY position", (clip_id,)
        )
    ]


def test_rate_clip_rejects_an_unknown_verdict(conn, clip_id):
    with pytest.raises(ValueError, match="verdict"):
        store.rate_clip(conn, clip_id=clip_id, verdict="amazing")


def test_mark_shot_rejects_an_unknown_mark(conn, clip_id):
    segment_id = _segment_ids(conn, clip_id)[0]
    with pytest.raises(ValueError, match="mark"):
        store.mark_shot(conn, segment_id=segment_id, mark="meh")


def test_ratings_are_append_only_and_latest_wins(conn, clip_id):
    store.rate_clip(conn, clip_id=clip_id, verdict="ok")
    store.rate_clip(conn, clip_id=clip_id, verdict="fire")
    assert conn.execute("SELECT COUNT(*) FROM clip_rating").fetchone()[0] == 2
    assert store.clip_detail(conn, clip_id)["verdict"] == "fire"


def test_mark_shot_copies_the_spans_off_the_segment(conn, clip_id):
    segment_id = _segment_ids(conn, clip_id)[0]
    store.mark_shot(conn, segment_id=segment_id, mark="good")
    row = conn.execute("SELECT * FROM shot_rating").fetchone()
    assert (row["seg_start_s"], row["seg_end_s"]) == (1.0, 3.0)
    assert (row["shot_start_s"], row["shot_end_s"]) == (0.5, 3.5)
    assert row["film_hash"] == "abc"


def test_a_shot_mark_survives_deletion_of_its_clip(conn, clip_id):
    segment_id = _segment_ids(conn, clip_id)[0]
    store.mark_shot(conn, segment_id=segment_id, mark="veto")
    conn.execute("DELETE FROM clip WHERE id = ?", (clip_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM shot_rating").fetchone()
    assert row is not None
    assert row["segment_id"] is None
    assert row["mark"] == "veto"


def test_a_clip_verdict_dies_with_its_clip(conn, clip_id):
    store.rate_clip(conn, clip_id=clip_id, verdict="fire")
    conn.execute("DELETE FROM clip WHERE id = ?", (clip_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM clip_rating").fetchone()[0] == 0


def test_mark_shot_rejects_an_unknown_segment(conn):
    with pytest.raises(LookupError, match="segment"):
        store.mark_shot(conn, segment_id=999, mark="good")


def test_clips_for_review_hides_clips_that_already_have_a_verdict(conn, clip_id):
    assert [c["id"] for c in store.clips_for_review(conn)] == [clip_id]
    store.rate_clip(conn, clip_id=clip_id, verdict="no")
    assert store.clips_for_review(conn) == []
    assert [c["id"] for c in store.clips_for_review(conn, unrated_only=False)] == [clip_id]


def test_clips_for_review_filters_by_preset(conn, clip_id):
    assert store.clips_for_review(conn, preset="sample_preset")
    assert store.clips_for_review(conn, preset="nope") == []


def test_clip_detail_returns_segments_with_marks(conn, clip_id):
    segment_id = _segment_ids(conn, clip_id)[0]
    store.mark_shot(conn, segment_id=segment_id, mark="good")
    detail = store.clip_detail(conn, clip_id)
    assert detail["path"] == "output/01.mp4"
    assert [s["position"] for s in detail["segments"]] == [0, 1]
    assert detail["segments"][0]["mark"] == "good"
    assert detail["segments"][1]["mark"] is None


def test_clip_by_path_finds_a_recorded_clip(conn, clip_id):
    assert store.clip_by_path(conn, "output/01.mp4")["id"] == clip_id
    assert store.clip_by_path(conn, "output/missing.mp4") is None


def test_summary_counts_verdicts_and_marks(conn, clip_id):
    store.rate_clip(conn, clip_id=clip_id, verdict="fire")
    store.mark_shot(conn, segment_id=_segment_ids(conn, clip_id)[0], mark="veto")
    result = store.summary(conn)
    assert result["clips"] == 1
    assert result["verdicts"]["fire"] == 1
    assert result["marks"]["veto"] == 1
