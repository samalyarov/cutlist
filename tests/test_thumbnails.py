import shutil

from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.media.thumbs import thumbnail_bytes

JPEG_MAGIC = b"\xff\xd8\xff"


def test_thumbnail_bytes_returns_a_jpeg(fixture_video):
    image = thumbnail_bytes(fixture_video, 2.0)
    assert image.startswith(JPEG_MAGIC)
    assert len(image) > 200


def test_thumbnail_bytes_leaves_no_file_behind(fixture_video, tmp_path):
    before = set(tmp_path.rglob("*"))
    thumbnail_bytes(fixture_video, 2.0)
    assert set(tmp_path.rglob("*")) == before


def test_record_clip_stores_a_thumbnail_per_segment(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_video(conn, video_hash="h1", display_name="x.mp4")
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="s", preset_json="{}",
        caption_text="c", seed=1, cutlist_version="0.1.0", video_hashes=["h1"],
    )
    clip_id = store.record_clip(
        conn, run_id=run_id, ordinal=1, path="o/01.mp4", duration_s=4.0,
        segments=[
            store.SegmentRecord("h1", 1.0, 3.0, 0.0, 4.0, 0, thumbnail=b"\xff\xd8\xffA"),
            store.SegmentRecord("h1", 5.0, 7.0, 4.0, 8.0, 1, thumbnail=b"\xff\xd8\xffB"),
        ],
    )
    detail = store.clip_detail(conn, clip_id)
    images = [store.segment_thumbnail(conn, s["id"]) for s in detail["segments"]]
    assert images == [b"\xff\xd8\xffA", b"\xff\xd8\xffB"]


def test_a_segment_without_a_thumbnail_reports_none(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_video(conn, video_hash="h1", display_name="x.mp4")
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="s", preset_json="{}",
        caption_text="c", seed=1, cutlist_version="0.1.0", video_hashes=["h1"],
    )
    clip_id = store.record_clip(
        conn, run_id=run_id, ordinal=1, path="o/01.mp4", duration_s=2.0,
        segments=[store.SegmentRecord("h1", 1.0, 3.0, 0.0, 4.0, 0)],
    )
    segment_id = store.clip_detail(conn, clip_id)["segments"][0]["id"]
    assert store.segment_thumbnail(conn, segment_id) is None


def test_thumbnails_survive_the_source_video_being_deleted(tmp_path, fixture_video):
    """The reason this table exists: a mark stays legible after the source is gone."""
    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_video(conn, video_hash="h1", display_name="fixture.mp4")
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="s", preset_json="{}",
        caption_text="c", seed=1, cutlist_version="0.1.0", video_hashes=["h1"],
    )
    captured = thumbnail_bytes(fixture_video, 2.0)
    clip_id = store.record_clip(
        conn, run_id=run_id, ordinal=1, path="o/01.mp4", duration_s=2.0,
        segments=[store.SegmentRecord("h1", 1.0, 3.0, 0.0, 4.0, 0, thumbnail=captured)],
    )
    segment_id = store.clip_detail(conn, clip_id)["segments"][0]["id"]

    copy = tmp_path / "source.mp4"
    shutil.copy(fixture_video, copy)
    copy.unlink()

    assert store.segment_thumbnail(conn, segment_id) == captured


def test_deleting_a_clip_takes_its_thumbnails_with_it(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_video(conn, video_hash="h1", display_name="x.mp4")
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="s", preset_json="{}",
        caption_text="c", seed=1, cutlist_version="0.1.0", video_hashes=["h1"],
    )
    clip_id = store.record_clip(
        conn, run_id=run_id, ordinal=1, path="o/01.mp4", duration_s=2.0,
        segments=[store.SegmentRecord("h1", 1.0, 3.0, 0.0, 4.0, 0, thumbnail=b"\xff\xd8\xffA")],
    )
    with conn:
        conn.execute("DELETE FROM clip WHERE id = ?", (clip_id,))
    assert conn.execute("SELECT COUNT(*) FROM segment_thumbnail").fetchone()[0] == 0
