import sqlite3
from pathlib import Path

# Ratings are the only thing here that cannot be regenerated, which is why
# they carry their own copy of what they refer to: a clip verdict dies with
# its clip (it describes one specific assembly), but a shot mark outlives
# any clip that happened to contain it, so it keeps its own timecodes and
# merely loses the segment pointer.
_V1 = """
CREATE TABLE IF NOT EXISTS film (
    film_hash     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    duration_s    REAL,
    fps           REAL,
    width         INTEGER,
    height        INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id              INTEGER PRIMARY KEY,
    preset_name     TEXT NOT NULL,
    preset_sha256   TEXT NOT NULL,
    preset_json     TEXT NOT NULL,
    caption_text    TEXT NOT NULL,
    seed            INTEGER NOT NULL,
    cutlist_version TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- Which sources a run was pointed at. Intent, not outcome: a run that fails
-- before writing a clip still needs to record what it was aiming at, and that
-- is not derivable from the segments it never produced.
CREATE TABLE IF NOT EXISTS run_film (
    run_id    INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    film_hash TEXT    NOT NULL REFERENCES film(film_hash),
    PRIMARY KEY (run_id, film_hash)
);

CREATE TABLE IF NOT EXISTS clip (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL,
    path       TEXT    NOT NULL,
    duration_s REAL    NOT NULL,
    UNIQUE (run_id, ordinal)
);

CREATE TABLE IF NOT EXISTS segment (
    id           INTEGER PRIMARY KEY,
    clip_id      INTEGER NOT NULL REFERENCES clip(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    film_hash    TEXT    NOT NULL REFERENCES film(film_hash),
    seg_start_s  REAL    NOT NULL,
    seg_end_s    REAL    NOT NULL,
    shot_start_s REAL    NOT NULL,
    shot_end_s   REAL    NOT NULL,
    shot_index   INTEGER,
    UNIQUE (clip_id, position)
);

CREATE TABLE IF NOT EXISTS clip_rating (
    id         INTEGER PRIMARY KEY,
    clip_id    INTEGER NOT NULL REFERENCES clip(id) ON DELETE CASCADE,
    verdict    TEXT    NOT NULL CHECK (verdict IN ('fire', 'ok', 'no')),
    note       TEXT,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS shot_rating (
    id           INTEGER PRIMARY KEY,
    film_hash    TEXT    NOT NULL REFERENCES film(film_hash),
    seg_start_s  REAL    NOT NULL,
    seg_end_s    REAL    NOT NULL,
    shot_start_s REAL    NOT NULL,
    shot_end_s   REAL    NOT NULL,
    mark         TEXT    NOT NULL CHECK (mark IN ('good', 'bad', 'veto')),
    segment_id   INTEGER REFERENCES segment(id) ON DELETE SET NULL,
    note         TEXT,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segment_clip   ON segment (clip_id);
CREATE INDEX IF NOT EXISTS idx_segment_film   ON segment (film_hash);
CREATE INDEX IF NOT EXISTS idx_clip_run       ON clip (run_id);
CREATE INDEX IF NOT EXISTS idx_clip_rating    ON clip_rating (clip_id, created_at);
CREATE INDEX IF NOT EXISTS idx_shot_rating    ON shot_rating (film_hash, seg_start_s);

-- A clip's composition is exactly the distinct sources among its segments.
-- A view rather than a cached column, because a cached column would
-- eventually disagree with the segments it claims to describe.
CREATE VIEW IF NOT EXISTS clip_film AS
SELECT clip_id, film_hash, COUNT(*) AS segment_count
FROM segment
GROUP BY clip_id, film_hash;
"""

MIGRATIONS = [_V1]

# Derived, never hand-written: a constant that has to be bumped alongside the
# list is a constant that will eventually disagree with it.
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a connection up to SCHEMA_VERSION, applying only what is missing."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, statements in enumerate(MIGRATIONS, start=1):
        if current < version:
            conn.executescript(statements)
            # PRAGMA does not accept bound parameters. version comes from
            # enumerate over a module-level list, so it is always an int.
            conn.execute(f"PRAGMA user_version = {version:d}")
    conn.commit()


def connect(path: Path) -> sqlite3.Connection:
    """Open the ratings store, creating and migrating it if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL because `review` and `rate` can both write; foreign_keys because
    # SQLite leaves them off by default and the ON DELETE rules are load-bearing.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn
