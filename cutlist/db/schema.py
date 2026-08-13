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

# The project's vocabulary is "video": not every source is a film, and the
# narrower word invites assumptions about provenance the project does not want.
#
# Order matters. SQLite does not refuse a RENAME COLUMN that a view depends on
# -- it silently rewrites the view's body to use the new name. Left in place,
# clip_film would survive as a view still named clip_film whose internals said
# video_hash. There is no ALTER VIEW, so it has to be dropped and recreated for
# its new name regardless. Table and column renames do propagate correctly into
# dependent REFERENCES clauses, so the foreign-key graph needs no manual repair.
_V2 = """
DROP VIEW IF EXISTS clip_film;

ALTER TABLE film RENAME TO video;
ALTER TABLE run_film RENAME TO run_video;

ALTER TABLE video RENAME COLUMN film_hash TO video_hash;
ALTER TABLE run_video RENAME COLUMN film_hash TO video_hash;
ALTER TABLE segment RENAME COLUMN film_hash TO video_hash;
ALTER TABLE shot_rating RENAME COLUMN film_hash TO video_hash;

DROP INDEX IF EXISTS idx_segment_film;
CREATE INDEX IF NOT EXISTS idx_segment_video ON segment (video_hash);

CREATE VIEW IF NOT EXISTS clip_video AS
SELECT clip_id, video_hash, COUNT(*) AS segment_count
FROM segment
GROUP BY clip_id, video_hash;

-- Thumbnails captured at draft time, so a segment mark stays legible after the
-- source video is deleted. A separate table rather than a column on segment:
-- mark_shot and segment_by_id both SELECT *, and a BLOB there would drag image
-- bytes through every mark written.
CREATE TABLE IF NOT EXISTS segment_thumbnail (
    segment_id  INTEGER PRIMARY KEY REFERENCES segment(id) ON DELETE CASCADE,
    image       BLOB NOT NULL,
    captured_at TEXT NOT NULL
);
"""

# The library: whole detected shots, kept at source resolution so they can be
# reused outside cutlist. A shot is the unit rather than a draft's trimmed pick,
# because a shot is the same shot whichever run found it -- trimmed picks would
# fill the table with near-duplicates and no id would mean anything durable.
#
# run.kind arrives here rather than as a CHECK constraint because SQLite's
# ALTER TABLE ADD COLUMN does not accept one; store.py validates it alongside
# the verdict and mark rules. It exists because run.seed is meaningless for a
# hand-picked assembly, and recording seed 0 without saying so would be a quiet
# lie about reproducibility.
_V3 = """
CREATE TABLE IF NOT EXISTS library_clip (
    id         INTEGER PRIMARY KEY,
    video_hash TEXT    NOT NULL REFERENCES video(video_hash),
    start_s    REAL    NOT NULL,
    end_s      REAL    NOT NULL,
    shot_index INTEGER,
    path       TEXT    NOT NULL,
    duration_s REAL    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE (video_hash, start_s, end_s)
);

CREATE INDEX IF NOT EXISTS idx_library_video ON library_clip (video_hash);

ALTER TABLE run ADD COLUMN kind TEXT NOT NULL DEFAULT 'draft';
"""

MIGRATIONS = [_V1, _V2, _V3]

# Derived, never hand-written: a constant that has to be bumped alongside the
# list is a constant that will eventually disagree with it.
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a connection up to SCHEMA_VERSION, applying only what is missing."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, statements in enumerate(MIGRATIONS, start=1):
        if current < version:
            # executescript() commits any pending transaction before it runs, and
            # then executes the script in autocommit mode unless the script itself
            # opens a transaction -- so without an explicit BEGIN, each DDL
            # statement commits on its own as it runs. _V1 could get away with
            # that because every statement is CREATE ... IF NOT EXISTS and the
            # whole script is safe to replay. A rename migration cannot: killed
            # after `ALTER TABLE film RENAME TO video` but before `run_film` is
            # renamed, user_version is still the old value, so the next connect()
            # replays the same script from the top against a database that no
            # longer has a `film` table -- unreadable and unmigratable. The BEGIN
            # has to live inside the script text, because executescript's own
            # implicit commit happens before any separately issued BEGIN would
            # take effect. PRAGMA user_version is itself part of the database's
            # transactional state, so stamping it inside the same transaction
            # means the schema change and the version marker commit together or
            # both roll back.
            #
            # PRAGMA does not accept bound parameters. version comes from
            # enumerate over a module-level list, so it is always an int.
            conn.executescript(
                f"BEGIN;\n{statements}\nPRAGMA user_version = {version:d};\nCOMMIT;"
            )


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
