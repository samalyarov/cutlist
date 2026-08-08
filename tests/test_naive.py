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
