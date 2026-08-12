import random
from dataclasses import dataclass

from cutlist.media.render import Segment
from cutlist.media.shots import Shot
from cutlist.presets import RhythmSpec


class NotEnoughFootage(RuntimeError):
    """The video has too few usable shots to fill an assembly."""


# A single random count-and-sample can land on a pool that happens to have no
# slack in the direction it needs (e.g. every drawn shot already pinned at its
# own short duration). That's bad luck, not a real shortage of footage, so a
# handful of retries with fresh draws is tried before giving up.
_MAX_ATTEMPTS = 20


@dataclass(frozen=True)
class Pick:
    """A chosen segment together with the shot it was cut from.

    Rendering only needs the segment, but provenance needs the shot: a
    judgement about "this moment" and one about "this take" are different
    claims, and neither is recoverable from the other afterwards.
    """

    shot: Shot
    segment: Segment


def draft_picks(
    shots: list[Shot],
    rhythm: RhythmSpec,
    rng: random.Random,
) -> list[Pick]:
    """Pick segments at random, subject to the preset's duration rules.

    Selection here is deliberately blind — it only enforces the rhythm. Scoring
    replaces the sampling step later without changing the duration logic.
    """
    usable = [shot for shot in shots if shot.duration >= rhythm.min_seconds]
    if len(usable) < rhythm.min_segments:
        raise NotEnoughFootage(
            f"need at least {rhythm.min_segments} shots of "
            f"{rhythm.min_seconds}s or more, found {len(usable)}"
        )

    for _ in range(_MAX_ATTEMPTS):
        count = rng.randint(rhythm.min_segments, min(rhythm.max_segments, len(usable)))
        chosen = sorted(rng.sample(usable, count), key=lambda shot: shot.start)

        durations = _fit_total(
            [min(rhythm.target_seconds, shot.duration) for shot in chosen],
            [min(rhythm.max_seconds, shot.duration) for shot in chosen],
            rhythm,
        )
        if durations is not None:
            return [
                Pick(shot=shot, segment=_centred(shot, length))
                for shot, length in zip(chosen, durations)
            ]

    raise NotEnoughFootage(
        f"{len(usable)} usable shots never redistributed into a "
        f"{rhythm.min_total}-{rhythm.max_total}s total after {_MAX_ATTEMPTS} draws"
    )


def _centred(shot: Shot, length: float) -> Segment:
    """Take the middle of a shot, so the cut avoids the transition frames."""
    start = shot.start + (shot.duration - length) / 2
    return Segment(start=start, duration=length)


def _fit_total(
    durations: list[float],
    ceilings: list[float],
    rhythm: RhythmSpec,
) -> list[float] | None:
    """Stretch or squeeze segment lengths until the total lands in range.

    Each segment stays within its own floor and ceiling, so a short shot is
    never asked to give more than it has. Returns None when this particular
    draw of shots has no slack left in the direction it needs -- the caller
    tries a fresh draw rather than treating one unlucky sample as fatal.
    """
    total = sum(durations)

    if total > rhythm.max_total:
        durations = _shrink_toward(durations, rhythm.max_total, rhythm.min_seconds)
    elif total < rhythm.min_total:
        durations = _grow_toward(durations, rhythm.min_total, ceilings)

    total = sum(durations)
    if not rhythm.min_total - 1e-6 <= total <= rhythm.max_total + 1e-6:
        return None
    return durations


def _shrink_toward(durations: list[float], target: float, floor: float) -> list[float]:
    """Shorten every segment toward a shared floor, in proportion to its slack.

    Proportional rather than equal: taking the same amount from every segment
    would push the already-short ones under the floor first.
    """
    slack = [duration - floor for duration in durations]
    return _apply(durations, slack, sum(durations) - target, sign=-1)


def _grow_toward(
    durations: list[float], target: float, ceilings: list[float]
) -> list[float]:
    """Lengthen every segment toward its own ceiling, in proportion to its slack.

    Ceilings are per-segment because a segment can never outgrow the shot it
    was cut from, and shots differ in length.
    """
    slack = [ceiling - duration for duration, ceiling in zip(durations, ceilings)]
    return _apply(durations, slack, target - sum(durations), sign=1)


def _apply(
    durations: list[float], slack: list[float], needed: float, *, sign: int
) -> list[float]:
    """Spend `needed` seconds across the segments, capped by available slack.

    When the slack cannot cover what is needed, every segment lands exactly on
    its bound and the caller's range check rejects the draw.
    """
    available = sum(slack)
    if available <= 0:
        return durations
    share = min(1.0, needed / available)
    return [
        duration + sign * portion * share
        for duration, portion in zip(durations, slack)
    ]
