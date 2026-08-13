from pathlib import Path

from cutlist.shell import run

# Flat colours with hard cuts between them, alternating dark and light so every
# adjacent pair moves by at least 144 in BT.601 luma. Scene detection is
# luma-dominant: two colours that look different but land at similar brightness
# produce a cut the detector cannot see. Lengths vary so the rhythm rules have
# real choices to make rather than one forced answer.
DEMO_SHOTS = [
    ("0x101010", 4.0),
    ("0xF0F0F0", 2.5),
    ("0x203080", 5.0),
    ("0xF0E040", 3.0),
    ("0x404040", 4.5),
    ("0xD0D0D0", 2.0),
    ("0x801020", 3.5),
    ("0xC0E8F0", 5.5),
    ("0x2E4D32", 3.0),
    ("0xFFFFFF", 2.5),
    ("0x000080", 4.0),
    ("0xE0E060", 3.5),
]

DEMO_WIDTH = 640
DEMO_HEIGHT = 360
DEMO_FPS = 25


def build_demo_source(dest: Path) -> Path:
    """Synthesise a multi-shot video to draft from.

    Exists so the tool can be run by someone who has no source video. None
    ships with cutlist and none can: the project deliberately distributes no
    footage.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    inputs: list[str] = []
    for colour, seconds in DEMO_SHOTS:
        inputs += [
            "-f", "lavfi",
            "-i", f"color=c={colour}:s={DEMO_WIDTH}x{DEMO_HEIGHT}"
                  f":d={seconds}:r={DEMO_FPS}",
        ]
    labels = "".join(f"[{i}:v]" for i in range(len(DEMO_SHOTS)))

    run([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", f"{labels}concat=n={len(DEMO_SHOTS)}:v=1:a=0[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(dest),
    ])
    return dest
