import shutil
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from cutlist.assemble import MAX_RANGE, AssembleError, parse_ids
from cutlist.cli import app
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.media.probe import probe
from cutlist.shell import run
from tests.conftest import FIXTURE_HEX_COLORS, FIXTURE_SHOT_SECONDS

runner = CliRunner()

PRESET = "presets/sample_preset.yaml"


def test_parse_ids_keeps_order_and_repeats():
    """The order is the edit: 3,1,3 plays clip 3, then 1, then 3 again."""
    assert parse_ids("3,1,3") == [3, 1, 3]


def test_parse_ids_expands_inclusive_ranges():
    assert parse_ids("2-5,9") == [2, 3, 4, 5, 9]


def test_parse_ids_tolerates_spacing():
    assert parse_ids(" 2 , 4 ") == [2, 4]


def test_parse_ids_rejects_a_backwards_range():
    with pytest.raises(AssembleError, match="backwards"):
        parse_ids("5-2")


def test_parse_ids_rejects_junk():
    with pytest.raises(AssembleError, match="not a clip id"):
        parse_ids("2,banana")


def test_parse_ids_rejects_an_empty_list():
    with pytest.raises(AssembleError, match="no clip ids"):
        parse_ids("")


@pytest.mark.parametrize("text", ["²", "3,²", "1-²", "²-3"])
def test_parse_ids_rejects_digits_int_would_not_accept(text):
    """str.isdigit() is True for the superscript two, and int() then raises
    ValueError -- which is not a handled error, so it reached the user as a
    traceback. isdecimal admits exactly what int accepts."""
    assert "²".isdigit(), "the premise of this test"
    with pytest.raises(AssembleError, match="not a (range of )?clip id"):
        parse_ids(text)


def test_parse_ids_refuses_a_range_too_wide_to_be_meant():
    """Validated before the range is materialised. "0-100000000" is one
    keystroke from "0-10" and used to allocate a hundred million ints before
    anything checked them -- a typo turning into a hang."""
    with pytest.raises(AssembleError, match=f"limited to {MAX_RANGE}"):
        parse_ids("0-100000000")


def test_parse_ids_allows_a_range_at_the_limit():
    assert len(parse_ids(f"1-{MAX_RANGE}")) == MAX_RANGE


@pytest.fixture
def extracted(tmp_path, fixture_video):
    """A workspace with fixture_video's six shots in the library."""
    result = runner.invoke(
        app, ["extract", str(fixture_video), "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    conn = connect(tmp_path / "cutlist.sqlite")
    ids = [row[0] for row in conn.execute("SELECT id FROM library_clip ORDER BY id")]
    assert len(ids) >= 3, "fixture should yield several shots"
    return tmp_path, conn, ids


def test_assemble_produces_a_playable_clip(extracted):
    root, _, ids = extracted
    chosen = ",".join(str(i) for i in ids[:3])

    result = runner.invoke(
        app, ["assemble", chosen, "--preset", PRESET, "--root", str(root)]
    )

    assert result.exit_code == 0, result.output
    written = sorted((root / "output" / "assembled").rglob("*.mp4"))
    assert len(written) == 1
    assert probe(written[0]).duration > 0


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    n = int(value, 16)
    return (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF


def _sample_centre(clip: Path, at: float, into: Path) -> tuple[int, int, int]:
    """One frame at `at` seconds, read at the middle of the picture.

    The caption sits in a band at the top and the 320x240 fixture is
    pillarboxed into the 854x480 output, so the picture's centre is the one
    place guaranteed to hold source content rather than caption or padding.
    """
    run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{at:.3f}", "-i", str(clip),
        "-frames:v", "1", str(into),
    ])
    img = Image.open(into).convert("RGB")
    return img.getpixel((img.width // 2, img.height // 2))


def test_the_assembled_file_holds_the_chosen_footage_in_the_order_named(extracted):
    """Only the file can say this. Every other test here reads `segment` rows
    built straight from the resolved id list, so they cannot disagree with it
    whatever the encoder did -- truncating every part to a fixed length,
    encoding only the first chosen clip, and reversing the part order all
    leave the database saying exactly what it says now. "The order is the
    edit" is this module's headline claim; this is what checks it.
    """
    root, conn, ids = extracted
    assert len(ids) >= 5, "fixture should yield enough shots to order-check"
    # Non-adjacent and out of id order, so a reversal changes which colour
    # lands in which part rather than merely relabelling them.
    chosen = [ids[0], ids[-1], ids[len(ids) // 2]]

    result = runner.invoke(
        app,
        ["assemble", ",".join(str(i) for i in chosen), "--preset", PRESET,
         "--root", str(root)],
    )
    assert result.exit_code == 0, result.output

    written = sorted((root / "output" / "assembled").rglob("*.mp4"))
    assert len(written) == 1
    produced = written[0]

    rows = [store.library_clip(conn, clip_id) for clip_id in chosen]
    recorded = conn.execute(
        "SELECT duration_s FROM clip ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert recorded == pytest.approx(sum(row["duration_s"] for row in rows), abs=0.01)

    # The video is as long as its own record claims it is.
    assert probe(produced).duration == pytest.approx(recorded, abs=0.5)

    frames = root / "frames"
    frames.mkdir()
    elapsed = 0.0
    for position, row in enumerate(rows):
        midpoint = elapsed + row["duration_s"] / 2
        elapsed += row["duration_s"]
        # Which flat colour the fixture was showing when this shot was cut.
        expected_hex = FIXTURE_HEX_COLORS[
            int((row["start_s"] + row["duration_s"] / 2) // FIXTURE_SHOT_SECONDS)
        ]
        expected = _hex_to_rgb(expected_hex)

        pixel = _sample_centre(produced, midpoint, frames / f"{position}.png")

        # Lossy H.264, so near rather than equal; the six fixture colours are
        # spread far enough apart that 40 cannot confuse two of them.
        assert all(abs(a - b) <= 40 for a, b in zip(pixel, expected)), (
            f"part {position} should be library clip {chosen[position]} "
            f"(source {row['start_s']:.2f}s): sampled {pixel} at output "
            f"{midpoint:.2f}s, expected ~{expected} ({expected_hex})"
        )


def test_assemble_records_a_segment_per_chosen_clip_in_order(extracted):
    root, conn, ids = extracted
    chosen = [ids[2], ids[0], ids[2]]

    result = runner.invoke(
        app,
        ["assemble", ",".join(str(i) for i in chosen), "--preset", PRESET,
         "--root", str(root)],
    )
    assert result.exit_code == 0, result.output

    rows = conn.execute(
        "SELECT position, seg_start_s FROM segment "
        "JOIN clip ON clip.id = segment.clip_id "
        "JOIN run ON run.id = clip.run_id WHERE run.kind = 'assemble' "
        "ORDER BY position"
    ).fetchall()
    starts = {
        i: conn.execute(
            "SELECT start_s FROM library_clip WHERE id = ?", (i,)
        ).fetchone()[0]
        for i in set(chosen)
    }
    assert [row["seg_start_s"] for row in rows] == [starts[i] for i in chosen]


def test_assemble_records_a_thumbnail_for_every_segment(extracted):
    root, conn, ids = extracted
    chosen = [ids[0], ids[1]]

    result = runner.invoke(
        app,
        [
            "assemble", ",".join(str(i) for i in chosen), "--preset", PRESET,
            "--root", str(root),
        ],
    )
    assert result.exit_code == 0, result.output

    clip_id = conn.execute("SELECT id FROM clip ORDER BY id DESC LIMIT 1").fetchone()[0]
    segments = conn.execute(
        "SELECT id FROM segment WHERE clip_id = ? ORDER BY position", (clip_id,)
    ).fetchall()
    assert len(segments) == len(chosen)
    for segment in segments:
        image = store.segment_thumbnail(conn, segment["id"])
        assert image, f"segment {segment['id']} has no thumbnail"
        assert image[:2] == b"\xff\xd8", "a thumbnail should be JPEG bytes"


def test_assembled_thumbnails_survive_deleting_the_source(tmp_path, fixture_video):
    """A mark stays legible after its source is gone -- and "masters for
    reuse" is an invitation to delete the source. Captured from the library
    master, which is the same footage and is still there; a capture from the
    source could not have happened at all here.
    """
    source = tmp_path / "input" / "fixture.mp4"
    source.parent.mkdir(parents=True)
    shutil.copy(fixture_video, source)
    assert runner.invoke(
        app, ["extract", str(source), "--root", str(tmp_path)]
    ).exit_code == 0

    conn = connect(tmp_path / "cutlist.sqlite")
    ids = [row[0] for row in conn.execute("SELECT id FROM library_clip ORDER BY id")]

    # Gone before a single frame is assembled.
    source.unlink()

    result = runner.invoke(
        app,
        ["assemble", f"{ids[0]},{ids[2]}", "--preset", PRESET, "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    clip_id = conn.execute("SELECT id FROM clip ORDER BY id DESC LIMIT 1").fetchone()[0]
    segments = conn.execute(
        "SELECT id FROM segment WHERE clip_id = ? ORDER BY position", (clip_id,)
    ).fetchall()
    assert len(segments) == 2
    for segment in segments:
        assert store.segment_thumbnail(conn, segment["id"])


def test_assemble_records_the_original_source_not_the_library_file(extracted):
    """Provenance names the video the footage came from, so an assembled clip
    decomposes exactly like a drafted one and a rating means the same thing."""
    root, conn, ids = extracted

    result = runner.invoke(
        app, ["assemble", str(ids[0]), "--preset", PRESET, "--root", str(root)]
    )
    assert result.exit_code == 0, result.output

    segment_hash = conn.execute(
        "SELECT video_hash FROM segment ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    library_hash = conn.execute(
        "SELECT video_hash FROM library_clip WHERE id = ?", (ids[0],)
    ).fetchone()[0]
    assert segment_hash == library_hash
    assert conn.execute(
        "SELECT COUNT(*) FROM video WHERE video_hash = ?", (segment_hash,)
    ).fetchone()[0] == 1


def test_assemble_records_a_run_of_kind_assemble(extracted):
    """A draft is reproducible from its seed; an assembly is a list of choices.
    kind is what tells them apart, since seed cannot."""
    root, conn, ids = extracted

    result = runner.invoke(
        app, ["assemble", str(ids[0]), "--preset", PRESET, "--root", str(root)]
    )
    assert result.exit_code == 0, result.output

    assert conn.execute(
        "SELECT kind FROM run ORDER BY id DESC LIMIT 1"
    ).fetchone()[0] == "assemble"


def test_assemble_ignores_the_preset_rhythm(extracted):
    """The clips were chosen deliberately. A rule that excluded some of them
    must not override the choice -- sample_preset caps total at 15s and
    segments at 10, and the list below breaks both: twelve parts of 5s each.
    Listing each id twice is what puts the count over the segment cap, which
    six shots on their own cannot do."""
    root, conn, ids = extracted
    doubled = list(ids) + list(ids)
    assert len(doubled) > 10, "the list must exceed sample_preset's max segments"
    chosen = ",".join(str(i) for i in doubled)

    result = runner.invoke(
        app, ["assemble", chosen, "--preset", PRESET, "--root", str(root)]
    )

    assert result.exit_code == 0, result.output
    count = conn.execute(
        "SELECT COUNT(*) FROM segment JOIN clip ON clip.id = segment.clip_id "
        "JOIN run ON run.id = clip.run_id WHERE run.kind = 'assemble'"
    ).fetchone()[0]
    assert count == len(doubled)


def test_assemble_reports_an_unknown_id(extracted):
    root, _, _ = extracted

    result = runner.invoke(
        app, ["assemble", "9999", "--preset", PRESET, "--root", str(root)]
    )

    assert result.exit_code == 1
    assert "no library clip with id 9999" in result.output
    assert "Traceback" not in result.output


def test_assemble_reports_a_missing_library_file(extracted):
    root, conn, ids = extracted
    path = conn.execute(
        "SELECT path FROM library_clip WHERE id = ?", (ids[0],)
    ).fetchone()[0]
    (root / path).unlink()

    result = runner.invoke(
        app, ["assemble", str(ids[0]), "--preset", PRESET, "--root", str(root)]
    )

    assert result.exit_code == 1
    assert "file is missing" in result.output
    assert "Traceback" not in result.output


def test_assemble_writes_nothing_when_an_id_is_unknown(extracted):
    """Everything is checked before anything is encoded, so a typo in the last
    id does not surface after minutes of rendering -- or leave a partial run."""
    root, conn, ids = extracted
    before = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]

    result = runner.invoke(
        app,
        ["assemble", f"{ids[0]},9999", "--preset", PRESET, "--root", str(root)],
    )

    assert result.exit_code == 1
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == before
    assert not list((root / "output").rglob("*.mp4"))
