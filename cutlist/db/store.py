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

    video_hash: str
    seg_start_s: float
    seg_end_s: float
    shot_start_s: float
    shot_end_s: float
    shot_index: int | None = None


def record_video(
    conn: sqlite3.Connection,
    *,
    video_hash: str,
    display_name: str,
    duration_s: float | None = None,
    fps: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Register a source, or refresh what we know about one already seen."""
    now = _now()
    with conn:
        conn.execute(
            """
            INSERT INTO video (video_hash, display_name, duration_s, fps, width, height,
                               first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (video_hash) DO UPDATE SET
                display_name = excluded.display_name,
                duration_s   = COALESCE(excluded.duration_s, video.duration_s),
                fps          = COALESCE(excluded.fps, video.fps),
                width        = COALESCE(excluded.width, video.width),
                height       = COALESCE(excluded.height, video.height),
                last_seen_at = excluded.last_seen_at
            """,
            (video_hash, display_name, duration_s, fps, width, height, now, now),
        )


def start_run(
    conn: sqlite3.Connection,
    *,
    preset_name: str,
    preset_sha256: str,
    preset_json: str,
    caption_text: str,
    seed: int,
    cutlist_version: str,
    video_hashes: list[str],
) -> int:
    """Open a run and record which sources it was pointed at.

    Called before any clip is rendered, so a run that fails partway still
    leaves a record of its inputs.
    """
    with conn:
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
            "INSERT OR IGNORE INTO run_video (run_id, video_hash) VALUES (?, ?)",
            [(run_id, video_hash) for video_hash in video_hashes],
        )
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
    with conn:
        cursor = conn.execute(
            "INSERT INTO clip (run_id, ordinal, path, duration_s) VALUES (?, ?, ?, ?)",
            (run_id, ordinal, path, duration_s),
        )
        clip_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO segment (clip_id, position, video_hash, seg_start_s, seg_end_s,
                                 shot_start_s, shot_end_s, shot_index)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (clip_id, position, s.video_hash, s.seg_start_s, s.seg_end_s,
                 s.shot_start_s, s.shot_end_s, s.shot_index)
                for position, s in enumerate(segments)
            ],
        )
    return clip_id


VERDICTS = ("fire", "ok", "no")
MARKS = ("good", "bad", "veto")


class RatingError(ValueError):
    """A rating was malformed -- a verdict, mark, or --segments string."""


class RatingNotFound(LookupError):
    """A rating names a clip or segment that is not recorded."""


def rate_clip(
    conn: sqlite3.Connection, *, clip_id: int, verdict: str, note: str | None = None
) -> int:
    """Record a verdict on one assembled clip.

    Append-only: re-rating a clip adds a row rather than replacing one, so
    changing your mind is itself recorded.
    """
    if verdict not in VERDICTS:
        raise RatingError(f"verdict must be one of {', '.join(VERDICTS)}, got {verdict!r}")
    with conn:
        cursor = conn.execute(
            "INSERT INTO clip_rating (clip_id, verdict, note, created_at) VALUES (?, ?, ?, ?)",
            (clip_id, verdict, note, _now()),
        )
    return int(cursor.lastrowid)


def mark_shot(
    conn: sqlite3.Connection, *, segment_id: int, mark: str, note: str | None = None
) -> int:
    """Record a mark on the footage a segment was cut from.

    The segment's spans are copied onto the rating rather than referenced,
    so the judgement outlives the clip that occasioned it.
    """
    if mark not in MARKS:
        raise RatingError(f"mark must be one of {', '.join(MARKS)}, got {mark!r}")
    segment = conn.execute("SELECT * FROM segment WHERE id = ?", (segment_id,)).fetchone()
    if segment is None:
        raise RatingNotFound(f"no such segment: {segment_id}")

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO shot_rating (video_hash, seg_start_s, seg_end_s, shot_start_s,
                                     shot_end_s, mark, segment_id, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (segment["video_hash"], segment["seg_start_s"], segment["seg_end_s"],
             segment["shot_start_s"], segment["shot_end_s"], mark, segment_id, note, _now()),
        )
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
    video: str | None = None,
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
    if video is not None:
        sql += """ AND EXISTS (
            SELECT 1 FROM clip_video
            WHERE clip_video.clip_id = clip.id AND clip_video.video_hash = ?
        )"""
        params.append(video)
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
        SELECT segment.id, segment.position, segment.video_hash,
               segment.seg_start_s, segment.seg_end_s,
               segment.shot_start_s, segment.shot_end_s, segment.shot_index,
               video.display_name,
               ({_LATEST_MARK}) AS mark
        FROM segment JOIN video ON video.video_hash = segment.video_hash
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


def video_display_name(conn: sqlite3.Connection, video_hash: str) -> str | None:
    row = conn.execute(
        "SELECT display_name FROM video WHERE video_hash = ?", (video_hash,)
    ).fetchone()
    return None if row is None else row["display_name"]


def summary(conn: sqlite3.Connection) -> dict:
    """Counts for `cutlist ratings`."""
    def _counts(sql: str) -> dict[str, int]:
        return {row[0]: row[1] for row in conn.execute(sql)}

    return {
        "videos": conn.execute("SELECT COUNT(*) FROM video").fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM run").fetchone()[0],
        "clips": conn.execute("SELECT COUNT(*) FROM clip").fetchone()[0],
        "segments": conn.execute("SELECT COUNT(*) FROM segment").fetchone()[0],
        "verdicts": _counts(
            "SELECT verdict, COUNT(*) FROM clip_rating GROUP BY verdict"
        ),
        "marks": _counts("SELECT mark, COUNT(*) FROM shot_rating GROUP BY mark"),
    }
