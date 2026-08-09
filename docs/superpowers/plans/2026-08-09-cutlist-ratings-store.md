# cutlist Ratings and Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record what every clip was made of, and collect `fire`/`ok`/`no` verdicts on clips plus `good`/`bad`/`veto` marks on the shots inside them.

**Architecture:** A global SQLite database at the workspace root gains rows as `draft` renders. `cutlist review` serves a single local HTML page for keyboard-driven rating; `cutlist rate` writes the same rows from the terminal. All SQL lives in one module so the two rating paths cannot drift. Nothing reads ratings back — this phase only collects.

**Tech Stack:** Python 3.12, stdlib `sqlite3` and `http.server`, existing `typer` CLI, `ffmpeg` for thumbnails, `pytest`.

## Global Constraints

- Python 3.12 only (`requires-python = ">=3.12,<3.13"`).
- **No new runtime dependencies.** `sqlite3` and `http.server` are stdlib. Do not add a web framework, ORM, migration library, or HTTP client.
- The review page has **no CDN, no build step, no external requests**. CSS and JS are inlined in `page.html`; any font is a local file.
- Commit messages are short and meaningful. **No `Co-Authored-By` trailer, no "Generated with" line, no AI attribution of any kind.**
- Nothing in this phase reads ratings back to influence selection. `draft` writes only.
- Timestamps are UTC ISO-8601 strings: `datetime.now(timezone.utc).isoformat()`.
- Money paths use no floats; there is no money here, but durations are floats and compared with a tolerance, never `==`.
- Tests use `pytest`. **No browser automation** — the review page's HTTP API is tested, its DOM is not.
- Run tests with `.venv\Scripts\python.exe -m pytest` on Windows, `.venv/bin/python -m pytest` elsewhere.

---

### Task 1: Database schema and migrations

**Files:**
- Create: `cutlist/db/__init__.py`
- Create: `cutlist/db/schema.py`
- Test: `tests/test_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SCHEMA_VERSION: int`
  - `connect(path: Path) -> sqlite3.Connection` — opens, enables WAL and foreign keys, migrates, returns a connection with `row_factory = sqlite3.Row`.
  - `migrate(conn: sqlite3.Connection) -> None` — applies pending migrations, idempotent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schema.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cutlist.db'`

- [ ] **Step 3: Write the implementation**

Create `cutlist/db/__init__.py` as an empty file.

Create `cutlist/db/schema.py`:

```python
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_schema.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add cutlist/db/__init__.py cutlist/db/schema.py tests/test_schema.py
git commit -m "feat: ratings database schema and migrations"
```

---

### Task 2: Provenance writes

**Files:**
- Create: `cutlist/db/store.py`
- Test: `tests/test_store_provenance.py`

**Interfaces:**
- Consumes: `cutlist.db.schema.connect`.
- Produces:
  - `SegmentRecord` — frozen dataclass: `film_hash: str`, `seg_start_s: float`, `seg_end_s: float`, `shot_start_s: float`, `shot_end_s: float`, `shot_index: int | None`.
  - `record_film(conn, *, film_hash: str, display_name: str, duration_s: float | None = None, fps: float | None = None, width: int | None = None, height: int | None = None) -> None`
  - `start_run(conn, *, preset_name: str, preset_sha256: str, preset_json: str, caption_text: str, seed: int, cutlist_version: str, film_hashes: list[str]) -> int`
  - `record_clip(conn, *, run_id: int, ordinal: int, path: str, duration_s: float, segments: list[SegmentRecord]) -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_store_provenance.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_store_provenance.py -v`
Expected: FAIL — `ImportError: cannot import name 'store'`

- [ ] **Step 3: Write the implementation**

Create `cutlist/db/store.py`:

```python
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SegmentRecord:
    """One rendered trim, and the shot it was taken from.

    Both spans are kept because they are different claims: the segment is
    what was on screen and judged, the shot is the take it belongs to. Neither
    can be recovered from the other after the fact.
    """

    film_hash: str
    seg_start_s: float
    seg_end_s: float
    shot_start_s: float
    shot_end_s: float
    shot_index: int | None = None


def record_film(
    conn: sqlite3.Connection,
    *,
    film_hash: str,
    display_name: str,
    duration_s: float | None = None,
    fps: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Register a source, or refresh what we know about one already seen."""
    now = _now()
    conn.execute(
        """
        INSERT INTO film (film_hash, display_name, duration_s, fps, width, height,
                          first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (film_hash) DO UPDATE SET
            display_name = excluded.display_name,
            duration_s   = COALESCE(excluded.duration_s, film.duration_s),
            fps          = COALESCE(excluded.fps, film.fps),
            width        = COALESCE(excluded.width, film.width),
            height       = COALESCE(excluded.height, film.height),
            last_seen_at = excluded.last_seen_at
        """,
        (film_hash, display_name, duration_s, fps, width, height, now, now),
    )
    conn.commit()


def start_run(
    conn: sqlite3.Connection,
    *,
    preset_name: str,
    preset_sha256: str,
    preset_json: str,
    caption_text: str,
    seed: int,
    cutlist_version: str,
    film_hashes: list[str],
) -> int:
    """Open a run and record which sources it was pointed at.

    Called before any clip is rendered, so a run that fails partway still
    leaves a record of its inputs.
    """
    cursor = conn.execute(
        """
        INSERT INTO run (preset_name, preset_sha256, preset_json, caption_text,
                         seed, cutlist_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (preset_name, preset_sha256, preset_json, caption_text, seed,
         cutlist_version, _now()),
    )
    run_id = int(cursor.lastrowid)
    conn.executemany(
        "INSERT OR IGNORE INTO run_film (run_id, film_hash) VALUES (?, ?)",
        [(run_id, film_hash) for film_hash in film_hashes],
    )
    conn.commit()
    return run_id


def record_clip(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    ordinal: int,
    path: str,
    duration_s: float,
    segments: list[SegmentRecord],
) -> int:
    """Record one rendered clip and everything it was assembled from."""
    cursor = conn.execute(
        "INSERT INTO clip (run_id, ordinal, path, duration_s) VALUES (?, ?, ?, ?)",
        (run_id, ordinal, path, duration_s),
    )
    clip_id = int(cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO segment (clip_id, position, film_hash, seg_start_s, seg_end_s,
                             shot_start_s, shot_end_s, shot_index)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (clip_id, position, s.film_hash, s.seg_start_s, s.seg_end_s,
             s.shot_start_s, s.shot_end_s, s.shot_index)
            for position, s in enumerate(segments)
        ],
    )
    conn.commit()
    return clip_id
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_store_provenance.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add cutlist/db/store.py tests/test_store_provenance.py
git commit -m "feat: record runs, clips and segment provenance"
```

---

### Task 3: Rating writes and review reads

**Files:**
- Modify: `cutlist/db/store.py` (append; do not alter Task 2's functions)
- Test: `tests/test_store_ratings.py`

**Interfaces:**
- Consumes: everything from Task 2.
- Produces:
  - `VERDICTS: tuple[str, ...]` — `("fire", "ok", "no")`
  - `MARKS: tuple[str, ...]` — `("good", "bad", "veto")`
  - `rate_clip(conn, *, clip_id: int, verdict: str, note: str | None = None) -> int`
  - `mark_shot(conn, *, segment_id: int, mark: str, note: str | None = None) -> int`
  - `clip_by_path(conn, path: str) -> sqlite3.Row | None`
  - `clips_for_review(conn, *, film: str | None = None, preset: str | None = None, unrated_only: bool = True) -> list[dict]`
  - `clip_detail(conn, clip_id: int) -> dict | None`
  - `summary(conn) -> dict`
  - `clip_path(conn, clip_id: int) -> str | None`
  - `segment_by_id(conn, segment_id: int) -> sqlite3.Row | None`
  - `film_display_name(conn, film_hash: str) -> str | None`

The last three exist so the review server never writes SQL of its own — the
spec makes `store.py` the single module that talks to the database, and the
two rating paths only stay consistent if that holds.

- [ ] **Step 1: Write the failing test**

Create `tests/test_store_ratings.py`:

```python
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
        conn, preset_name="real_saturday", preset_sha256="sha", preset_json="{}",
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
    assert store.clips_for_review(conn, preset="real_saturday")
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_store_ratings.py -v`
Expected: FAIL — `AttributeError: module 'cutlist.db.store' has no attribute 'rate_clip'`

- [ ] **Step 3: Write the implementation**

Append to `cutlist/db/store.py`:

```python
VERDICTS = ("fire", "ok", "no")
MARKS = ("good", "bad", "veto")


def rate_clip(
    conn: sqlite3.Connection, *, clip_id: int, verdict: str, note: str | None = None
) -> int:
    """Record a verdict on one assembled clip.

    Append-only: re-rating a clip adds a row rather than replacing one, so
    changing your mind is itself recorded.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}, got {verdict!r}")
    cursor = conn.execute(
        "INSERT INTO clip_rating (clip_id, verdict, note, created_at) VALUES (?, ?, ?, ?)",
        (clip_id, verdict, note, _now()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def mark_shot(
    conn: sqlite3.Connection, *, segment_id: int, mark: str, note: str | None = None
) -> int:
    """Record a mark on the footage a segment was cut from.

    The segment's spans are copied onto the rating rather than referenced,
    so the judgement outlives the clip that occasioned it.
    """
    if mark not in MARKS:
        raise ValueError(f"mark must be one of {', '.join(MARKS)}, got {mark!r}")
    segment = conn.execute("SELECT * FROM segment WHERE id = ?", (segment_id,)).fetchone()
    if segment is None:
        raise LookupError(f"no such segment: {segment_id}")

    cursor = conn.execute(
        """
        INSERT INTO shot_rating (film_hash, seg_start_s, seg_end_s, shot_start_s,
                                 shot_end_s, mark, segment_id, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (segment["film_hash"], segment["seg_start_s"], segment["seg_end_s"],
         segment["shot_start_s"], segment["shot_end_s"], mark, segment_id, note, _now()),
    )
    conn.commit()
    return int(cursor.lastrowid)


# The current verdict for a clip is its most recent row. Expressed once here
# so every read path resolves "latest wins" the same way.
_LATEST_VERDICT = """
SELECT verdict FROM clip_rating
WHERE clip_id = clip.id
ORDER BY created_at DESC, id DESC LIMIT 1
"""

_LATEST_MARK = """
SELECT mark FROM shot_rating
WHERE segment_id = segment.id
ORDER BY created_at DESC, id DESC LIMIT 1
"""


def clip_by_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM clip WHERE path = ?", (path,)).fetchone()


def clips_for_review(
    conn: sqlite3.Connection,
    *,
    film: str | None = None,
    preset: str | None = None,
    unrated_only: bool = True,
) -> list[dict]:
    """List clips to review, newest first."""
    sql = f"""
        SELECT clip.id, clip.ordinal, clip.path, clip.duration_s,
               run.preset_name, run.caption_text, run.created_at,
               ({_LATEST_VERDICT}) AS verdict,
               (SELECT COUNT(*) FROM segment WHERE segment.clip_id = clip.id)
                   AS segment_count
        FROM clip
        JOIN run ON run.id = clip.run_id
        WHERE 1 = 1
    """
    params: list[object] = []
    if preset is not None:
        sql += " AND run.preset_name = ?"
        params.append(preset)
    if film is not None:
        sql += """ AND EXISTS (
            SELECT 1 FROM clip_film
            WHERE clip_film.clip_id = clip.id AND clip_film.film_hash = ?
        )"""
        params.append(film)
    if unrated_only:
        sql += f" AND ({_LATEST_VERDICT}) IS NULL"
    sql += " ORDER BY run.created_at DESC, clip.ordinal ASC"

    return [dict(row) for row in conn.execute(sql, params)]


def clip_detail(conn: sqlite3.Connection, clip_id: int) -> dict | None:
    """One clip with its segments and their current marks."""
    row = conn.execute(
        f"""
        SELECT clip.id, clip.ordinal, clip.path, clip.duration_s,
               run.preset_name, run.caption_text, run.seed,
               ({_LATEST_VERDICT}) AS verdict
        FROM clip JOIN run ON run.id = clip.run_id
        WHERE clip.id = ?
        """,
        (clip_id,),
    ).fetchone()
    if row is None:
        return None

    segments = conn.execute(
        f"""
        SELECT segment.id, segment.position, segment.film_hash,
               segment.seg_start_s, segment.seg_end_s,
               segment.shot_start_s, segment.shot_end_s, segment.shot_index,
               film.display_name,
               ({_LATEST_MARK}) AS mark
        FROM segment JOIN film ON film.film_hash = segment.film_hash
        WHERE segment.clip_id = ?
        ORDER BY segment.position
        """,
        (clip_id,),
    ).fetchall()

    detail = dict(row)
    detail["segments"] = [dict(s) for s in segments]
    return detail


def clip_path(conn: sqlite3.Connection, clip_id: int) -> str | None:
    """The workspace-relative path a clip was written to."""
    row = conn.execute("SELECT path FROM clip WHERE id = ?", (clip_id,)).fetchone()
    return None if row is None else row["path"]


def segment_by_id(conn: sqlite3.Connection, segment_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM segment WHERE id = ?", (segment_id,)).fetchone()


def film_display_name(conn: sqlite3.Connection, film_hash: str) -> str | None:
    row = conn.execute(
        "SELECT display_name FROM film WHERE film_hash = ?", (film_hash,)
    ).fetchone()
    return None if row is None else row["display_name"]


def summary(conn: sqlite3.Connection) -> dict:
    """Counts for `cutlist ratings`."""
    def _counts(sql: str) -> dict[str, int]:
        return {row[0]: row[1] for row in conn.execute(sql)}

    return {
        "films": conn.execute("SELECT COUNT(*) FROM film").fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM run").fetchone()[0],
        "clips": conn.execute("SELECT COUNT(*) FROM clip").fetchone()[0],
        "segments": conn.execute("SELECT COUNT(*) FROM segment").fetchone()[0],
        "verdicts": _counts(
            "SELECT verdict, COUNT(*) FROM clip_rating GROUP BY verdict"
        ),
        "marks": _counts("SELECT mark, COUNT(*) FROM shot_rating GROUP BY mark"),
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_store_ratings.py -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add cutlist/db/store.py tests/test_store_ratings.py
git commit -m "feat: clip verdicts, shot marks and review queries"
```

---

### Task 4: Thread the shot through selection

The store needs each segment's parent shot, but `draft_segments` currently
returns bare `Segment` values and discards the `Shot` each was cut from. This
task makes that link survive.

**Files:**
- Modify: `cutlist/select/naive.py`
- Modify: `cutlist/cli.py:132-143`
- Modify: `tests/test_naive.py`
- Test: `tests/test_naive.py`

**Interfaces:**
- Consumes: `cutlist.media.shots.Shot`, `cutlist.media.render.Segment`, `cutlist.presets.RhythmSpec`.
- Produces:
  - `Pick` — frozen dataclass with `shot: Shot` and `segment: Segment`.
  - `draft_picks(shots: list[Shot], rhythm: RhythmSpec, rng: random.Random) -> list[Pick]` — replaces `draft_segments`; same selection behaviour, richer return.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_naive.py`:

```python
from cutlist.select.naive import Pick, draft_picks


def test_draft_picks_pairs_every_segment_with_its_shot(rhythm):
    shots = [Shot(index=i, start=i * 5.0, end=i * 5.0 + 5.0) for i in range(8)]
    picks = draft_picks(shots, rhythm, random.Random(1))

    assert picks
    assert all(isinstance(p, Pick) for p in picks)
    for pick in picks:
        assert pick.shot.start <= pick.segment.start
        assert pick.segment.end <= pick.shot.end


def test_draft_picks_keeps_shots_in_timecode_order(rhythm):
    shots = [Shot(index=i, start=i * 5.0, end=i * 5.0 + 5.0) for i in range(8)]
    picks = draft_picks(shots, rhythm, random.Random(2))
    starts = [p.shot.start for p in picks]
    assert starts == sorted(starts)


def test_draft_picks_is_reproducible_for_a_seed(rhythm):
    shots = [Shot(index=i, start=i * 5.0, end=i * 5.0 + 5.0) for i in range(8)]
    first = draft_picks(shots, rhythm, random.Random(42))
    second = draft_picks(shots, rhythm, random.Random(42))
    assert [(p.shot.index, p.segment.start) for p in first] == \
           [(p.shot.index, p.segment.start) for p in second]
```

If `tests/test_naive.py` has no `rhythm` fixture, add one that matches
`presets/real_saturday.yaml`:

```python
@pytest.fixture
def rhythm():
    return RhythmSpec(
        min_segments=4, max_segments=10,
        min_seconds=1.2, target_seconds=2.0, max_seconds=2.8,
        min_total=9.0, max_total=15.0,
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_naive.py -v`
Expected: FAIL — `ImportError: cannot import name 'Pick' from 'cutlist.select.naive'`

- [ ] **Step 3: Write the implementation**

In `cutlist/select/naive.py`, add the dataclass and rename the function. Replace the `import` block and the `draft_segments` definition:

```python
import random
from dataclasses import dataclass

from cutlist.media.render import Segment
from cutlist.media.shots import Shot
from cutlist.presets import RhythmSpec


@dataclass(frozen=True)
class Pick:
    """A chosen segment together with the shot it was cut from.

    Rendering only needs the segment, but provenance needs the shot: a
    judgement about "this moment" and one about "this take" are different
    claims, and neither is recoverable from the other afterwards.
    """

    shot: Shot
    segment: Segment
```

Change the signature and the return line of `draft_segments`:

```python
def draft_picks(
    shots: list[Shot],
    rhythm: RhythmSpec,
    rng: random.Random,
) -> list[Pick]:
```

and inside it, replace the successful return with:

```python
        if durations is not None:
            return [
                Pick(shot=shot, segment=_centred(shot, length))
                for shot, length in zip(chosen, durations)
            ]
```

Everything else in the module — `_centred`, `_fit_total`, `_redistribute`,
`NotEnoughFootage`, `_MAX_ATTEMPTS` — is unchanged.

Update the existing tests in `tests/test_naive.py` that call `draft_segments`:
replace each call with `draft_picks(...)` and take `[p.segment for p in picks]`
where the old test asserted on segments directly.

Update `cutlist/cli.py`. Change the import on line 15:

```python
from cutlist.select.naive import NotEnoughFootage, draft_picks
```

and the loop body at lines 131-136:

```python
        try:
            picks = draft_picks(found, spec.rhythm, rng)
            segments = [pick.segment for pick in picks]
            clip = destination / f"{n:02d}.mp4"
            render_clip(
                film, segments, caption_png, spec.output, clip, scratch_root / f"{n:02d}"
            )
```

- [ ] **Step 4: Run the whole suite to verify nothing regressed**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS — every previously passing test still passes, plus 3 new ones

- [ ] **Step 5: Commit**

```bash
git add cutlist/select/naive.py cutlist/cli.py tests/test_naive.py
git commit -m "refactor: carry the source shot alongside each drafted segment"
```

---

### Task 5: `draft` records provenance

**Files:**
- Modify: `cutlist/paths.py` (add `Workspace.database`)
- Modify: `cutlist/cli.py:88-147`
- Test: `tests/test_draft_provenance.py`

**Interfaces:**
- Consumes: `store.record_film`, `store.start_run`, `store.record_clip`, `store.SegmentRecord`, `schema.connect`, `Pick`.
- Produces:
  - `Workspace.database -> Path` — `root / "cutlist.sqlite"`.
  - A `draft` run that writes one `run` row, one `run_film` row, and one `clip` row with its `segment` rows per rendered clip.

- [ ] **Step 1: Write the failing test**

Create `tests/test_draft_provenance.py`:

```python
import json

import pytest
from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.db.schema import connect

runner = CliRunner()

PRESET = """
name: test_preset
caption:
  text: "TEST"
rhythm:
  segments: {min: 2, max: 3}
  seg_duration: {min: 1.0, target: 1.5, max: 2.0}
  total: {min: 3.0, max: 6.0}
output:
  width: 160
  height: 120
  fps: 25
  crf: 30
"""


@pytest.fixture
def preset_file(tmp_path):
    path = tmp_path / "test_preset.yaml"
    path.write_text(PRESET, encoding="utf-8")
    return path


def _draft(fixture_film, preset_file, root, extra=()):
    return runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", str(preset_file),
        "--count", "2",
        "--root", str(root),
        *extra,
    ])


def test_draft_records_a_run_with_its_source(fixture_film, preset_file, tmp_path):
    result = _draft(fixture_film, preset_file, tmp_path)
    assert result.exit_code == 0, result.output

    conn = connect(tmp_path / "cutlist.sqlite")
    run = conn.execute("SELECT * FROM run").fetchone()
    assert run["preset_name"] == "test_preset"
    assert run["caption_text"] == "TEST"
    assert conn.execute(
        "SELECT COUNT(*) FROM run_film WHERE run_id = ?", (run["id"],)
    ).fetchone()[0] == 1


def test_draft_always_records_a_seed_even_when_none_was_given(
    fixture_film, preset_file, tmp_path
):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    assert conn.execute("SELECT seed FROM run").fetchone()["seed"] is not None


def test_draft_records_the_supplied_seed(fixture_film, preset_file, tmp_path):
    _draft(fixture_film, preset_file, tmp_path, extra=["--seed", "1234"])
    conn = connect(tmp_path / "cutlist.sqlite")
    assert conn.execute("SELECT seed FROM run").fetchone()["seed"] == 1234


def test_draft_stores_the_resolved_preset(fixture_film, preset_file, tmp_path):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    stored = json.loads(conn.execute("SELECT preset_json FROM run").fetchone()[0])
    assert stored["name"] == "test_preset"
    assert stored["rhythm"]["min_segments"] == 2


def test_draft_records_a_clip_row_per_rendered_file(
    fixture_film, preset_file, tmp_path
):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    clips = conn.execute("SELECT * FROM clip ORDER BY ordinal").fetchall()
    assert [c["ordinal"] for c in clips] == [1, 2]
    for clip in clips:
        assert (tmp_path / clip["path"]).exists()


def test_every_recorded_segment_lies_inside_its_shot(
    fixture_film, preset_file, tmp_path
):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    rows = conn.execute("SELECT * FROM segment").fetchall()
    assert rows
    for row in rows:
        assert row["shot_start_s"] <= row["seg_start_s"]
        assert row["seg_end_s"] <= row["shot_end_s"]


def test_recorded_segment_durations_match_the_clip_duration(
    fixture_film, preset_file, tmp_path
):
    _draft(fixture_film, preset_file, tmp_path)
    conn = connect(tmp_path / "cutlist.sqlite")
    for clip in conn.execute("SELECT * FROM clip"):
        total = conn.execute(
            "SELECT SUM(seg_end_s - seg_start_s) FROM segment WHERE clip_id = ?",
            (clip["id"],),
        ).fetchone()[0]
        assert abs(total - clip["duration_s"]) < 0.05
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_draft_provenance.py -v`
Expected: FAIL — no `cutlist.sqlite` is created; `sqlite3.OperationalError: no such table: run` or an empty `run` table

- [ ] **Step 3: Write the implementation**

Add to `cutlist/paths.py`, inside `Workspace`:

```python
    @property
    def database(self) -> Path:
        """The ratings store.

        Deliberately at the workspace root rather than under cache/: taste
        generalises across films, and the cache is regenerable. Deleting it
        must not destroy a month of judgements.
        """
        return self.root / "cutlist.sqlite"
```

Add these imports to `cutlist/cli.py`:

```python
import hashlib
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version

from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.paths import Workspace, film_id
```

(The existing `from cutlist.paths import Workspace` line is replaced by the one above.)

Add these helpers to `cutlist/cli.py`, above the `draft` command:

```python
def _cutlist_version() -> str:
    """Which build produced a run.

    Recorded so ratings from the random-selection era are never silently
    pooled with ratings from a later scoring era -- they measure different
    things.
    """
    try:
        return version("cutlist")
    except PackageNotFoundError:
        return "unknown"


def _preset_fingerprint(path: Path, spec) -> tuple[str, str]:
    """Hash the preset file, and serialise the resolved preset.

    The hash groups runs that used an identical preset. The JSON makes each
    run self-describing after the YAML is edited or deleted.
    """
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest, json.dumps(asdict(spec), sort_keys=True)
```

Rewrite the body of `draft` from the `workspace = ...` line onward:

```python
    workspace = Workspace(root=root)
    destination = workspace.output_for(film, spec.name)

    typer.echo(f"caption: {spec.caption.text}")
    typer.echo("detecting shots...")
    found = detect_shots(film)
    typer.echo(f"{len(found)} shots")

    # A run with no recorded seed cannot be reproduced, and an unreproducible
    # run cannot have its provenance rebuilt if anything downstream is lost.
    # Generate one rather than leaving it to chance.
    if seed is None:
        seed = random.randrange(2**31)
    rng = random.Random(seed)

    info = probe_film(film)
    film_hash = film_id(film)
    conn = connect(workspace.database)
    store.record_film(
        conn,
        film_hash=film_hash,
        display_name=film.name,
        duration_s=info.duration,
        fps=info.fps,
        width=info.width,
        height=info.height,
    )

    preset_sha256, preset_json = _preset_fingerprint(preset, spec)
    # Opened before the first render, so a run that dies partway still
    # records which source it was pointed at.
    run_id = store.start_run(
        conn,
        preset_name=spec.name,
        preset_sha256=preset_sha256,
        preset_json=preset_json,
        caption_text=spec.caption.text,
        seed=seed,
        cutlist_version=_cutlist_version(),
        film_hashes=[film_hash],
    )

    scratch_root = workspace.cache_for(film) / f"scratch_{uuid.uuid4().hex[:8]}"
    caption_png = render_caption(spec.caption, spec.output, scratch_root / "caption.png")

    written = 0
    for n in range(1, count + 1):
        try:
            picks = draft_picks(found, spec.rhythm, rng)
            segments = [pick.segment for pick in picks]
            clip = destination / f"{n:02d}.mp4"
            render_clip(
                film, segments, caption_png, spec.output, clip, scratch_root / f"{n:02d}"
            )
        except (NotEnoughFootage, ToolError) as exc:
            typer.echo(
                f"wrote {written} of {count} clips; failed on {n:02d}: {exc}", err=True
            )
            raise typer.Exit(code=1) from None

        length = sum(s.duration for s in segments)
        store.record_clip(
            conn,
            run_id=run_id,
            ordinal=n,
            path=clip.relative_to(root).as_posix(),
            duration_s=length,
            segments=[
                store.SegmentRecord(
                    film_hash=film_hash,
                    seg_start_s=pick.segment.start,
                    seg_end_s=pick.segment.end,
                    shot_start_s=pick.shot.start,
                    shot_end_s=pick.shot.end,
                    shot_index=pick.shot.index,
                )
                for pick in picks
            ],
        )

        typer.echo(f"{clip.name}  {len(segments)} segments  {length:.1f}s")
        written += 1

    typer.echo(f"\nwrote {written} clips to {destination}  (seed {seed})")
```

- [ ] **Step 4: Run the whole suite to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS, including 7 new provenance tests

- [ ] **Step 5: Commit**

```bash
git add cutlist/paths.py cutlist/cli.py tests/test_draft_provenance.py
git commit -m "feat: draft records run, clip and segment provenance"
```

---

### Task 6: Segment thumbnails

**Files:**
- Create: `cutlist/media/thumbs.py`
- Test: `tests/test_thumbs.py`

**Interfaces:**
- Consumes: `cutlist.shell.run`.
- Produces:
  - `thumbnail(video: Path, at_seconds: float, dest: Path, *, width: int = 160) -> Path` — extracts one frame, returns `dest`. If `dest` already exists it is returned untouched.

- [ ] **Step 1: Write the failing test**

Create `tests/test_thumbs.py`:

```python
from cutlist.media.thumbs import thumbnail


def test_thumbnail_writes_a_jpeg(fixture_film, tmp_path):
    dest = thumbnail(fixture_film, 7.5, tmp_path / "a.jpg")
    assert dest.exists()
    assert dest.stat().st_size > 0
    assert dest.read_bytes()[:2] == b"\xff\xd8"  # JPEG SOI marker


def test_thumbnail_creates_missing_parent_directories(fixture_film, tmp_path):
    dest = thumbnail(fixture_film, 2.0, tmp_path / "nested" / "deep" / "a.jpg")
    assert dest.exists()


def test_thumbnail_is_cached_and_not_regenerated(fixture_film, tmp_path):
    dest = thumbnail(fixture_film, 2.0, tmp_path / "a.jpg")
    marker = b"not-a-real-jpeg"
    dest.write_bytes(marker)
    again = thumbnail(fixture_film, 2.0, tmp_path / "a.jpg")
    assert again.read_bytes() == marker


def test_thumbnails_from_different_shots_differ(fixture_film, tmp_path):
    # fixture_film cuts every 5s between flat colours, so 2.5s and 7.5s are
    # different colours and must not produce identical bytes.
    first = thumbnail(fixture_film, 2.5, tmp_path / "a.jpg").read_bytes()
    second = thumbnail(fixture_film, 7.5, tmp_path / "b.jpg").read_bytes()
    assert first != second
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_thumbs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cutlist.media.thumbs'`

- [ ] **Step 3: Write the implementation**

Create `cutlist/media/thumbs.py`:

```python
from pathlib import Path

from cutlist.shell import run


def thumbnail(video: Path, at_seconds: float, dest: Path, *, width: int = 160) -> Path:
    """Extract one frame as a JPEG, or reuse the one already there.

    Generated on demand by `review` rather than during `draft`: a clip that is
    never reviewed never pays for its thumbnails, and they are regenerable, so
    they live on disk rather than in the database.
    """
    if dest.exists():
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-v", "error",
        # Before -i so ffmpeg seeks rather than decoding from the start; the
        # source is a feature-length video and this runs once per segment.
        "-ss", f"{max(at_seconds, 0.0):.3f}",
        "-i", str(video),
        "-frames:v", "1",
        # -2 keeps the height even, which libx264-encoded sources require.
        "-vf", f"scale={width}:-2",
        "-q:v", "4",
        str(dest),
    ])
    return dest
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_thumbs.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add cutlist/media/thumbs.py tests/test_thumbs.py
git commit -m "feat: extract segment thumbnails on demand"
```

---

### Task 7: `cutlist rate` and `cutlist ratings`

**Files:**
- Create: `cutlist/feedback/__init__.py`
- Create: `cutlist/feedback/rate.py`
- Modify: `cutlist/cli.py` (append two commands)
- Test: `tests/test_rate_cli.py`

**Interfaces:**
- Consumes: `store.clip_by_path`, `store.rate_clip`, `store.mark_shot`, `store.clip_detail`, `store.summary`, `schema.connect`.
- Produces:
  - `parse_segment_marks(text: str) -> list[tuple[int, str]]` — parses `"1:good,3:veto"` into `[(1, "good"), (3, "veto")]`. Positions are **1-based** as typed, matching what the review page shows. Raises `ValueError` on anything malformed.
  - CLI commands `rate` and `ratings`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rate_cli.py`:

```python
import pytest
from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.feedback.rate import parse_segment_marks

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path):
    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_film(conn, film_hash="abc", display_name="fixture.mp4", duration_s=30.0)
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="sha", preset_json="{}",
        caption_text="TEST", seed=1, cutlist_version="0.1.0", film_hashes=["abc"],
    )
    store.record_clip(
        conn, run_id=run_id, ordinal=1, path="output/01.mp4", duration_s=4.0,
        segments=[
            store.SegmentRecord("abc", 1.0, 3.0, 0.5, 3.5, 0),
            store.SegmentRecord("abc", 8.0, 10.0, 7.5, 10.5, 1),
        ],
    )
    return tmp_path


def test_parse_segment_marks_reads_pairs():
    assert parse_segment_marks("1:good,3:veto") == [(1, "good"), (3, "veto")]


def test_parse_segment_marks_tolerates_spaces():
    assert parse_segment_marks(" 2 : bad ") == [(2, "bad")]


@pytest.mark.parametrize("text", ["1", "1:", ":good", "1:sideways", "x:good", "1:good,"])
def test_parse_segment_marks_rejects_malformed_input(text):
    with pytest.raises(ValueError):
        parse_segment_marks(text)


def test_rate_records_a_verdict(workspace):
    result = runner.invoke(app, [
        "rate", "output/01.mp4", "fire", "--root", str(workspace),
    ])
    assert result.exit_code == 0, result.output
    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT verdict FROM clip_rating").fetchone()[0] == "fire"


def test_rate_records_segment_marks(workspace):
    result = runner.invoke(app, [
        "rate", "output/01.mp4", "ok", "--segments", "1:good,2:veto",
        "--root", str(workspace),
    ])
    assert result.exit_code == 0, result.output
    conn = connect(workspace / "cutlist.sqlite")
    marks = [r["mark"] for r in conn.execute(
        "SELECT mark FROM shot_rating ORDER BY seg_start_s"
    )]
    assert marks == ["good", "veto"]


def test_rate_rejects_an_unknown_clip(workspace):
    result = runner.invoke(app, [
        "rate", "output/nope.mp4", "fire", "--root", str(workspace),
    ])
    assert result.exit_code == 1
    assert "nope.mp4" in result.output


def test_rate_rejects_an_out_of_range_segment(workspace):
    result = runner.invoke(app, [
        "rate", "output/01.mp4", "ok", "--segments", "9:good", "--root", str(workspace),
    ])
    assert result.exit_code == 1
    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM shot_rating").fetchone()[0] == 0


def test_ratings_reports_counts(workspace):
    runner.invoke(app, ["rate", "output/01.mp4", "fire", "--root", str(workspace)])
    result = runner.invoke(app, ["ratings", "--root", str(workspace)])
    assert result.exit_code == 0
    assert "fire" in result.output


def test_ratings_json_is_machine_readable(workspace):
    import json
    runner.invoke(app, ["rate", "output/01.mp4", "no", "--root", str(workspace)])
    result = runner.invoke(app, ["ratings", "--json", "--root", str(workspace)])
    assert json.loads(result.output)["verdicts"]["no"] == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_rate_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cutlist.feedback'`

- [ ] **Step 3: Write the implementation**

Create `cutlist/feedback/__init__.py` as an empty file.

Create `cutlist/feedback/rate.py`:

```python
from cutlist.db.store import MARKS


def parse_segment_marks(text: str) -> list[tuple[int, str]]:
    """Parse `"1:good,3:veto"` into 1-based (position, mark) pairs.

    Positions are 1-based because that is what the review page prints under
    each segment, and typing what you can see beats an off-by-one.
    """
    pairs: list[tuple[int, str]] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise ValueError(f"empty entry in --segments: {text!r}")
        position, _, mark = chunk.partition(":")
        position, mark = position.strip(), mark.strip()
        if not position.isdigit():
            raise ValueError(f"segment position must be a number, got {position!r}")
        if mark not in MARKS:
            raise ValueError(f"mark must be one of {', '.join(MARKS)}, got {mark!r}")
        pairs.append((int(position), mark))
    return pairs
```

Append to `cutlist/cli.py`:

```python
@app.command()
@handle_errors
def rate(
    clip: str = typer.Argument(..., help="Path of the clip, as written by draft."),
    verdict: str = typer.Argument(..., help="fire, ok or no."),
    segments: str | None = typer.Option(
        None, "--segments", help='Marks by position, e.g. "1:good,3:veto".'
    ),
    note: str | None = typer.Option(None, "--note", help="Free text to store with the verdict."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Rate a clip, and optionally mark the segments inside it."""
    workspace = Workspace(root=root)
    conn = connect(workspace.database)

    # Normalised so `output/01.mp4` and `output\01.mp4` both resolve.
    wanted = Path(clip).as_posix()
    row = store.clip_by_path(conn, wanted)
    if row is None:
        raise LookupError(f"no recorded clip at {wanted}")

    marks = parse_segment_marks(segments) if segments else []
    detail = store.clip_detail(conn, row["id"])
    by_position = {s["position"] + 1: s["id"] for s in detail["segments"]}

    # Validated before anything is written, so a typo in one pair does not
    # leave half the marks recorded.
    for position, _ in marks:
        if position not in by_position:
            raise LookupError(
                f"clip has {len(by_position)} segments; no segment {position}"
            )

    store.rate_clip(conn, clip_id=row["id"], verdict=verdict, note=note)
    for position, mark in marks:
        store.mark_shot(conn, segment_id=by_position[position], mark=mark)

    typer.echo(f"{wanted}: {verdict}" + (f", {len(marks)} segment marks" if marks else ""))


@app.command()
@handle_errors
def ratings(
    as_json: bool = typer.Option(False, "--json", help="Emit the summary as JSON."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Report what has been rated so far."""
    conn = connect(Workspace(root=root).database)
    result = store.summary(conn)

    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return

    typer.echo(
        f"{result['films']} films, {result['runs']} runs, "
        f"{result['clips']} clips, {result['segments']} segments"
    )
    verdicts = result["verdicts"] or {}
    marks = result["marks"] or {}
    typer.echo("clips:    " + ", ".join(
        f"{k} {verdicts.get(k, 0)}" for k in store.VERDICTS
    ))
    typer.echo("segments: " + ", ".join(
        f"{k} {marks.get(k, 0)}" for k in store.MARKS
    ))
```

Extend the error boundary — `LookupError` now reaches the CLI. Change line 25 of `cutlist/cli.py`:

```python
HANDLED_ERRORS = (
    ToolError, PresetError, FontError, NotEnoughFootage, FileNotFoundError,
    LookupError, ValueError,
)
```

Add the import for the parser near the other `cutlist` imports:

```python
from cutlist.feedback.rate import parse_segment_marks
```

- [ ] **Step 4: Run the whole suite to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS, including 13 new tests

- [ ] **Step 5: Commit**

```bash
git add cutlist/feedback/ cutlist/cli.py tests/test_rate_cli.py
git commit -m "feat: rate clips and segments from the command line"
```

---

### Task 8: Review HTTP server

The page itself lands in Task 9. This task builds and tests the API it will call.

**Files:**
- Create: `cutlist/review/__init__.py`
- Create: `cutlist/review/server.py`
- Test: `tests/test_review_server.py`

**Interfaces:**
- Consumes: `store.clips_for_review`, `store.clip_detail`, `store.rate_clip`, `store.mark_shot`, `schema.connect`, `thumbs.thumbnail`.
- Produces:
  - `build_server(*, root: Path, port: int, film: str | None = None, preset: str | None = None, unrated_only: bool = True) -> ThreadingHTTPServer` — bound but not started; caller runs `serve_forever()`. Port `0` binds an ephemeral port, readable from `server.server_address[1]`.
  - Routes: `GET /` (HTML), `GET /api/clips`, `GET /api/clip/<id>`, `POST /api/ratings`, `GET /media/clip/<id>` (range-capable), `GET /media/thumb/<segment_id>`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_review_server.py`:

```python
import json
import threading
import urllib.error
import urllib.request

import pytest

from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.review.server import build_server


@pytest.fixture
def workspace(tmp_path, fixture_film):
    import shutil

    clip_dir = tmp_path / "output" / "fixture" / "p"
    clip_dir.mkdir(parents=True)
    shutil.copy(fixture_film, clip_dir / "01.mp4")

    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_film(conn, film_hash="abc", display_name="fixture.mp4", duration_s=30.0)
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="sha", preset_json="{}",
        caption_text="TEST", seed=1, cutlist_version="0.1.0", film_hashes=["abc"],
    )
    store.record_clip(
        conn, run_id=run_id, ordinal=1,
        path="output/fixture/p/01.mp4", duration_s=4.0,
        segments=[
            store.SegmentRecord("abc", 2.0, 4.0, 0.0, 5.0, 0),
            store.SegmentRecord("abc", 7.0, 9.0, 5.0, 10.0, 1),
        ],
    )
    # The source has to be findable for thumbnails; review resolves it from
    # the film's display_name under the workspace input directory.
    (tmp_path / "input").mkdir()
    shutil.copy(fixture_film, tmp_path / "input" / "fixture.mp4")
    return tmp_path


@pytest.fixture
def server(workspace):
    httpd = build_server(root=workspace, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url) as response:
        return response.status, response.read(), dict(response.headers)


def _post(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


def test_root_serves_the_page(server):
    status, body, headers = _get(f"{server}/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<title>" in body


def test_page_makes_no_external_requests(server):
    _, body, _ = _get(f"{server}/")
    text = body.decode()
    assert "http://" not in text.replace("http://127.0.0.1", "")
    assert "https://" not in text
    assert "cdn" not in text.lower()


def test_clips_endpoint_lists_unrated_clips(server):
    status, body, _ = _get(f"{server}/api/clips")
    assert status == 200
    clips = json.loads(body)
    assert len(clips) == 1
    assert clips[0]["segment_count"] == 2


def test_clip_endpoint_returns_segments(server):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    status, body, _ = _get(f"{server}/api/clip/{clips[0]['id']}")
    assert status == 200
    detail = json.loads(body)
    assert [s["position"] for s in detail["segments"]] == [0, 1]


def test_unknown_clip_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{server}/api/clip/9999")
    assert exc.value.code == 404


def test_posting_a_verdict_persists_it(server, workspace):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    status, body = _post(f"{server}/api/ratings", {
        "clip_id": clips[0]["id"], "verdict": "fire",
    })
    assert status == 200 and body["ok"] is True

    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT verdict FROM clip_rating").fetchone()[0] == "fire"


def test_posting_segment_marks_persists_them(server, workspace):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    detail = json.loads(_get(f"{server}/api/clip/{clips[0]['id']}")[1])
    _post(f"{server}/api/ratings", {
        "clip_id": clips[0]["id"],
        "verdict": "ok",
        "marks": [{"segment_id": detail["segments"][0]["id"], "mark": "veto"}],
    })
    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT mark FROM shot_rating").fetchone()[0] == "veto"


def test_a_malformed_payload_is_rejected_and_changes_nothing(server, workspace):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{server}/api/ratings", {"clip_id": 1, "verdict": "sideways"})
    assert exc.value.code == 400

    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM clip_rating").fetchone()[0] == 0


def test_video_is_served(server):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    status, body, headers = _get(f"{server}/media/clip/{clips[0]['id']}")
    assert status == 200
    assert headers["Content-Type"] == "video/mp4"
    assert headers["Accept-Ranges"] == "bytes"
    assert len(body) > 0


def test_video_honours_a_range_request(server):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    request = urllib.request.Request(f"{server}/media/clip/{clips[0]['id']}")
    request.add_header("Range", "bytes=0-99")
    with urllib.request.urlopen(request) as response:
        assert response.status == 206
        assert response.headers["Content-Range"].startswith("bytes 0-99/")
        assert len(response.read()) == 100


def test_thumbnail_is_served(server):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    detail = json.loads(_get(f"{server}/api/clip/{clips[0]['id']}")[1])
    status, body, headers = _get(f"{server}/media/thumb/{detail['segments'][0]['id']}")
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body[:2] == b"\xff\xd8"


def test_the_server_writes_no_sql_of_its_own():
    """store.py is the only module that talks to the database.

    Two rating paths (web and CLI) only stay consistent if neither grows its
    own queries, so this is asserted rather than left to review.
    """
    from pathlib import Path

    source = Path("cutlist/review/server.py").read_text(encoding="utf-8")
    for keyword in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert keyword not in source, f"{keyword.strip()} found in server.py"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_review_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cutlist.review'`

- [ ] **Step 3: Write the implementation**

Create `cutlist/review/__init__.py` as an empty file.

Create `cutlist/review/server.py`:

```python
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.media.thumbs import thumbnail

PAGE = Path(__file__).with_name("page.html")

_CLIP = re.compile(r"^/api/clip/(\d+)$")
_MEDIA_CLIP = re.compile(r"^/media/clip/(\d+)$")
_MEDIA_THUMB = re.compile(r"^/media/thumb/(\d+)$")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")

# Enough of a clip to fill a buffer without materialising a whole file in RAM.
_CHUNK = 64 * 1024


class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the review page and the JSON it talks to.

    One connection per request and a fresh sqlite connection per request:
    sqlite3 objects are not shareable across threads, and ThreadingHTTPServer
    hands each request to its own thread.
    """

    server_version = "cutlist-review"

    # Silence the default stderr access log; a local review tool logging every
    # range request drowns anything worth reading.
    def log_message(self, format, *args):  # noqa: A002 - signature is fixed
        pass

    # -- helpers ---------------------------------------------------------

    @property
    def config(self) -> dict:
        return self.server.cutlist  # type: ignore[attr-defined]

    def _db(self):
        return connect(self.config["root"] / "cutlist.sqlite")

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _source_for(self, film_hash: str, conn) -> Path | None:
        """Locate the original video a segment was cut from.

        The database records a display name, not a path, because the file can
        move. Look for it where the workspace keeps sources.
        """
        name = store.film_display_name(conn, film_hash)
        if name is None:
            return None
        candidate = self.config["root"] / "input" / name
        return candidate if candidate.exists() else None

    def _send_file(self, path: Path, content_type: str) -> None:
        """Serve a file, honouring a single-range request.

        Video needs this: without 206 support the browser cannot seek, and
        `J`/`K`/`L` shuttling does nothing.
        """
        size = path.stat().st_size
        header = self.headers.get("Range", "")
        match = _RANGE.match(header) if header else None

        if match is None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with path.open("rb") as handle:
                self._pump(handle, size)
            return

        first, last = match.group(1), match.group(2)
        start = int(first) if first else 0
        end = int(last) if last else size - 1
        end = min(end, size - 1)

        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            self._pump(handle, length)

    def _pump(self, handle, remaining: int) -> None:
        while remaining > 0:
            block = handle.read(min(_CHUNK, remaining))
            if not block:
                return
            self.wfile.write(block)
            remaining -= len(block)

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]

        if path == "/":
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/clips":
            conn = self._db()
            self._send_json(store.clips_for_review(
                conn,
                film=self.config["film"],
                preset=self.config["preset"],
                unrated_only=self.config["unrated_only"],
            ))
            return

        match = _CLIP.match(path)
        if match:
            detail = store.clip_detail(self._db(), int(match.group(1)))
            if detail is None:
                self._send_error(404, "no such clip")
                return
            self._send_json(detail)
            return

        match = _MEDIA_CLIP.match(path)
        if match:
            relative = store.clip_path(self._db(), int(match.group(1)))
            if relative is None:
                self._send_error(404, "no such clip")
                return
            clip_path = self.config["root"] / relative
            if not clip_path.exists():
                self._send_error(404, "clip file is missing")
                return
            self._send_file(clip_path, "video/mp4")
            return

        match = _MEDIA_THUMB.match(path)
        if match:
            self._serve_thumb(int(match.group(1)))
            return

        self._send_error(404, "not found")

    def _serve_thumb(self, segment_id: int) -> None:
        conn = self._db()
        segment = store.segment_by_id(conn, segment_id)
        if segment is None:
            self._send_error(404, "no such segment")
            return

        source = self._source_for(segment["film_hash"], conn)
        if source is None:
            self._send_error(404, "source video not found")
            return

        cache = self.config["root"] / "cache" / "thumbs"
        midpoint = (segment["seg_start_s"] + segment["seg_end_s"]) / 2
        dest = thumbnail(source, midpoint, cache / f"segment_{segment_id}.jpg")
        self._send_file(dest, "image/jpeg")

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path.split("?", 1)[0] != "/api/ratings":
            self._send_error(404, "not found")
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_error(400, "body is not valid JSON")
            return

        conn = self._db()
        clip_id = payload.get("clip_id")
        verdict = payload.get("verdict")
        marks = payload.get("marks") or []

        # Everything is validated before anything is written, so a bad mark
        # never leaves a verdict recorded without it.
        if not isinstance(clip_id, int) or store.clip_detail(conn, clip_id) is None:
            self._send_error(400, "clip_id must name a recorded clip")
            return
        if verdict is not None and verdict not in store.VERDICTS:
            self._send_error(400, f"verdict must be one of {', '.join(store.VERDICTS)}")
            return
        for entry in marks:
            if not isinstance(entry, dict) or entry.get("mark") not in store.MARKS:
                self._send_error(400, f"mark must be one of {', '.join(store.MARKS)}")
                return
            if not isinstance(entry.get("segment_id"), int):
                self._send_error(400, "each mark needs an integer segment_id")
                return

        try:
            for entry in marks:
                store.mark_shot(
                    conn, segment_id=entry["segment_id"], mark=entry["mark"]
                )
            if verdict is not None:
                store.rate_clip(conn, clip_id=clip_id, verdict=verdict)
        except (ValueError, LookupError) as exc:
            self._send_error(400, str(exc))
            return

        self._send_json({"ok": True})


def build_server(
    *,
    root: Path,
    port: int,
    film: str | None = None,
    preset: str | None = None,
    unrated_only: bool = True,
) -> ThreadingHTTPServer:
    """Bind the review server without starting it.

    Returned unstarted so tests can run it on an ephemeral port in a thread
    and the CLI can print the URL before blocking on serve_forever().
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ReviewHandler)
    httpd.cutlist = {  # type: ignore[attr-defined]
        "root": Path(root),
        "film": film,
        "preset": preset,
        "unrated_only": unrated_only,
    }
    return httpd
```

Create a minimal `cutlist/review/page.html` so the server tests pass — Task 9
replaces it wholesale:

```html
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>cutlist review</title></head>
<body><main id="app"></main></body>
</html>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_review_server.py -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add cutlist/review/ tests/test_review_server.py
git commit -m "feat: review server with ranged video and rating API"
```

---

### Task 9: Review page and the `review` command

**Files:**
- Modify: `cutlist/review/page.html` (replace the placeholder entirely)
- Modify: `cutlist/cli.py` (append the `review` command)
- Test: `tests/test_review_cli.py`

**Interfaces:**
- Consumes: `build_server` from Task 8.
- Produces: CLI command `review`.

**Design constraints — check the finished page against every line:**
- Chrome is fully achromatic (`R=G=B`). The mat immediately around the video is a neutral mid-gray (`#2e2e2e`–`#383838`), never black — a black surround exaggerates apparent contrast so shadow detail in the frame cannot be judged.
- No accent colour within ~200px of the video frame. Colour is signal only: green `#6a9a5b`, amber `#b58c3a`, red `#a3564e`, none above 45% saturation.
- Segment strip is one non-wrapping row, segments abutting with 1px seams, width proportional to duration with a 64px floor. No rounded corners on thumbnails, no per-segment padding, no cards.
- Marks render as a 3px bar on the thumbnail's bottom edge. `veto` drops the thumbnail to 35% opacity with a diagonal hatch and adds no colour.
- 13px chrome, 12px data, 24–28px controls, `line-height: 1.35`, 4px spacing grid. Full viewport, no page scroll, no centred max-width column.
- `font-variant-numeric: tabular-nums` on every timecode and duration.
- **Forbidden:** Inter, Tailwind indigo/violet (`#6366f1`, `#8b5cf6`), any gradient, `backdrop-filter`, `border-radius` above 3px, stacked shadows, uniform 24px gaps, stroke-1.5 icon sets, pill radii, emoji as UI icons.

- [ ] **Step 1: Write the failing test**

Create `tests/test_review_cli.py`:

```python
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cutlist.cli import app

runner = CliRunner()
PAGE = Path("cutlist/review/page.html").read_text(encoding="utf-8")

FORBIDDEN = [
    "Inter", "#6366f1", "#8b5cf6", "radial-gradient", "backdrop-filter",
    "cdn.", "googleapis", "unpkg", "jsdelivr",
]


@pytest.mark.parametrize("marker", FORBIDDEN)
def test_page_avoids_generated_default_markers(marker):
    assert marker.lower() not in PAGE.lower()


def test_the_only_gradient_is_the_veto_hatch():
    """Decorative gradients are the slop marker; a 45-degree hatch is texture.

    `veto` has to remove presence rather than add colour, and a hatch is how
    that reads without spending the colour budget.
    """
    gradients = re.findall(r"[a-z-]*gradient\(", PAGE)
    assert set(gradients) <= {"repeating-linear-gradient("}, gradients


def test_page_inlines_its_css_and_js():
    assert "<style>" in PAGE and "<script>" in PAGE
    assert "<link" not in PAGE
    assert 'src="http' not in PAGE


def test_page_uses_tabular_numerals():
    assert "tabular-nums" in PAGE


def test_page_declares_every_keybinding():
    for key in ["KeyF", "KeyO", "KeyN", "KeyG", "KeyB", "KeyV", "KeyZ", "Space"]:
        assert key in PAGE, f"missing binding for {key}"


def test_page_has_no_large_border_radius():
    radii = [int(v) for v in re.findall(r"border-radius:\s*(\d+)px", PAGE)]
    assert all(r <= 3 for r in radii), radii


def test_review_reports_a_taken_port(tmp_path):
    import socket

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.listen(1)
    try:
        result = runner.invoke(app, [
            "review", "--root", str(tmp_path), "--port", str(port), "--no-open",
        ])
        assert result.exit_code == 1
        assert str(port) in result.output
    finally:
        holder.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_review_cli.py -v`
Expected: FAIL — the placeholder page has no `<style>`, no keybindings; `review` is not a command

- [ ] **Step 3: Write the implementation**

Replace `cutlist/review/page.html` entirely:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cutlist review</title>
<style>
/* Achromatic throughout: a tinted surround shifts the viewer's white point
   through chromatic adaptation, which changes how the footage reads. */
:root {
  --chrome: #171717;
  --panel: #1f1f1f;
  --mat: #333333;
  --seam: #0e0e0e;
  --text: #d6d6d6;
  --dim: #8a8a8a;
  --good: #6a9a5b;
  --bad: #b58c3a;
  --veto: #a3564e;
  --mono: Consolas, "SF Mono", Menlo, "DejaVu Sans Mono", monospace;
  --sans: ui-sans-serif, system-ui, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
body {
  background: var(--chrome); color: var(--text);
  font: 13px/1.35 var(--sans);
  display: grid; grid-template-rows: auto 1fr auto auto;
}
header, footer {
  padding: 6px 12px; background: var(--panel);
  border-bottom: 1px solid var(--seam);
  display: flex; gap: 16px; align-items: center;
}
footer { border-bottom: 0; border-top: 1px solid var(--seam); }
.count, .echo, .tc { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.dim { color: var(--dim); }
kbd {
  font-family: var(--mono); font-size: 11px;
  padding: 1px 4px; border: 1px solid #3a3a3a; border-radius: 2px;
  background: #262626;
}
/* Mid-gray mat, never black: a black surround exaggerates apparent contrast
   and hides shadow detail in the frame. */
#stage {
  background: var(--mat);
  display: flex; align-items: center; justify-content: center;
  min-height: 0; padding: 24px;
}
video { max-width: 100%; max-height: 100%; display: block; background: #000; }
#strip {
  display: flex; background: var(--panel);
  border-top: 1px solid var(--seam); height: 108px;
}
.seg {
  position: relative; min-width: 64px;
  border-right: 1px solid var(--seam);
  display: flex; flex-direction: column; cursor: pointer;
}
.seg:last-child { border-right: 0; }
.seg img { width: 100%; flex: 1; object-fit: cover; display: block; }
.seg .meta {
  font-family: var(--mono); font-size: 12px;
  font-variant-numeric: tabular-nums;
  display: flex; justify-content: space-between;
  padding: 2px 4px; color: var(--dim);
}
.seg.sel { outline: 2px solid #cfcfcf; outline-offset: -2px; }
.seg::after {
  content: ""; position: absolute; left: 0; right: 0; bottom: 20px;
  height: 3px; background: transparent;
}
.seg.good::after { background: var(--good); }
.seg.bad::after  { background: var(--bad); }
/* Veto removes presence rather than adding colour -- it is destructive. */
.seg.veto img { opacity: 0.35; }
.seg.veto {
  background-image: repeating-linear-gradient(
    45deg, transparent 0 6px, rgba(0,0,0,0.35) 6px 12px);
}
#help {
  position: fixed; inset: 0; background: rgba(0,0,0,0.85);
  display: none; align-items: center; justify-content: center;
}
#help.on { display: flex; }
#help div { background: var(--panel); padding: 20px; border: 1px solid #3a3a3a; }
#help td { padding: 2px 12px 2px 0; }
</style>
</head>
<body>
<header>
  <strong>cutlist review</strong>
  <span class="count" id="count">—</span>
  <span class="dim" id="meta"></span>
</header>

<div id="stage"><video id="player" preload="metadata"></video></div>

<div id="strip"></div>

<footer>
  <span class="echo" id="echo">ready</span>
  <span class="dim">
    <kbd>f</kbd><kbd>o</kbd><kbd>n</kbd> verdict ·
    <kbd>1</kbd>–<kbd>9</kbd> then <kbd>g</kbd><kbd>b</kbd><kbd>v</kbd> mark ·
    <kbd>space</kbd> play · <kbd>j</kbd><kbd>k</kbd><kbd>l</kbd> shuttle ·
    <kbd>z</kbd> undo · <kbd>?</kbd> help
  </span>
</footer>

<div id="help"><div><table>
  <tr><td><kbd>space</kbd></td><td>play / pause</td></tr>
  <tr><td><kbd>j</kbd> <kbd>k</kbd> <kbd>l</kbd></td><td>rewind / pause / forward</td></tr>
  <tr><td><kbd>←</kbd> <kbd>→</kbd></td><td>step one frame</td></tr>
  <tr><td><kbd>1</kbd>–<kbd>9</kbd></td><td>select segment</td></tr>
  <tr><td><kbd>g</kbd> <kbd>b</kbd> <kbd>v</kbd></td><td>good / bad / veto</td></tr>
  <tr><td><kbd>f</kbd> <kbd>o</kbd> <kbd>n</kbd></td><td>fire / ok / no, then advance</td></tr>
  <tr><td><kbd>z</kbd></td><td>undo the last verdict</td></tr>
  <tr><td><kbd>?</kbd></td><td>close this</td></tr>
</table></div></div>

<script>
const player = document.getElementById("player");
const strip = document.getElementById("strip");
const echo = document.getElementById("echo");
const countEl = document.getElementById("count");
const metaEl = document.getElementById("meta");
const help = document.getElementById("help");

let clips = [], index = 0, detail = null, selected = 0, lastVerdict = null;

const tc = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const say = (m) => { echo.textContent = m; };

async function boot() {
  clips = await (await fetch("/api/clips")).json();
  if (!clips.length) { say("nothing left to review"); countEl.textContent = "0 / 0"; return; }
  await show(0);
}

async function show(i) {
  index = i; selected = 0;
  if (index >= clips.length) {
    player.removeAttribute("src"); strip.innerHTML = "";
    countEl.textContent = `${clips.length} / ${clips.length}`;
    say("batch complete"); return;
  }
  const clip = clips[index];
  detail = await (await fetch(`/api/clip/${clip.id}`)).json();
  countEl.textContent = `clip ${index + 1} / ${clips.length}`;
  metaEl.textContent = `${detail.preset_name} · "${detail.caption_text}" · seed ${detail.seed}`;
  player.src = `/media/clip/${clip.id}`;
  player.play().catch(() => {});
  draw();
}

function draw() {
  const total = detail.segments.reduce((a, s) => a + (s.seg_end_s - s.seg_start_s), 0);
  strip.innerHTML = "";
  detail.segments.forEach((s, i) => {
    const span = s.seg_end_s - s.seg_start_s;
    const el = document.createElement("div");
    // Width proportional to duration turns the strip into a timeline: the
    // clip's pacing is visible without reading a number.
    el.className = "seg" + (s.mark ? " " + s.mark : "") + (i === selected ? " sel" : "");
    el.style.flex = `${span / total} 1 0`;
    el.innerHTML =
      `<img src="/media/thumb/${s.id}" alt="">` +
      `<div class="meta"><span>${i + 1} ${tc(s.seg_start_s)}</span>` +
      `<span>${span.toFixed(1)}s</span></div>`;
    el.onclick = () => { selected = i; draw(); };
    strip.appendChild(el);
  });
}

async function post(body) {
  const r = await fetch("/api/ratings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) { say("rejected: " + (await r.json()).error); return false; }
  return true;
}

async function mark(kind) {
  const seg = detail.segments[selected];
  if (!seg) return;
  if (await post({ clip_id: clips[index].id, marks: [{ segment_id: seg.id, mark: kind }] })) {
    seg.mark = kind; draw();
    say(`seg ${selected + 1} → ${kind}`);
  }
}

async function verdict(kind) {
  const clip = clips[index];
  if (!clip) return;
  if (await post({ clip_id: clip.id, verdict: kind })) {
    lastVerdict = clip.id;
    say(`clip ${index + 1} → ${kind} · z undo`);
    await show(index + 1);
  }
}

async function undo() {
  if (lastVerdict === null) { say("nothing to undo"); return; }
  const target = clips.findIndex((c) => c.id === lastVerdict);
  lastVerdict = null;
  if (target >= 0) { await show(target); say("reopened — re-rate to overwrite"); }
}

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const code = e.code;

  if (code === "Slash" && e.shiftKey) { help.classList.toggle("on"); e.preventDefault(); return; }
  if (help.classList.contains("on")) { help.classList.remove("on"); return; }

  if (code === "Space") { player.paused ? player.play() : player.pause(); e.preventDefault(); return; }
  if (code === "KeyJ") { player.currentTime = Math.max(0, player.currentTime - 1); return; }
  if (code === "KeyK") { player.pause(); return; }
  if (code === "KeyL") { player.currentTime += 1; return; }
  if (code === "ArrowLeft") { player.currentTime = Math.max(0, player.currentTime - 0.04); return; }
  if (code === "ArrowRight") { player.currentTime += 0.04; return; }

  if (/^Digit[1-9]$/.test(code)) {
    const n = Number(code.slice(5)) - 1;
    if (detail && n < detail.segments.length) { selected = n; draw(); say(`seg ${n + 1}`); }
    return;
  }

  if (code === "KeyG") return void mark("good");
  if (code === "KeyB") return void mark("bad");
  if (code === "KeyV") return void mark("veto");
  if (code === "KeyF") return void verdict("fire");
  if (code === "KeyO") return void verdict("ok");
  if (code === "KeyN") return void verdict("no");
  if (code === "KeyZ") return void undo();
});

boot();
</script>
</body>
</html>
```

Append to `cutlist/cli.py`:

```python
@app.command()
@handle_errors
def review(
    film: str | None = typer.Option(None, "--film", help="Filter by film hash."),
    preset: str | None = typer.Option(None, "--preset", help="Filter by preset name."),
    port: int = typer.Option(8731, "--port", help="Port to serve on."),
    all_clips: bool = typer.Option(False, "--all", help="Include clips already rated."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open a browser."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Serve the local review page."""
    import webbrowser
    from cutlist.review.server import build_server

    try:
        httpd = build_server(
            root=root, port=port, film=film, preset=preset,
            unrated_only=not all_clips,
        )
    except OSError as exc:
        # Refuse rather than silently picking another port: a review URL you
        # did not ask for is worse than a clear failure.
        typer.echo(f"error: cannot bind port {port}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    typer.echo(f"review at {url}  (ctrl-c to stop)")
    if open_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nstopped")
    finally:
        httpd.server_close()
```

- [ ] **Step 4: Run the whole suite to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS — every test, including 12 new page and CLI tests

- [ ] **Step 5: Update the README**

Add to the Commands block in `README.md`:

```
cutlist review [--film HASH] [--preset NAME] [--port N] [--all]
cutlist rate <clip-path> <fire|ok|no> [--segments "1:good,3:veto"]
cutlist ratings [--json]
```

and a short section after Presets:

```markdown
## Rating

`draft` records what each clip was made of into `cutlist.sqlite` at the
workspace root: the run's seed and resolved preset, every clip, and every
segment with both its own timecodes and those of the shot it came from.

`cutlist review` serves a local page for watching a batch and rating it —
`f`/`o`/`n` for the clip verdict, `1`–`9` then `g`/`b`/`v` to mark individual
segments, `z` to undo, `?` for the full list. `cutlist rate` does the same
from the terminal.

Nothing consumes the ratings yet. This release collects them; scoring uses
them later.
```

- [ ] **Step 6: Commit**

```bash
git add cutlist/review/page.html cutlist/cli.py tests/test_review_cli.py README.md
git commit -m "feat: keyboard-driven review page and review command"
```

---

## Done when

- `.venv\Scripts\python.exe -m pytest` passes with no failures or errors.
- `cutlist draft <video> --preset presets/real_saturday.yaml --count 5` writes five clips and creates `cutlist.sqlite` with one `run`, one `run_film`, five `clip` rows, and their `segment` rows.
- `cutlist review` opens a page that plays each clip, shows its segments as a proportional strip, and accepts every documented key.
- `cutlist rate output/<video>/<preset>/01.mp4 fire --segments "1:good,2:veto"` writes one `clip_rating` and two `shot_rating` rows.
- `cutlist ratings --json` reports non-zero counts.
- Re-running `draft` against the same video adds a second run without touching the first run's rows.
- Deleting a `clip` row removes its `clip_rating` rows and leaves its `shot_rating` rows with `segment_id IS NULL`.
- Nowhere in `cutlist/` does any code read `clip_rating` or `shot_rating` to influence selection.
