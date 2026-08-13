import json
import threading
import urllib.error
import urllib.request

import pytest

from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.review.server import build_server


@pytest.fixture
def workspace(tmp_path, fixture_video):
    import shutil

    clip_dir = tmp_path / "output" / "fixture" / "p" / "1"
    clip_dir.mkdir(parents=True)
    shutil.copy(fixture_video, clip_dir / "01.mp4")
    shutil.copy(fixture_video, clip_dir / "02.mp4")

    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_video(conn, video_hash="abc", display_name="fixture.mp4", duration_s=30.0)
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="sha", preset_json="{}",
        caption_text="TEST", seed=1, cutlist_version="0.1.0", video_hashes=["abc"],
    )
    for ordinal in (1, 2):
        store.record_clip(
            conn, run_id=run_id, ordinal=ordinal,
            path=f"output/fixture/p/1/{ordinal:02d}.mp4", duration_s=4.0,
            segments=[store.SegmentRecord("abc", 2.0, 4.0, 0.0, 5.0, 0,
                                          thumbnail=b"\xff\xd8\xffA")],
        )
    # Delete the second clip's file: the exact state after a cleanup.
    (clip_dir / "02.mp4").unlink()
    return tmp_path


@pytest.fixture
def server(workspace):
    httpd = build_server(root=workspace, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def _post(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_clip_list_reports_availability(server):
    clips = _get(f"{server}/api/clips")
    by_ordinal = {clip["ordinal"]: clip for clip in clips}
    assert by_ordinal[1]["available"] is True
    assert by_ordinal[2]["available"] is False


def test_clip_detail_reports_availability(server):
    clips = _get(f"{server}/api/clips")
    missing = next(clip for clip in clips if clip["ordinal"] == 2)
    assert _get(f"{server}/api/clip/{missing['id']}")["available"] is False


def test_a_verdict_on_a_missing_clip_is_refused(server):
    clips = _get(f"{server}/api/clips")
    missing = next(clip for clip in clips if clip["ordinal"] == 2)
    status, body = _post(
        f"{server}/api/ratings", {"clip_id": missing["id"], "verdict": "fire"}
    )
    assert status == 400
    assert "missing" in body["error"]


def test_segment_marks_on_a_missing_clip_are_still_accepted(server):
    """A mark describes footage in the source, not the assembly that is gone."""
    clips = _get(f"{server}/api/clips")
    missing = next(clip for clip in clips if clip["ordinal"] == 2)
    segment_id = _get(f"{server}/api/clip/{missing['id']}")["segments"][0]["id"]
    status, body = _post(
        f"{server}/api/ratings",
        {"clip_id": missing["id"], "marks": [{"segment_id": segment_id, "mark": "good"}]},
    )
    assert (status, body["ok"]) == (200, True)


def test_a_verdict_on_a_present_clip_still_works(server):
    clips = _get(f"{server}/api/clips")
    present = next(clip for clip in clips if clip["ordinal"] == 1)
    status, body = _post(
        f"{server}/api/ratings", {"clip_id": present["id"], "verdict": "fire"}
    )
    assert (status, body["ok"]) == (200, True)


def test_rate_refuses_a_clip_whose_file_is_gone(workspace):
    from typer.testing import CliRunner

    from cutlist.cli import app

    result = CliRunner().invoke(
        app,
        ["rate", "output/fixture/p/1/02.mp4", "fire", "--root", str(workspace)],
    )
    assert result.exit_code == 1
    assert "missing" in result.output


def test_rate_applies_nothing_when_the_verdict_is_refused(workspace):
    """`rate` takes a verdict as a required argument, so a refused verdict
    fails the whole command. The marks that accompanied it must not land on
    their own -- a half-applied rating is worse than none.

    The web UI is where marks on a missing clip are still possible, because
    there the verdict is optional. That path is covered above.
    """
    from typer.testing import CliRunner

    from cutlist.cli import app

    result = CliRunner().invoke(
        app,
        ["rate", "output/fixture/p/1/02.mp4", "fire",
         "--segments", "1:good", "--root", str(workspace)],
    )
    assert result.exit_code == 1
    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM shot_rating").fetchone()[0] == 0
