import tempfile
from pathlib import Path

from cutlist.shell import run


def thumbnail_bytes(video: Path, at_seconds: float, *, width: int = 160) -> bytes:
    """Extract one frame as JPEG bytes.

    Returned as bytes rather than written to a path because the caller stores
    them in the database: a thumbnail has to outlive the source video, and a
    file under cache/ does not.

    Written to a temporary file and read back rather than piped through stdout,
    because shell.run decodes stdout as UTF-8 text and would corrupt the JPEG.
    """
    with tempfile.TemporaryDirectory() as scratch:
        dest = Path(scratch) / "frame.jpg"
        run([
            "ffmpeg", "-y", "-v", "error",
            # Before -i so ffmpeg seeks rather than decoding from the start;
            # the source is a feature-length video and this runs once per
            # segment.
            "-ss", f"{max(at_seconds, 0.0):.3f}",
            "-i", str(video),
            "-frames:v", "1",
            # -2 keeps the height even, which libx264-encoded sources require.
            "-vf", f"scale={width}:-2",
            "-q:v", "4",
            str(dest),
        ])
        return dest.read_bytes()
