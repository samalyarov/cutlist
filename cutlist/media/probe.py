import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from cutlist.shell import run


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(path: Path) -> VideoInfo:
    payload = json.loads(run([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]))

    streams = payload["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")

    return VideoInfo(
        path=path,
        duration=float(payload["format"]["duration"]),
        width=int(video["width"]),
        height=int(video["height"]),
        fps=float(Fraction(video["r_frame_rate"])),
        has_audio=any(s["codec_type"] == "audio" for s in streams),
    )
