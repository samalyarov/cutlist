import sqlite3

import pytest

from cutlist.db.schema import SCHEMA_VERSION, connect, migrate

TABLES = {"film", "run", "run_film", "clip", "segment", "clip_rating", "shot_rating"}


def _names(conn, kind):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    ).fetchall()
    return {row["name"] for row in rows}


def test_connect_creates_every_table_and_the_view(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    assert TABLES <= _names(conn, "table")
    assert "clip_film" in _names(conn, "view")


def test_connect_stamps_the_schema_version(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_migrate_is_idempotent(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    migrate(conn)
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_connect_creates_missing_parent_directories(tmp_path):
    conn = connect(tmp_path / "nested" / "deeper" / "cutlist.sqlite")
    assert TABLES <= _names(conn, "table")


def test_foreign_keys_are_enforced(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO clip (run_id, ordinal, path, duration_s) VALUES (?, ?, ?, ?)",
            (999, 1, "nope.mp4", 10.0),
        )


def test_verdict_and_mark_values_are_constrained(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    conn.execute(
        "INSERT INTO run (preset_name, preset_sha256, preset_json, caption_text, "
        "seed, cutlist_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("p", "sha", "{}", "c", 1, "0.1.0", "2026-08-09T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO clip (run_id, ordinal, path, duration_s) VALUES (?, ?, ?, ?)",
        (1, 1, "01.mp4", 10.0),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO clip_rating (clip_id, verdict, created_at) VALUES (?, ?, ?)",
            (1, "amazing", "2026-08-09T00:00:00+00:00"),
        )
