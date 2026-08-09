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
