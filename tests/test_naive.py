import random

import pytest

from cutlist.media.shots import Shot
from cutlist.presets import RhythmSpec
from cutlist.select.naive import NotEnoughFootage, draft_segments

RHYTHM = RhythmSpec(
    min_segments=4, max_segments=10,
    min_seconds=1.2, target_seconds=2.0, max_seconds=2.8,
    min_total=9.0, max_total=15.0,
)


def make_shots(count, length=6.0):
    return [Shot(i, i * length, (i + 1) * length) for i in range(count)]


@pytest.mark.parametrize("seed", range(25))
def test_respects_every_duration_rule(seed):
    segments = draft_segments(make_shots(40), RHYTHM, random.Random(seed))

    assert RHYTHM.min_segments <= len(segments) <= RHYTHM.max_segments
    for segment in segments:
        assert RHYTHM.min_seconds - 1e-6 <= segment.duration <= RHYTHM.max_seconds + 1e-6
    total = sum(s.duration for s in segments)
    assert RHYTHM.min_total - 1e-6 <= total <= RHYTHM.max_total + 1e-6


@pytest.mark.parametrize("seed", range(25))
def test_every_segment_sits_inside_a_real_shot(seed):
    shots = make_shots(40)
    segments = draft_segments(shots, RHYTHM, random.Random(seed))

    for segment in segments:
        assert any(
            shot.start <= segment.start and segment.end <= shot.end for shot in shots
        )


@pytest.mark.parametrize("seed", range(10))
def test_segments_are_ordered_by_timecode(seed):
    segments = draft_segments(make_shots(40), RHYTHM, random.Random(seed))
    assert [s.start for s in segments] == sorted(s.start for s in segments)


def test_never_reuses_a_shot():
    shots = make_shots(40)
    segments = draft_segments(shots, RHYTHM, random.Random(0))
    owners = [
        next(s.index for s in shots if s.start <= seg.start and seg.end <= s.end)
        for seg in segments
    ]
    assert len(set(owners)) == len(owners)


def test_different_seeds_give_different_drafts():
    shots = make_shots(40)
    first = draft_segments(shots, RHYTHM, random.Random(1))
    second = draft_segments(shots, RHYTHM, random.Random(2))
    assert [s.start for s in first] != [s.start for s in second]


def test_ignores_shots_shorter_than_the_minimum():
    shots = [Shot(0, 0.0, 0.3), Shot(1, 0.3, 0.6)] + [
        Shot(i, i * 6.0, (i + 1) * 6.0) for i in range(2, 30)
    ]
    segments = draft_segments(shots, RHYTHM, random.Random(0))
    for segment in segments:
        assert segment.start >= 12.0


def test_raises_when_there_is_not_enough_footage():
    with pytest.raises(NotEnoughFootage):
        draft_segments(make_shots(2), RHYTHM, random.Random(0))


# min_seconds == target_seconds == max_seconds pins every usable shot's
# duration at exactly the ceiling, so growth has zero slack to work with
# whenever a draw needs one. Only the segment count picked (and therefore how
# many shots' worth of footage land in the total) decides whether growth is
# even required.
ZERO_SLACK_RHYTHM = RhythmSpec(
    min_segments=2, max_segments=6,
    min_seconds=1.0, target_seconds=2.0, max_seconds=2.0,
    min_total=10.0, max_total=11.0,
)


@pytest.mark.parametrize("seed", range(200))
def test_zero_slack_shots_still_reach_the_total(seed):
    # Every shot is exactly 2.0s -- the reviewer's case that raised
    # NotEnoughFootage on 116/200 seeds before draft_segments retried a
    # fresh count instead of giving up on the first unlucky draw.
    shots = [Shot(i, i * 2.0, (i + 1) * 2.0) for i in range(20)]
    segments = draft_segments(shots, ZERO_SLACK_RHYTHM, random.Random(seed))

    total = sum(s.duration for s in segments)
    assert ZERO_SLACK_RHYTHM.min_total - 1e-6 <= total <= ZERO_SLACK_RHYTHM.max_total + 1e-6


def make_uneven_shots():
    """A realistic mix: most shots clustered near the floor, a few long ones.

    The near-floor shots are already pinned at their own duration (no slack
    to grow into), so reaching min_total depends on some draws pulling in a
    long shot -- exercising the retry path rather than the uniform 6.0s
    shots the other tests use, which always have slack in both directions.
    """
    shots = []
    t = 0.0
    for i in range(30):
        shots.append(Shot(i, t, t + 1.3))
        t += 1.3
    for i in range(30, 35):
        shots.append(Shot(i, t, t + 20.0))
        t += 20.0
    return shots


@pytest.mark.parametrize("seed", range(30))
def test_respects_bounds_with_an_uneven_shot_distribution(seed):
    shots = make_uneven_shots()
    segments = draft_segments(shots, RHYTHM, random.Random(seed))

    assert RHYTHM.min_segments <= len(segments) <= RHYTHM.max_segments
    for segment in segments:
        assert RHYTHM.min_seconds - 1e-6 <= segment.duration <= RHYTHM.max_seconds + 1e-6
    total = sum(s.duration for s in segments)
    assert RHYTHM.min_total - 1e-6 <= total <= RHYTHM.max_total + 1e-6
    for segment in segments:
        assert any(
            shot.start <= segment.start and segment.end <= shot.end for shot in shots
        )


def test_raises_when_no_draw_can_reach_the_total():
    # Only 4 shots clear the floor, so count is forced to exactly 4 every
    # attempt -- there is no different draw the retry loop could find. With
    # min_seconds == target_seconds == max_seconds, those 4 shots have no
    # slack to grow into, and 4 * 2.0s can never reach a 9.0s floor. This
    # must stay a real, un-retryable failure, not something the retry masks.
    #
    # Durations here are whole seconds rather than i * 1.2: that drifts to
    # 1.1999999999999997 for one shot, which lands under the 1.2s floor and
    # trips the len(usable) < min_segments guard before the retry loop is
    # even reached -- so the old version of this test never touched the
    # raise it was meant to exercise.
    shots = [Shot(i, i * 2.0, (i + 1) * 2.0) for i in range(4)]
    tight_rhythm = RhythmSpec(
        min_segments=4, max_segments=10,
        min_seconds=2.0, target_seconds=2.0, max_seconds=2.0,
        min_total=9.0, max_total=15.0,
    )
    with pytest.raises(NotEnoughFootage):
        draft_segments(shots, tight_rhythm, random.Random(0))
