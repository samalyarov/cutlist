from cutlist.media.shots import Shot, _merge_short, _renumber, detect_shots
from tests.conftest import CUTFREE_DURATION, FIXTURE_CUTS, FIXTURE_DURATION


def test_cutfree_video_yields_one_shot_spanning_the_whole_film(cutfree_film):
    # detect() defaults to assuming the video opens mid-scene, which for a
    # video with zero cuts means zero scenes rather than one covering
    # everything. Without start_in_scene=True this comes back [], and the
    # `cutlist shots` command then crashes computing a median of nothing.
    shots = detect_shots(cutfree_film)
    assert len(shots) == 1
    assert shots[0].start == 0.0
    assert abs(shots[0].end - CUTFREE_DURATION) < 0.5


def test_detects_every_cut_in_the_fixture(fixture_film):
    shots = detect_shots(fixture_film)
    assert len(shots) == len(FIXTURE_CUTS) + 1


def test_shot_boundaries_land_on_the_real_cuts(fixture_film):
    shots = detect_shots(fixture_film)
    detected = [shot.start for shot in shots[1:]]
    for expected, actual in zip(FIXTURE_CUTS, detected):
        assert abs(actual - expected) < 0.25


def test_shots_tile_the_film_without_gaps(fixture_film):
    shots = detect_shots(fixture_film)
    assert shots[0].start == 0.0
    assert abs(shots[-1].end - FIXTURE_DURATION) < 0.5
    for earlier, later in zip(shots, shots[1:]):
        assert earlier.end == later.start


def test_indices_are_sequential(fixture_film):
    shots = detect_shots(fixture_film)
    assert [shot.index for shot in shots] == list(range(len(shots)))


def test_duration_property(fixture_film):
    for shot in detect_shots(fixture_film):
        assert shot.duration == shot.end - shot.start
        assert shot.duration > 0


def test_leading_short_shot_merges_into_the_next_one():
    # A 0.1s flash frame at the start has no predecessor to absorb into.
    shots = [
        Shot(0, 0.0, 0.1),
        Shot(1, 0.1, 5.0),
        Shot(2, 5.0, 10.0),
    ]
    merged = _merge_short(shots, minimum=0.4)
    assert [(s.start, s.end) for s in merged] == [(0.0, 5.0), (5.0, 10.0)]
    for earlier, later in zip(merged, merged[1:]):
        assert earlier.end == later.start


def test_several_leading_short_shots_collapse_into_one():
    shots = [
        Shot(0, 0.0, 0.1),
        Shot(1, 0.1, 0.2),
        Shot(2, 0.2, 0.3),
        Shot(3, 0.3, 5.0),
    ]
    merged = _merge_short(shots, minimum=0.4)
    assert [(s.start, s.end) for s in merged] == [(0.0, 5.0)]


def test_single_short_shot_is_preserved_not_dropped():
    # Nothing to fold it into, and dropping the only shot would be worse
    # than keeping one that's under the minimum.
    shots = [Shot(0, 0.0, 0.1)]
    merged = _merge_short(shots, minimum=0.4)
    assert [(s.start, s.end) for s in merged] == [(0.0, 0.1)]


def test_indices_are_sequential_after_merging_leading_short_shots():
    shots = [
        Shot(0, 0.0, 0.1),
        Shot(1, 0.1, 0.2),
        Shot(2, 0.2, 5.0),
        Shot(3, 5.0, 10.0),
    ]
    merged = _renumber(_merge_short(shots, minimum=0.4))
    assert [s.index for s in merged] == list(range(len(merged)))
