import shutil
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from cutlist.assemble import (
    MAX_RANGE,
    TRIM_SLACK,
    AssembleError,
    Pick,
    _resolve,
    parse_ids,
)
from cutlist.cli import app
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.media.probe import probe
from cutlist.paths import video_id
from cutlist.shell import run
from tests.conftest import FIXTURE_HEX_COLORS, FIXTURE_SHOT_SECONDS

runner = CliRunner()

PRESET = "presets/sample_preset.yaml"


def test_parse_ids_keeps_order_and_repeats():
    """The order is the edit: 3,1,3 plays clip 3, then 1, then 3 again."""
    assert parse_ids("3,1,3") == [Pick(3), Pick(1), Pick(3)]


def test_parse_ids_expands_inclusive_ranges():
    assert parse_ids("2-5,9") == [Pick(2), Pick(3), Pick(4), Pick(5), Pick(9)]


def test_parse_ids_tolerates_spacing():
    assert parse_ids(" 2 , 4 ") == [Pick(2), Pick(4)]


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


# --- trims: id:start-end -----------------------------------------------------


def test_parse_ids_reads_a_trim():
    assert parse_ids("3:1.5-4") == [Pick(3, (1.5, 4.0))]


def test_parse_ids_mixes_trims_with_ids_and_ranges():
    """A trim's own "-" must not be read as a range of ids, and a trimmed id
    and the same id untrimmed are two different picks, both kept in order."""
    assert parse_ids("2-3, 7 : 0 - 2 ,7") == [
        Pick(2), Pick(3), Pick(7, (0.0, 2.0)), Pick(7),
    ]


def test_parse_ids_refuses_a_trim_on_a_range():
    """Shots differ in length, so one trim over a range means something
    different for each; it is refused rather than guessed at."""
    with pytest.raises(AssembleError, match="one clip id"):
        parse_ids("2-5:0-1")


@pytest.mark.parametrize("text", ["3:4-4", "3:5-2"])
def test_parse_ids_refuses_an_empty_or_backwards_trim(text):
    with pytest.raises(AssembleError, match="empty or runs backwards"):
        parse_ids(text)


@pytest.mark.parametrize(
    "text",
    # Each is something float() would accept or a shape a typo produces.
    # "nan" is the dangerous one: it compares false against every bound, so
    # it would pass the backwards check and the past-the-end check alike.
    ["3:", "3:1", "3:-2", "3:1-", "3:a-2", "3:-1-2", "3:1-inf", "3:nan-2",
     "3:1e3-2000", "3:１-2", "3:1-2-3", "3:1,5-2"],
)
def test_parse_ids_refuses_a_malformed_trim(text):
    with pytest.raises(AssembleError, match="not a trim|not a clip id|empty entry"):
        parse_ids(text)


@pytest.mark.parametrize("text", [":1-2", "x:1-2", "²:1-2"])
def test_parse_ids_refuses_a_trim_without_a_clip_id(text):
    with pytest.raises(AssembleError, match="one clip id"):
        parse_ids(text)


def _assemble(root, clips: str):
    return runner.invoke(
        app, ["assemble", clips, "--preset", PRESET, "--root", str(root)]
    )


def _latest_segments(conn):
    return conn.execute(
        "SELECT * FROM segment WHERE clip_id = "
        "(SELECT id FROM clip ORDER BY id DESC LIMIT 1) ORDER BY position"
    ).fetchall()


def test_a_trimmed_pick_records_its_stretch_and_its_whole_shot(extracted):
    """The segment is the stretch played, in source timecodes; the parent is
    the whole shot. Recording the offset alone (forgetting the shot's start)
    or the shot bounds as the segment would both put a rating on footage the
    clip does not contain -- and send `rerender` to cut the wrong stretch."""
    root, conn, ids = extracted
    shot = store.library_clip(conn, ids[1])

    result = _assemble(root, f"{ids[1]}:1-3.5")
    assert result.exit_code == 0, result.output

    (segment,) = _latest_segments(conn)
    assert segment["seg_start_s"] == pytest.approx(shot["start_s"] + 1, abs=1e-6)
    assert segment["seg_end_s"] == pytest.approx(shot["start_s"] + 3.5, abs=1e-6)
    assert segment["shot_start_s"] == shot["start_s"]
    assert segment["shot_end_s"] == shot["end_s"]
    assert segment["shot_index"] == shot["shot_index"]

    recorded = conn.execute(
        "SELECT duration_s, path FROM clip ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert recorded["duration_s"] == pytest.approx(2.5)
    # The file, not just the row: a trim the encoder ignored would leave
    # the database saying 2.5s over a 5s video.
    assert probe(root / recorded["path"]).duration == pytest.approx(2.5, abs=0.05)


def test_an_untrimmed_pick_records_exactly_what_it_always_did(extracted):
    """Mixing trims in must not perturb the whole-shot picks beside them."""
    root, conn, ids = extracted
    whole = store.library_clip(conn, ids[0])

    result = _assemble(root, f"{ids[0]},{ids[1]}:0-1")
    assert result.exit_code == 0, result.output

    first = _latest_segments(conn)[0]
    assert (first["seg_start_s"], first["seg_end_s"]) == (whole["start_s"], whole["end_s"])


@pytest.fixture
def two_colour_master(tmp_path, fixture_video):
    """A library clip spanning the fixture's 5s-15s: white for 5s, then navy.

    Every real shot in the fixture is one flat colour, so no frame taken from
    one can tell which stretch of it was played. This master can: an offset
    that was ignored, or applied to the wrong file, shows up as the wrong
    colour. It starts at 5s rather than 0 so that a trim recorded without
    the shot's own start added would point `rerender` at different colours.
    """
    source = tmp_path / "input" / "fixture.mp4"
    source.parent.mkdir(parents=True)
    shutil.copy(fixture_video, source)
    assert runner.invoke(app, ["extract", str(source), "--root", str(tmp_path)]).exit_code == 0

    master = tmp_path / "library" / "two_colour.mp4"
    run([
        "ffmpeg", "-y", "-v", "error", "-ss", "5", "-i", str(source), "-t", "10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(master),
    ])
    conn = connect(tmp_path / "cutlist.sqlite")
    clip_id = store.record_library_clip(
        conn, video_hash=video_id(source), start_s=5.0, end_s=15.0, shot_index=None,
        path=master.relative_to(tmp_path).as_posix(), duration_s=10.0,
    )
    return tmp_path, conn, clip_id


def _assert_colours(clip: Path, frames: Path, expected: list[tuple[float, str]]):
    frames.mkdir(exist_ok=True)
    for n, (at, hex_colour) in enumerate(expected):
        pixel = _sample_centre(clip, at, frames / f"{n}.png")
        want = _hex_to_rgb(hex_colour)
        assert all(abs(a - b) <= 40 for a, b in zip(pixel, want)), (
            f"at {at:.2f}s expected ~{want} ({hex_colour}), sampled {pixel}"
        )


def test_the_assembled_file_plays_the_trimmed_stretch(two_colour_master):
    """Only the frames can say which stretch was encoded. Navy first, then
    white, from one master: a dropped offset would encode white twice."""
    root, conn, clip_id = two_colour_master
    white, navy = FIXTURE_HEX_COLORS[1], FIXTURE_HEX_COLORS[2]

    result = _assemble(root, f"{clip_id}:6-9,{clip_id}:1-4")
    assert result.exit_code == 0, result.output

    path = root / conn.execute("SELECT path FROM clip ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert probe(path).duration == pytest.approx(6.0, abs=0.05)
    _assert_colours(path, root / "frames", [(1.5, navy), (4.5, white)])

    starts = [(s["seg_start_s"], s["seg_end_s"]) for s in _latest_segments(conn)]
    assert starts == [(11.0, 14.0), (6.0, 9.0)]


def test_rerender_rebuilds_a_trimmed_clip_from_the_source(two_colour_master):
    """`rerender` knows nothing about trims; it cuts the recorded source
    timecodes. This is what proves the record is right: the rebuilt file,
    cut from the original video rather than the library master, must hold
    the same stretches in the same order."""
    root, conn, clip_id = two_colour_master
    white, navy = FIXTURE_HEX_COLORS[1], FIXTURE_HEX_COLORS[2]

    assert _assemble(root, f"{clip_id}:6-9,{clip_id}:1-4").exit_code == 0
    relative = conn.execute("SELECT path FROM clip ORDER BY id DESC LIMIT 1").fetchone()[0]
    (root / relative).unlink()

    rebuilt = runner.invoke(app, ["rerender", relative, "--root", str(root)])
    assert rebuilt.exit_code == 0, rebuilt.output

    assert probe(root / relative).duration == pytest.approx(6.0, abs=0.05)
    _assert_colours(root / relative, root / "frames", [(1.5, navy), (4.5, white)])


def test_a_trimmed_thumbnail_shows_the_stretch_played(two_colour_master):
    """A mark's picture must be of the footage judged, not of the middle of
    the whole shot. The trim is white; the master's own midpoint is already
    navy, so a thumbnail taken there would show a colour never played."""
    root, conn, clip_id = two_colour_master

    assert _assemble(root, f"{clip_id}:1-4").exit_code == 0
    (segment,) = _latest_segments(conn)

    thumb = root / "thumb.jpg"
    thumb.write_bytes(store.segment_thumbnail(conn, segment["id"]))
    img = Image.open(thumb).convert("RGB")
    pixel = img.getpixel((img.width // 2, img.height // 2))
    white = _hex_to_rgb(FIXTURE_HEX_COLORS[1])
    assert all(abs(a - b) <= 40 for a, b in zip(pixel, white)), pixel


@pytest.mark.parametrize("trim", ["0-99", "6-7", "5.5-6"])
def test_a_trim_past_the_end_is_refused_before_anything_is_written(extracted, trim):
    """Checked with the other ids, before encoding, so a bad trim on the last
    pick neither costs minutes of rendering nor leaves a partial run."""
    root, conn, ids = extracted
    before = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]

    result = _assemble(root, f"{ids[0]},{ids[1]}:{trim}")

    assert result.exit_code == 1
    assert "runs past the end" in result.output
    assert "Traceback" not in result.output
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == before
    assert not list((root / "output").rglob("*.mp4"))


def test_a_trim_ending_at_the_rounded_duration_means_the_end(extracted):
    """Filenames print durations to two decimals, so the end a person types
    can overshoot the real one by under half a hundredth. That is "to the
    end": accepted, and recorded as the shot's own end rather than a
    timecode past it."""
    root, conn, ids = extracted
    shot = store.library_clip(conn, ids[1])
    end = shot["duration_s"] + TRIM_SLACK * 0.8

    result = _assemble(root, f"{ids[1]}:1-{end:.4f}")
    assert result.exit_code == 0, result.output
    assert _latest_segments(conn)[0]["seg_end_s"] == shot["end_s"]

    beyond = shot["duration_s"] + TRIM_SLACK * 1.2
    assert "runs past the end" in _assemble(root, f"{ids[1]}:1-{beyond:.4f}").output

    # Inside the slack but starting after the footage does: clamping the end
    # would leave a stretch that ends before it begins.
    late = f"{shot['duration_s'] + 0.001:.4f}-{shot['duration_s'] + 0.004:.4f}"
    assert "shorter than one frame" in _assemble(root, f"{ids[1]}:{late}").output


@pytest.mark.parametrize(
    "trim",
    # Sub-millisecond: rounds to a zero-length segment. Under a millisecond
    # past a whole second: used to reach ffmpeg as "-t 0.000" and fail after
    # the run row was written. Just under one frame at sample_preset's 25 fps.
    ["0.0001-0.0002", "1-1.0004", "0-0.039"],
)
def test_a_trim_shorter_than_a_frame_is_refused_before_anything_is_written(extracted, trim):
    root, conn, ids = extracted
    before = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]

    result = _assemble(root, f"{ids[0]},{ids[1]}:{trim}")

    assert result.exit_code == 1
    assert "shorter than one frame at 25 fps" in result.output
    assert "Traceback" not in result.output
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == before
    assert not list((root / "output").rglob("*.mp4"))


def test_a_one_frame_trim_is_allowed(extracted):
    root, conn, ids = extracted
    result = _assemble(root, f"{ids[1]}:0-0.04")
    assert result.exit_code == 0, result.output


def test_the_slack_holds_at_its_own_boundary_at_ntsc_rates(extracted):
    """A 55-frame shot at 29.97 fps is 1.835s: the filename says "1.84s" and
    `cutlist library` may say "1.83s". Both must mean "to the end". In floats
    1.835 + 0.005 is 1.8399999999999999, so a bound compared unrounded at half
    a hundredth refused the very number on the file."""
    root, conn, ids = extracted
    master = store.library_clip(conn, ids[0])
    ntsc = store.record_library_clip(
        conn, video_hash=master["video_hash"], start_s=100.0, end_s=101.835,
        shot_index=None, path=master["path"], duration_s=1.835,
    )

    for typed in ("1.84", "1.83", "1.845"):
        (part,) = _resolve(conn, root, [Pick(ntsc, (0.0, float(typed)))], fps=29.97)
        assert part.source_end <= 101.835, typed
    assert part.source_end == 101.835

    with pytest.raises(AssembleError, match="runs past the end"):
        _resolve(conn, root, [Pick(ntsc, (0.0, 1.85))], fps=29.97)


@pytest.mark.parametrize(
    "text", ["99999999999999999999", "1-99999999999999999999", "9" * 5000 + ":0-1"],
)
def test_parse_ids_refuses_an_id_too_long_to_store(text):
    """SQLite stores a signed 64-bit INTEGER. Longer digit strings passed
    `isdecimal` and then raised OverflowError at the query -- or ValueError in
    `int` itself past Python's digit limit -- as a traceback either way."""
    with pytest.raises(AssembleError, match="not a (range of )?clip id|one clip id"):
        parse_ids(text)


def test_an_id_at_the_digit_limit_is_reported_as_unknown_not_a_crash(extracted):
    root, _, _ = extracted
    result = _assemble(root, "9" * 18)
    assert result.exit_code == 1
    assert "no library clip with id" in result.output
    assert "Traceback" not in result.output


def test_a_mixed_clip_is_exactly_as_long_as_its_segments(extracted):
    """The clip row and its segment rows are both a record of length; a
    consumer summing the segments must get the clip's own figure."""
    root, conn, ids = extracted

    result = _assemble(root, f"{ids[0]},{ids[1]}:0.0004-1.9996,{ids[2]}:1.1-3.3")
    assert result.exit_code == 0, result.output

    spans = sum(s["seg_end_s"] - s["seg_start_s"] for s in _latest_segments(conn))
    recorded = conn.execute(
        "SELECT duration_s FROM clip ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert recorded == store.ms(spans)
