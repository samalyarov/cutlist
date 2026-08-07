import pytest

from cutlist.shell import run

# Explicit hex values, not colour names: named colours like X11 "green"
# (0,128,0) can land within a few luma units of another named colour
# (red is ~76, this green is ~75 under BT.601), which makes the cut
# invisible to luma-dominant scene-detection heuristics. These six were
# picked for BT.601 luma spread instead (76, 255, 15, 226, 64, 179), so
# every adjacent pair moves by at least 115 and every cut is detectable.
FIXTURE_HEX_COLORS = ["0xFF0000", "0xFFFFFF", "0x000080", "0xFFFF00", "0x404040", "0x00FFFF"]
FIXTURE_SHOT_SECONDS = 5.0
FIXTURE_DURATION = FIXTURE_SHOT_SECONDS * len(FIXTURE_HEX_COLORS)
FIXTURE_CUTS = [FIXTURE_SHOT_SECONDS * i for i in range(1, len(FIXTURE_HEX_COLORS))]


@pytest.fixture(scope="session")
def fixture_film(tmp_path_factory):
    """A 30s video of six flat colours, cutting every 5s.

    Session-scoped because encoding it takes a second or two and nothing
    mutates it.
    """
    out = tmp_path_factory.mktemp("media") / "fixture.mp4"

    inputs = []
    for colour in FIXTURE_HEX_COLORS:
        inputs += [
            "-f", "lavfi",
            "-i", f"color=c={colour}:s=320x240:d={FIXTURE_SHOT_SECONDS}:r=25",
        ]
    labels = "".join(f"[{i}:v]" for i in range(len(FIXTURE_HEX_COLORS)))

    run([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", f"{labels}concat=n={len(FIXTURE_HEX_COLORS)}:v=1:a=0[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out),
    ])
    return out
