from dataclasses import dataclass, replace
from pathlib import Path

import yaml


class PresetError(ValueError):
    """A preset is missing something, or asks for something impossible."""


@dataclass(frozen=True)
class CaptionSpec:
    text: str
    position: str = "top_center"
    font: str | None = None
    size_frac: float = 0.065
    fill: str = "#FFFFFF"
    outline: str = "#000000"
    outline_frac: float = 0.006


@dataclass(frozen=True)
class RhythmSpec:
    min_segments: int
    max_segments: int
    min_seconds: float
    target_seconds: float
    max_seconds: float
    min_total: float
    max_total: float


@dataclass(frozen=True)
class OutputSpec:
    width: int = 854
    height: int = 480
    fps: int = 25
    crf: int = 20


@dataclass(frozen=True)
class Preset:
    name: str
    caption: CaptionSpec
    rhythm: RhythmSpec
    output: OutputSpec

    def with_caption(self, text: str) -> "Preset":
        return replace(self, caption=replace(self.caption, text=text))


def load_preset(path: Path) -> Preset:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    name = raw.get("name") or path.stem
    caption = _caption(raw.get("caption") or {})
    rhythm = _rhythm(raw.get("rhythm") or {})
    output = OutputSpec(**(raw.get("output") or {}))

    _validate(rhythm)
    return Preset(name=name, caption=caption, rhythm=rhythm, output=output)


def _caption(block: dict) -> CaptionSpec:
    if not block.get("text"):
        raise PresetError("caption.text is required and must not be empty")
    return CaptionSpec(**block)


def _rhythm(block: dict) -> RhythmSpec:
    try:
        segments = block["segments"]
        duration = block["seg_duration"]
        total = block["total"]
        return RhythmSpec(
            min_segments=int(segments["min"]),
            max_segments=int(segments["max"]),
            min_seconds=float(duration["min"]),
            target_seconds=float(duration["target"]),
            max_seconds=float(duration["max"]),
            min_total=float(total["min"]),
            max_total=float(total["max"]),
        )
    except KeyError as exc:
        raise PresetError(f"rhythm is missing {exc}") from exc


def _validate(rhythm: RhythmSpec) -> None:
    if rhythm.min_segments > rhythm.max_segments:
        raise PresetError("rhythm.segments.min exceeds segments.max")
    if rhythm.min_seconds > rhythm.max_seconds:
        raise PresetError("rhythm.seg_duration.min exceeds seg_duration.max")
    if not rhythm.min_seconds <= rhythm.target_seconds <= rhythm.max_seconds:
        raise PresetError("rhythm.seg_duration.target is outside min..max")
    if rhythm.min_total > rhythm.max_total:
        raise PresetError("rhythm.total.min exceeds total.max")

    # Even packing every segment at its longest duration must reach total.min,
    # and packing every segment at its shortest duration must not overshoot total.max.
    longest = rhythm.max_segments * rhythm.max_seconds
    shortest = rhythm.min_segments * rhythm.min_seconds
    if rhythm.min_total > longest:
        raise PresetError(
            f"rhythm.total.min of {rhythm.min_total}s is unreachable: "
            f"at most {longest}s fits in {rhythm.max_segments} segments"
        )
    if rhythm.max_total < shortest:
        raise PresetError(
            f"rhythm.total.max of {rhythm.max_total}s is unreachable: "
            f"{rhythm.min_segments} segments run at least {shortest}s"
        )
