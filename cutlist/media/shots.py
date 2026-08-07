from dataclasses import dataclass
from pathlib import Path

from scenedetect import ContentDetector, detect


@dataclass(frozen=True)
class Shot:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_shots(
    path: Path,
    *,
    threshold: float = 27.0,
    min_shot_seconds: float = 0.4,
) -> list[Shot]:
    """Split a film into uninterrupted takes.

    min_shot_seconds suppresses the sub-frame detections that camera flashes
    and fast motion produce — measuring the reference clips turned up several
    "cuts" only 0.06s apart that were strobing, not editing.
    """
    scenes = detect(
        str(path),
        ContentDetector(threshold=threshold, min_scene_len=1),
        show_progress=False,
    )

    shots = [
        Shot(index=0, start=start.get_seconds(), end=end.get_seconds())
        for start, end in scenes
    ]
    return _renumber(_merge_short(shots, min_shot_seconds))


def _merge_short(shots: list[Shot], minimum: float) -> list[Shot]:
    merged: list[Shot] = []
    for shot in shots:
        if merged and shot.duration < minimum:
            previous = merged[-1]
            merged[-1] = Shot(previous.index, previous.start, shot.end)
        else:
            merged.append(shot)
    return merged


def _renumber(shots: list[Shot]) -> list[Shot]:
    return [Shot(i, shot.start, shot.end) for i, shot in enumerate(shots)]
