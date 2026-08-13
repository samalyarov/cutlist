import sqlite3

import pytest

from cutlist.db import store
from cutlist.db.schema import _V1, _V2, SCHEMA_VERSION, connect, migrate

STAMP = "2026-08-13T00:00:00+00:00"


def _v2_database(path):
    """A database at _V2 with a row in every table, as v1.1 would have left it."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_V1)
    conn.execute("PRAGMA user_version = 1")
    conn.executescript(_V2)
    conn.execute("PRAGMA user_version = 2")

    conn.execute(
        "INSERT INTO video VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("h1", "x.mp4", 100.0, 25.0, 1920, 1080, STAMP, STAMP),
    )
    conn.execute(
        "INSERT INTO run (preset_name, preset_sha256, preset_json, caption_text, "
        "seed, cutlist_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("p", "sha", "{}", "cap", 7, "0.1.0", STAMP),
    )
    conn.execute("INSERT INTO run_video VALUES (1, 'h1')")
    conn.execute(
        "INSERT INTO clip (run_id, ordinal, path, duration_s) VALUES (1, 1, 'o/01.mp4', 12.0)"
    )
    conn.execute(
        "INSERT INTO segment (clip_id, position, video_hash, seg_start_s, seg_end_s, "
        "shot_start_s, shot_end_s, shot_index) VALUES (1, 0, 'h1', 1.0, 3.0, 0.5, 4.0, 2)"
    )
    conn.execute(
        "INSERT INTO clip_rating (clip_id, verdict, created_at) VALUES (1, 'fire', ?)",
        (STAMP,),
    )
    conn.execute(
        "INSERT INTO shot_rating (video_hash, seg_start_s, seg_end_s, shot_start_s, "
        "shot_end_s, mark, segment_id, created_at) VALUES ('h1', 1.0, 3.0, 0.5, 4.0, "
        "'good', 1, ?)",
        (STAMP,),
    )
    conn.execute(
        "INSERT INTO segment_thumbnail (segment_id, image, captured_at) VALUES (1, ?, ?)",
        (b"\x89PNG", STAMP),
    )
    conn.commit()
    return conn


def test_migration_preserves_every_row(tmp_path):
    conn = _v2_database(tmp_path / "old.sqlite")
    migrate(conn)

    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("video", "run", "run_video", "clip", "segment",
                      "clip_rating", "shot_rating", "segment_thumbnail")
    }
    assert counts == {
        "video": 1, "run": 1, "run_video": 1, "clip": 1, "segment": 1,
        "clip_rating": 1, "shot_rating": 1, "segment_thumbnail": 1,
    }
    assert conn.execute("SELECT COUNT(*) FROM library_clip").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_existing_runs_default_to_draft_kind(tmp_path):
    """A run recorded before kind existed is a draft, because that is all
    cutlist could produce at the time."""
    conn = _v2_database(tmp_path / "old.sqlite")
    migrate(conn)
    assert conn.execute("SELECT kind FROM run").fetchone()[0] == "draft"


def test_library_clip_table_exists_with_its_unique_constraint(tmp_path):
    conn = connect(tmp_path / "new.sqlite")
    store.record_video(conn, video_hash="h1", display_name="x.mp4")
    store.record_library_clip(
        conn, video_hash="h1", start_s=1.0, end_s=3.0,
        shot_index=0, path="library/x/1.mp4", duration_s=2.0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO library_clip (video_hash, start_s, end_s, path, "
            "duration_s, created_at) VALUES ('h1', 1.0, 3.0, 'other.mp4', 2.0, 't')"
        )


def test_the_asymmetric_delete_rule_still_holds(tmp_path):
    """A migration is exactly the kind of change that can silently drop an
    ON DELETE clause, so re-assert the invariant after applying _V3."""
    conn = _v2_database(tmp_path / "old.sqlite")
    migrate(conn)

    conn.execute("DELETE FROM clip WHERE id = 1")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM segment").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clip_rating").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM segment_thumbnail").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM shot_rating").fetchone()[0] == 1
    assert conn.execute("SELECT segment_id FROM shot_rating").fetchone()[0] is None


def test_migration_is_idempotent(tmp_path):
    conn = _v2_database(tmp_path / "old.sqlite")
    migrate(conn)
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 1
