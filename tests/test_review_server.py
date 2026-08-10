import json
import threading
import urllib.error
import urllib.request

import pytest

from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.review.server import build_server


@pytest.fixture
def workspace(tmp_path, fixture_film):
    import shutil

    clip_dir = tmp_path / "output" / "fixture" / "p"
    clip_dir.mkdir(parents=True)
    shutil.copy(fixture_film, clip_dir / "01.mp4")

    conn = connect(tmp_path / "cutlist.sqlite")
    store.record_film(conn, film_hash="abc", display_name="fixture.mp4", duration_s=30.0)
    run_id = store.start_run(
        conn, preset_name="p", preset_sha256="sha", preset_json="{}",
        caption_text="TEST", seed=1, cutlist_version="0.1.0", film_hashes=["abc"],
    )
    store.record_clip(
        conn, run_id=run_id, ordinal=1,
        path="output/fixture/p/01.mp4", duration_s=4.0,
        segments=[
            store.SegmentRecord("abc", 2.0, 4.0, 0.0, 5.0, 0),
            store.SegmentRecord("abc", 7.0, 9.0, 5.0, 10.0, 1),
        ],
    )
    # The source has to be findable for thumbnails; review resolves it from
    # the film's display_name under the workspace input directory.
    (tmp_path / "input").mkdir()
    shutil.copy(fixture_film, tmp_path / "input" / "fixture.mp4")
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
        return response.status, response.read(), dict(response.headers)


def _post(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


def test_root_serves_the_page(server):
    status, body, headers = _get(f"{server}/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<title>" in body


def test_page_makes_no_external_requests(server):
    _, body, _ = _get(f"{server}/")
    text = body.decode()
    assert "http://" not in text.replace("http://127.0.0.1", "")
    assert "https://" not in text
    assert "cdn" not in text.lower()


def test_clips_endpoint_lists_unrated_clips(server):
    status, body, _ = _get(f"{server}/api/clips")
    assert status == 200
    clips = json.loads(body)
    assert len(clips) == 1
    assert clips[0]["segment_count"] == 2


def test_clip_endpoint_returns_segments(server):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    status, body, _ = _get(f"{server}/api/clip/{clips[0]['id']}")
    assert status == 200
    detail = json.loads(body)
    assert [s["position"] for s in detail["segments"]] == [0, 1]


def test_unknown_clip_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{server}/api/clip/9999")
    assert exc.value.code == 404


def test_posting_a_verdict_persists_it(server, workspace):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    status, body = _post(f"{server}/api/ratings", {
        "clip_id": clips[0]["id"], "verdict": "fire",
    })
    assert status == 200 and body["ok"] is True

    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT verdict FROM clip_rating").fetchone()[0] == "fire"


def test_posting_segment_marks_persists_them(server, workspace):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    detail = json.loads(_get(f"{server}/api/clip/{clips[0]['id']}")[1])
    _post(f"{server}/api/ratings", {
        "clip_id": clips[0]["id"],
        "verdict": "ok",
        "marks": [{"segment_id": detail["segments"][0]["id"], "mark": "veto"}],
    })
    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT mark FROM shot_rating").fetchone()[0] == "veto"


def test_a_malformed_payload_is_rejected_and_changes_nothing(server, workspace):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{server}/api/ratings", {"clip_id": 1, "verdict": "sideways"})
    assert exc.value.code == 400

    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM clip_rating").fetchone()[0] == 0


def test_a_partially_invalid_mark_list_writes_nothing(server, workspace):
    """One valid mark plus one referencing an unknown segment must both fail.

    Marks are written one at a time inside store.mark_shot; if validation
    only checked shapes and not existence, the valid mark could commit
    before the invalid one raised, leaving a stray row behind a 400.
    """
    clips = json.loads(_get(f"{server}/api/clips")[1])
    detail = json.loads(_get(f"{server}/api/clip/{clips[0]['id']}")[1])
    valid_segment_id = detail["segments"][0]["id"]

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{server}/api/ratings", {
            "clip_id": clips[0]["id"],
            "verdict": "ok",
            "marks": [
                {"segment_id": valid_segment_id, "mark": "good"},
                {"segment_id": 9999999, "mark": "good"},
            ],
        })
    assert exc.value.code == 400

    conn = connect(workspace / "cutlist.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM shot_rating").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clip_rating").fetchone()[0] == 0


def test_video_is_served(server):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    status, body, headers = _get(f"{server}/media/clip/{clips[0]['id']}")
    assert status == 200
    assert headers["Content-Type"] == "video/mp4"
    assert headers["Accept-Ranges"] == "bytes"
    assert len(body) > 0


def test_video_honours_a_range_request(server, workspace):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    request = urllib.request.Request(f"{server}/media/clip/{clips[0]['id']}")
    request.add_header("Range", "bytes=0-99")
    with urllib.request.urlopen(request) as response:
        assert response.status == 206
        assert response.headers["Content-Range"].startswith("bytes 0-99/")
        body = response.read()

    on_disk = (workspace / "output" / "fixture" / "p" / "01.mp4").read_bytes()
    assert body == on_disk[:100]


def test_video_honours_a_suffix_range_request(server, workspace):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    request = urllib.request.Request(f"{server}/media/clip/{clips[0]['id']}")
    request.add_header("Range", "bytes=-100")
    with urllib.request.urlopen(request) as response:
        assert response.status == 206
        body = response.read()

    on_disk = (workspace / "output" / "fixture" / "p" / "01.mp4").read_bytes()
    assert len(body) == 100
    assert body == on_disk[-100:]


def test_thumbnail_is_served(server):
    clips = json.loads(_get(f"{server}/api/clips")[1])
    detail = json.loads(_get(f"{server}/api/clip/{clips[0]['id']}")[1])
    status, body, headers = _get(f"{server}/media/thumb/{detail['segments'][0]['id']}")
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body[:2] == b"\xff\xd8"


def test_the_server_writes_no_sql_of_its_own():
    """store.py is the only module that talks to the database.

    Two rating paths (web and CLI) only stay consistent if neither grows its
    own queries, so this is asserted rather than left to review.
    """
    from pathlib import Path

    source = Path("cutlist/review/server.py").read_text(encoding="utf-8")
    for keyword in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert keyword not in source, f"{keyword.strip()} found in server.py"
