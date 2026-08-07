import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from cutlist.shell import ToolError, run


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def _pick_video_stream(streams: list[dict], path: Path) -> dict:
    # Embedded cover art (e.g. an mjpeg thumbnail) shows up as its own
    # video stream, so picking "the" video stream by position or an
    # unfiltered first-match would silently return the thumbnail instead
    # of the film on files that carry one.
    candidates = [
        s for s in streams
        if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    if not candidates:
        raise ToolError(f"no video stream found in {path}")
    return max(candidates, key=lambda s: int(s["width"]) * int(s["height"]))


def probe(path: Path) -> VideoInfo:
    payload = json.loads(run([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]))

    streams = payload["streams"]
    video = _pick_video_stream(streams, path)

    return VideoInfo(
        path=path,
        duration=float(payload["format"]["duration"]),
        width=int(video["width"]),
        height=int(video["height"]),
        fps=float(Fraction(video["r_frame_rate"])),
        has_audio=any(s["codec_type"] == "audio" for s in streams),
    )
