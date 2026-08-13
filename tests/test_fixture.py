from cutlist.shell import run
from tests.conftest import FIXTURE_DURATION


def test_fixture_video_has_expected_shape(fixture_video):
    assert fixture_video.exists()
    out = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=width,height",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(fixture_video),
    ])
    values = out.split()
    assert values[0] == "320"
    assert values[1] == "240"
    assert abs(float(values[2]) - FIXTURE_DURATION) < 0.5
