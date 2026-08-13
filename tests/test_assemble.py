import pytest
from typer.testing import CliRunner

from cutlist.assemble import AssembleError, parse_ids
from cutlist.cli import app
from cutlist.db.schema import connect
from cutlist.media.probe import probe

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
    """The clips were chosen deliberately. A total that excludes them must not
    override the choice -- sample_preset caps total at 15s, and six shots of
    5s each far exceed it."""
    root, conn, ids = extracted
    chosen = ",".join(str(i) for i in ids)

    result = runner.invoke(
        app, ["assemble", chosen, "--preset", PRESET, "--root", str(root)]
    )

    assert result.exit_code == 0, result.output
    count = conn.execute(
        "SELECT COUNT(*) FROM segment JOIN clip ON clip.id = segment.clip_id "
        "JOIN run ON run.id = clip.run_id WHERE run.kind = 'assemble'"
    ).fetchone()[0]
    assert count == len(ids)


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
