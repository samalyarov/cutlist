from cutlist.media.shots import detect_shots
from tests.conftest import FIXTURE_CUTS, FIXTURE_DURATION


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
