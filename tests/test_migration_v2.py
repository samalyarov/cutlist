import sqlite3

import pytest

from cutlist.db.schema import _V1, SCHEMA_VERSION, connect, migrate

STAMP = "2026-08-12T00:00:00+00:00"


def _v1_database(path):
    """A database at _V1 with a row in every table, as v1.0 would have left it."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_V1)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO film VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("h1", "x.mp4", 100.0, 25.0, 1920, 1080, STAMP, STAMP),
    )
    conn.execute(
        "INSERT INTO run (preset_name, preset_sha256, preset_json, caption_text, "
        "seed, cutlist_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("p", "sha", "{}", "cap", 7, "0.1.0", STAMP),
    )
    conn.execute("INSERT INTO run_film VALUES (1, 'h1')")
    conn.execute(
        "INSERT INTO clip (run_id, ordinal, path, duration_s) VALUES (1, 1, 'o/01.mp4', 12.0)"
    )
    conn.execute(
        "INSERT INTO segment (clip_id, position, film_hash, seg_start_s, seg_end_s, "
        "shot_start_s, shot_end_s, shot_index) VALUES (1, 0, 'h1', 1.0, 3.0, 0.5, 4.0, 2)"
    )
    conn.execute(
        "INSERT INTO clip_rating (clip_id, verdict, created_at) VALUES (1, 'fire', ?)",
        (STAMP,),
    )
    conn.execute(
        "INSERT INTO shot_rating (film_hash, seg_start_s, seg_end_s, shot_start_s, "
        "shot_end_s, mark, segment_id, created_at) VALUES ('h1', 1.0, 3.0, 0.5, 4.0, "
        "'good', 1, ?)",
        (STAMP,),
    )
    conn.commit()
    return conn


def test_migration_preserves_every_row(tmp_path):
    conn = _v1_database(tmp_path / "old.sqlite")
    migrate(conn)

    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("video", "run", "run_video", "clip", "segment",
                      "clip_rating", "shot_rating")
    }
    assert counts == {
        "video": 1, "run": 1, "run_video": 1, "clip": 1,
        "segment": 1, "clip_rating": 1, "shot_rating": 1,
    }
    assert conn.execute("SELECT video_hash FROM segment").fetchone()[0] == "h1"
    assert conn.execute("SELECT video_hash FROM shot_rating").fetchone()[0] == "h1"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_migration_leaves_foreign_keys_intact_and_enforced(tmp_path):
    conn = _v1_database(tmp_path / "old.sqlite")
    migrate(conn)

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO segment (clip_id, position, video_hash, seg_start_s, "
            "seg_end_s, shot_start_s, shot_end_s) "
            "VALUES (1, 1, 'nope', 1.0, 2.0, 1.0, 2.0)"
        )


def test_migration_preserves_the_asymmetric_delete_rule(tmp_path):
    """The invariant the whole schema is shaped around, re-asserted after rename.

    A verdict describes one specific assembly and dies with it. A shot mark
    describes footage in the source and outlives any clip that contained it,
    keeping its own timecodes and merely losing the segment pointer.
    """
    conn = _v1_database(tmp_path / "old.sqlite")
    migrate(conn)

    conn.execute("DELETE FROM clip WHERE id = 1")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM segment").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clip_rating").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM shot_rating").fetchone()[0] == 1
    assert conn.execute("SELECT segment_id FROM shot_rating").fetchone()[0] is None


def test_the_old_view_is_gone_and_the_new_one_reports_composition(tmp_path):
    conn = _v1_database(tmp_path / "old.sqlite")
    migrate(conn)

    views = {
        row[0] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
    }
    assert "clip_film" not in views
    assert "clip_video" in views
    row = conn.execute("SELECT * FROM clip_video").fetchone()
    assert (row["clip_id"], row["video_hash"], row["segment_count"]) == (1, "h1", 1)


def test_no_film_named_object_survives_anywhere(tmp_path):
    conn = _v1_database(tmp_path / "old.sqlite")
    migrate(conn)

    schema = " ".join(
        row[0] or "" for row in conn.execute("SELECT sql FROM sqlite_master")
    )
    assert "film" not in schema.lower()


def test_a_fresh_database_has_the_thumbnail_table(tmp_path):
    conn = connect(tmp_path / "new.sqlite")
    tables = {
        row[0] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "segment_thumbnail" in tables
