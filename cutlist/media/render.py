import shutil
from dataclasses import dataclass
from pathlib import Path

from cutlist.presets import OutputSpec
from cutlist.shell import run


@dataclass(frozen=True)
class Segment:
    """A slice of the source film, in source timecodes."""

    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def encode_segment(
    film: Path,
    segment: Segment,
    caption_png: Path,
    output: OutputSpec,
    dest: Path,
) -> Path:
    """Cut one segment, letterbox it to the output size, and burn in the caption.

    The caption never changes within a clip, so compositing it here means the
    whole pipeline needs exactly one encode pass. Every segment then starts on
    a keyframe, which is what makes the later concat safe.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    scale = (
        f"[0:v]scale={output.width}:{output.height}"
        ":force_original_aspect_ratio=decrease,"
        f"pad={output.width}:{output.height}:-1:-1:color=black,"
        f"fps={output.fps},setsar=1[v];[v][1:v]overlay=0:0"
    )

    run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{segment.start:.3f}",
        "-i", str(film),
        "-i", str(caption_png),
        "-t", f"{segment.duration:.3f}",
        "-an",
        "-filter_complex", scale,
        "-c:v", "libx264", "-crf", str(output.crf), "-pix_fmt", "yuv420p",
        str(dest),
    ])
    return dest


def concat(parts: list[Path], dest: Path) -> Path:
    """Join encoded segments without re-encoding."""
    if not parts:
        raise ValueError("nothing to concatenate")

    dest.parent.mkdir(parents=True, exist_ok=True)
    listing = dest.parent / f"{dest.stem}_parts.txt"
    listing.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts),
        encoding="utf-8",
    )

    try:
        run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(listing),
            "-c", "copy",
            str(dest),
        ])
    finally:
        listing.unlink(missing_ok=True)
    return dest


def render_clip(
    film: Path,
    segments: list[Segment],
    caption_png: Path,
    output: OutputSpec,
    dest: Path,
    scratch: Path,
) -> Path:
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        parts = [
            encode_segment(
                film, segment, caption_png, output, scratch / f"seg_{i:02d}.mp4"
            )
            for i, segment in enumerate(segments)
        ]
        concat(parts, dest)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return dest
