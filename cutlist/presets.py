from dataclasses import dataclass, fields, replace
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
    output = _build(OutputSpec, raw.get("output") or {}, "output")

    _validate(rhythm)
    return Preset(name=name, caption=caption, rhythm=rhythm, output=output)


def _check_keys(block: dict, known: set[str], label: str) -> None:
    unknown = sorted(set(block) - known)
    if unknown:
        raise PresetError(f"{label} has unknown keys: {', '.join(unknown)}")


def _build(cls, block: dict, label: str):
    # A typo in a preset (e.g. "colour" instead of "fill") should read as a
    # preset problem, not a Python TypeError from the dataclass constructor.
    _check_keys(block, {f.name for f in fields(cls)}, label)
    return cls(**block)


def _caption(block: dict) -> CaptionSpec:
    if not block.get("text"):
        raise PresetError("caption.text is required and must not be empty")
    return _build(CaptionSpec, block, "caption")


def _rhythm(block: dict) -> RhythmSpec:
    try:
        segments = block["segments"]
        duration = block["seg_duration"]
        total = block["total"]
        _check_keys(segments, {"min", "max"}, "rhythm.segments")
        _check_keys(duration, {"min", "target", "max"}, "rhythm.seg_duration")
        _check_keys(total, {"min", "max"}, "rhythm.total")
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

    # A total is reachable only if some whole segment count between segments.min
    # and segments.max has a duration range ([n*min_seconds, n*max_seconds]) that
    # overlaps [total.min, total.max]. Checking just the global envelope (fewest
    # segments at their shortest .. most segments at their longest) is not enough:
    # it misses gaps between per-count intervals, e.g. segments 1..3 at 1.0-1.05s
    # each can only total 1.00-1.05, 2.00-2.10 or 3.00-3.15 -- nothing in between.
    intervals = [
        (n, n * rhythm.min_seconds, n * rhythm.max_seconds)
        for n in range(rhythm.min_segments, rhythm.max_segments + 1)
    ]
    reachable = any(
        lo <= rhythm.max_total and hi >= rhythm.min_total for _, lo, hi in intervals
    )
    if not reachable:
        achievable = ", ".join(f"{n}:[{lo:g}, {hi:g}]" for n, lo, hi in intervals)
        raise PresetError(
            f"rhythm.total range [{rhythm.min_total:g}, {rhythm.max_total:g}]s is "
            f"unreachable at any segment count in {rhythm.min_segments}..{rhythm.max_segments}; "
            f"achievable totals per segment count are {achievable}"
        )
