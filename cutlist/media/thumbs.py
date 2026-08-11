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
