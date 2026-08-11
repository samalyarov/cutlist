import random

import pytest

from cutlist.media.shots import Shot
from cutlist.presets import RhythmSpec
from cutlist.select.naive import NotEnoughFootage, Pick, draft_picks, _grow_toward, _shrink_toward


def test_shrink_toward_moves_each_duration_down_in_proportion_to_its_slack():
    # Two segments, 4.0s of slack each above a 1.0 floor. Asking for a total of
    # 6.0 from 10.0 means giving up 4.0 of the 8.0 available -- half each slack.
    result = _shrink_toward([5.0, 5.0], target=6.0, floor=1.0)
    assert result == pytest.approx([3.0, 3.0])


def test_shrink_toward_never_pushes_a_duration_below_the_floor():
    # 1.5 has only 0.5 of slack above the floor; 6.0 has 5.0. Demanding more
    # than the total slack clamps every element at its floor rather than
    # producing a sub-floor duration.
    result = _shrink_toward([1.5, 6.0], target=0.0, floor=1.0)
    assert result == pytest.approx([1.0, 1.0])


def test_shrink_toward_returns_input_untouched_when_there_is_no_slack():
    assert _shrink_toward([1.0, 1.0], target=0.5, floor=1.0) == [1.0, 1.0]


def test_grow_toward_respects_a_per_element_ceiling():
    # The second element can only reach 2.0, so it contributes 1.0 of the
    # 5.0 total slack while the first contributes 4.0.
    result = _grow_toward([1.0, 1.0], target=4.0, ceilings=[5.0, 2.0])
    assert result == pytest.approx([2.6, 1.4])


def test_grow_toward_stops_at_the_ceilings_when_the_target_is_out_of_reach():
    result = _grow_toward([1.0, 1.0], target=99.0, ceilings=[2.0, 3.0])
    assert result == pytest.approx([2.0, 3.0])


def test_grow_toward_returns_input_untouched_when_every_element_is_capped():
    assert _grow_toward([2.0, 3.0], target=99.0, ceilings=[2.0, 3.0]) == [2.0, 3.0]


RHYTHM = RhythmSpec(
    min_segments=4, max_segments=10,
    min_seconds=1.2, target_seconds=2.0, max_seconds=2.8,
    min_total=9.0, max_total=15.0,
)


def make_shots(count, length=6.0):
    return [Shot(i, i * length, (i + 1) * length) for i in range(count)]


@pytest.mark.parametrize("seed", range(25))
def test_respects_every_duration_rule(seed):
    picks = draft_picks(make_shots(40), RHYTHM, random.Random(seed))
    segments = [p.segment for p in picks]

    assert RHYTHM.min_segments <= len(segments) <= RHYTHM.max_segments
    for segment in segments:
        assert RHYTHM.min_seconds - 1e-6 <= segment.duration <= RHYTHM.max_seconds + 1e-6
    total = sum(s.duration for s in segments)
    assert RHYTHM.min_total - 1e-6 <= total <= RHYTHM.max_total + 1e-6


@pytest.mark.parametrize("seed", range(25))
def test_every_segment_sits_inside_a_real_shot(seed):
    shots = make_shots(40)
    picks = draft_picks(shots, RHYTHM, random.Random(seed))
    segments = [p.segment for p in picks]

    for segment in segments:
        assert any(
            shot.start <= segment.start and segment.end <= shot.end for shot in shots
        )


@pytest.mark.parametrize("seed", range(10))
def test_segments_are_ordered_by_timecode(seed):
    picks = draft_picks(make_shots(40), RHYTHM, random.Random(seed))
    segments = [p.segment for p in picks]
    assert [s.start for s in segments] == sorted(s.start for s in segments)


def test_never_reuses_a_shot():
    shots = make_shots(40)
    picks = draft_picks(shots, RHYTHM, random.Random(0))
    segments = [p.segment for p in picks]
    owners = [
        next(s.index for s in shots if s.start <= seg.start and seg.end <= s.end)
        for seg in segments
    ]
    assert len(set(owners)) == len(owners)


def test_different_seeds_give_different_drafts():
    shots = make_shots(40)
    first = [p.segment for p in draft_picks(shots, RHYTHM, random.Random(1))]
    second = [p.segment for p in draft_picks(shots, RHYTHM, random.Random(2))]
    assert [s.start for s in first] != [s.start for s in second]


def test_ignores_shots_shorter_than_the_minimum():
    shots = [Shot(0, 0.0, 0.3), Shot(1, 0.3, 0.6)] + [
        Shot(i, i * 6.0, (i + 1) * 6.0) for i in range(2, 30)
    ]
    picks = draft_picks(shots, RHYTHM, random.Random(0))
    segments = [p.segment for p in picks]
    for segment in segments:
        assert segment.start >= 12.0


def test_raises_when_there_is_not_enough_footage():
    with pytest.raises(NotEnoughFootage):
        draft_picks(make_shots(2), RHYTHM, random.Random(0))


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
    # NotEnoughFootage on 116/200 seeds before draft_picks retried a
    # fresh count instead of giving up on the first unlucky draw.
    shots = [Shot(i, i * 2.0, (i + 1) * 2.0) for i in range(20)]
    picks = draft_picks(shots, ZERO_SLACK_RHYTHM, random.Random(seed))
    segments = [p.segment for p in picks]

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
    picks = draft_picks(shots, RHYTHM, random.Random(seed))
    segments = [p.segment for p in picks]

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
        draft_picks(shots, tight_rhythm, random.Random(0))


def test_draft_picks_pairs_every_segment_with_its_shot():
    shots = [Shot(index=i, start=i * 5.0, end=i * 5.0 + 5.0) for i in range(8)]
    picks = draft_picks(shots, RHYTHM, random.Random(1))

    assert picks
    assert all(isinstance(p, Pick) for p in picks)
    for pick in picks:
        assert pick.shot.start <= pick.segment.start
        assert pick.segment.end <= pick.shot.end


def test_draft_picks_keeps_shots_in_timecode_order():
    shots = [Shot(index=i, start=i * 5.0, end=i * 5.0 + 5.0) for i in range(8)]
    picks = draft_picks(shots, RHYTHM, random.Random(2))
    starts = [p.shot.start for p in picks]
    assert starts == sorted(starts)


def test_draft_picks_is_reproducible_for_a_seed():
    shots = [Shot(index=i, start=i * 5.0, end=i * 5.0 + 5.0) for i in range(8)]
    first = draft_picks(shots, RHYTHM, random.Random(42))
    second = draft_picks(shots, RHYTHM, random.Random(42))
    assert [(p.shot.index, p.segment.start) for p in first] == \
           [(p.shot.index, p.segment.start) for p in second]
