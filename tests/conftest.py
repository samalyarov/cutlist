import pytest

from cutlist.shell import run

FIXTURE_COLORS = ["red", "green", "blue", "yellow", "magenta", "cyan"]
FIXTURE_SHOT_SECONDS = 5.0
FIXTURE_DURATION = FIXTURE_SHOT_SECONDS * len(FIXTURE_COLORS)
FIXTURE_CUTS = [FIXTURE_SHOT_SECONDS * i for i in range(1, len(FIXTURE_COLORS))]


@pytest.fixture(scope="session")
def fixture_film(tmp_path_factory):
    """A 30s video of six flat colours, cutting every 5s.

    Session-scoped because encoding it takes a second or two and nothing
    mutates it.
    """
    out = tmp_path_factory.mktemp("media") / "fixture.mp4"

    inputs = []
    for colour in FIXTURE_COLORS:
        inputs += [
            "-f", "lavfi",
            "-i", f"color=c={colour}:s=320x240:d={FIXTURE_SHOT_SECONDS}:r=25",
        ]
    labels = "".join(f"[{i}:v]" for i in range(len(FIXTURE_COLORS)))

    run([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", f"{labels}concat=n={len(FIXTURE_COLORS)}:v=1:a=0[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out),
    ])
    return out
