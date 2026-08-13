"""Extracting whole shots into a reusable library.

A library clip is a master: source resolution, no caption, no letterbox, audio
kept when the source has it. Those are decisions belonging to a finished clip,
not to the footage it was cut from.
"""

from collections.abc import Callable
from pathlib import Path

from cutlist.db import store
from cutlist.media.probe import probe
from cutlist.media.shots import Shot, detect_shots
from cutlist.paths import video_id
from cutlist.shell import run


def _timecode(seconds: float) -> str:
    """`00h12m34.567s` -- millisecond precision because shot boundaries can
    fall inside the same second and whole-second names would collide."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours):02d}h{int(minutes):02d}m{secs:06.3f}s"


def library_path(workspace, video: Path, start_s: float, duration_s: float) -> Path:
    """Timecode-first, not id-first: the point of a browsable directory is
    finding footage by eye, and a timecode says where a clip came from."""
    directory = workspace.library / video.stem
    return directory / f"{_timecode(start_s)}__{duration_s:.2f}s.mp4"


def extract_shot(video: Path, shot: Shot, dest: Path, *, crf: int = 18) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-v", "error",
        # Before -i so ffmpeg seeks rather than decoding from the start.
        "-ss", f"{shot.start:.3f}",
        "-i", str(video),
        "-t", f"{shot.duration:.3f}",
        # No scale filter: source resolution is the point of a master.
        "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        # Audio survives when present; a library clip is raw material for
        # editing elsewhere, and audio dropped here cannot be recovered.
        "-c:a", "aac", "-b:a", "192k",
        str(dest),
    ])
    return dest


def extract_all(
    conn,
    *,
    video: Path,
    workspace,
    crf: int = 18,
    on_progress: Callable[[int, int, Shot, str], None] | None = None,
) -> tuple[int, int]:
    """Extract every shot of `video` into the library, skipping what is
    already there.

    Idempotency has two halves. The database half is `record_library_clip`'s
    `ON CONFLICT DO NOTHING`: a shot found twice never gets a second row. The
    filesystem half is here -- a row existing is not enough to skip the
    re-encode, because the row and the file it names can disagree (the file
    can be deleted, moved, or never finish writing). Only a row *and* a file
    that both exist mean the work is already done; anything else re-encodes,
    trusting the row (via `record_library_clip`'s own conflict handling) to
    settle on the id that was already assigned.

    `on_progress`, when given, is called once per shot as
    `on_progress(index, total, shot, status)` with `index` 1-based and
    `status` one of `"added"` or `"skipped"`.
    """
    info = probe(video)
    video_hash = video_id(video)
    store.record_video(
        conn,
        video_hash=video_hash,
        display_name=video.name,
        duration_s=info.duration,
        fps=info.fps,
        width=info.width,
        height=info.height,
    )

    shots = detect_shots(video)
    added = skipped = 0
    total = len(shots)

    for index, shot in enumerate(shots, start=1):
        dest = library_path(workspace, video, shot.start, shot.duration)
        existing = store.library_clip_at(
            conn, video_hash=video_hash, start_s=shot.start, end_s=shot.end
        )

        if existing is not None and dest.exists():
            skipped += 1
            status = "skipped"
        else:
            extract_shot(video, shot, dest, crf=crf)
            store.record_library_clip(
                conn,
                video_hash=video_hash,
                start_s=shot.start,
                end_s=shot.end,
                shot_index=shot.index,
                path=dest.relative_to(workspace.root).as_posix(),
                duration_s=shot.duration,
            )
            added += 1
            status = "added"

        if on_progress is not None:
            on_progress(index, total, shot, status)

    return added, skipped


def estimate(video: Path, shots: list[Shot]) -> str:
    """A sentence stating the cost before it is paid.

    Extracting every shot is effectively a full transcode of the source, so
    the estimate is real arithmetic on the source's own duration and file
    size -- the total footage to encode, and its size at the source's own
    bitrate -- not a placeholder that reads the same for a 10s clip and a
    two-hour film.
    """
    info = probe(video)
    total_s = sum(shot.duration for shot in shots)
    bitrate = video.stat().st_size / info.duration if info.duration > 0 else 0.0
    estimated_mb = (bitrate * total_s) / (1024 * 1024)
    minutes = total_s / 60
    return f"~{minutes:.1f} min of footage, ~{estimated_mb:.0f} MB at the source's bitrate"
