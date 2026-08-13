import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from cutlist.presets import OutputSpec
from cutlist.shell import run


@dataclass(frozen=True)
class Segment:
    """A slice of the source video, in source timecodes."""

    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def encode_segment(
    video: Path,
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
        "-i", str(video),
        "-i", str(caption_png),
        "-t", f"{segment.duration:.3f}",
        "-an",
        "-filter_complex", scale,
        "-c:v", "libx264", "-crf", str(output.crf), "-pix_fmt", "yuv420p",
        str(dest),
    ])
    return dest


def _concat_line(part: Path) -> str:
    """Format one entry for the concat demuxer's file listing.

    The demuxer parses each path like a single-quoted shell token, so a
    literal `'` in the path has to close the quote, escape one, and reopen
    it -- otherwise any video with an apostrophe in its name breaks the list.
    """
    escaped = part.resolve().as_posix().replace("'", "'\\''")
    return f"file '{escaped}'"


def concat(parts: list[Path], dest: Path) -> Path:
    """Join encoded segments without re-encoding, replacing `dest` atomically.

    ffmpeg writes to a sibling temporary and the result is moved onto `dest`
    only once a whole file exists, so a failed join cannot damage whatever
    `dest` already held. `rerender` depends on this: there `dest` is by
    definition a clip that has already been rated, and destroying it on a
    failed rebuild would take a judgement with it.

    The temporary is a sibling of `dest` rather than of `parts` because
    `os.replace` is only atomic within one filesystem, and the caller's
    scratch lives under `cache/`, which need not share a filesystem with
    `output/`. It keeps `dest`'s suffix because ffmpeg picks its muxer from
    the output extension.
    """
    if not parts:
        raise ValueError("nothing to concatenate")

    dest.parent.mkdir(parents=True, exist_ok=True)
    # parts live in the caller's scratch directory, not dest's. Writing the
    # listing there too -- rather than next to dest -- keeps two renders
    # that happen to share a dest stem from colliding, and means a crash
    # hard enough to skip the finally below still only leaves a stray file
    # in scratch (which render_clip discards) rather than in the user's
    # output directory.
    listing = parts[0].parent / f"{dest.stem}_parts.txt"
    listing.write_text(
        "\n".join(_concat_line(p) for p in parts),
        encoding="utf-8",
    )

    # Randomly suffixed so two renders racing for the same dest cannot stage
    # over each other, and dot-prefixed so a crash hard enough to skip the
    # finally below leaves something obviously not a clip.
    staged = dest.with_name(f".{dest.stem}.{uuid.uuid4().hex[:8]}{dest.suffix}")
    try:
        run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(listing),
            "-c", "copy",
            str(staged),
        ])
        os.replace(staged, dest)
    finally:
        listing.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
    return dest


def render_clip(
    video: Path,
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
                video, segment, caption_png, output, scratch / f"seg_{i:02d}.mp4"
            )
            for i, segment in enumerate(segments)
        ]
        concat(parts, dest)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return dest
