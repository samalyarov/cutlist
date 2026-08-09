import random
from dataclasses import dataclass

from cutlist.media.render import Segment
from cutlist.media.shots import Shot
from cutlist.presets import RhythmSpec


class NotEnoughFootage(RuntimeError):
    """The film has too few usable shots to fill an assembly."""


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
        durations = _redistribute(durations, rhythm.max_total, rhythm.min_seconds, shrink=True)
    elif total < rhythm.min_total:
        durations = _redistribute(durations, rhythm.min_total, ceilings, shrink=False)

    total = sum(durations)
    if not rhythm.min_total - 1e-6 <= total <= rhythm.max_total + 1e-6:
        return None
    return durations


def _redistribute(durations, target, bound, *, shrink):
    """Move every segment toward its bound in proportion to its slack."""
    bounds = bound if isinstance(bound, list) else [bound] * len(durations)
    slack = [
        (d - b) if shrink else (b - d)
        for d, b in zip(durations, bounds)
    ]
    available = sum(slack)
    needed = abs(sum(durations) - target)

    if available <= 0:
        return durations

    share = min(1.0, needed / available)
    return [
        d - slack[i] * share if shrink else d + slack[i] * share
        for i, d in enumerate(durations)
    ]
