import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.media.thumbs import thumbnail

PAGE = Path(__file__).with_name("page.html")

_CLIP = re.compile(r"^/api/clip/(\d+)$")
_MEDIA_CLIP = re.compile(r"^/media/clip/(\d+)$")
_MEDIA_THUMB = re.compile(r"^/media/thumb/(\d+)$")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")

# Enough of a clip to fill a buffer without materialising a whole file in RAM.
_CHUNK = 64 * 1024


def _is_id(value) -> bool:
    """Is this a usable row id from a JSON body?

    `isinstance(True, int)` is True in Python, so a bare int check would accept
    {"clip_id": true} and rate clip 1 -- a row the caller never named.
    """
    return isinstance(value, int) and not isinstance(value, bool)


class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the review page and the JSON it talks to.

    One connection per request and a fresh sqlite connection per request:
    sqlite3 objects are not shareable across threads, and ThreadingHTTPServer
    hands each request to its own thread.
    """

    server_version = "cutlist-review"

    # Silence the default stderr access log; a local review tool logging every
    # range request drowns anything worth reading.
    def log_message(self, format, *args):  # noqa: A002 - signature is fixed
        pass

    # -- helpers ---------------------------------------------------------

    @property
    def config(self) -> dict:
        return self.server.cutlist  # type: ignore[attr-defined]

    def _db(self):
        return connect(self.config["root"] / "cutlist.sqlite")

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _source_for(self, video_hash: str, conn) -> Path | None:
        """Locate the original video a segment was cut from.

        The database records a display name, not a path, because the file can
        move. Look for it where the workspace keeps sources.
        """
        name = store.video_display_name(conn, video_hash)
        if name is None:
            return None
        candidate = self.config["root"] / "input" / name
        return candidate if candidate.exists() else None

    def _resolve_within_root(self, relative: str) -> Path | None:
        """Join a workspace-relative path, refusing to leave the workspace.

        `relative` comes from `clip.path` in the database. `draft` only ever
        writes a path already relative to root, but nothing here enforces
        that, so a future writer landing an absolute path or a `..` segment
        must not gain filesystem access outside root.
        """
        root = self.config["root"].resolve()
        candidate = (self.config["root"] / relative).resolve()
        if not candidate.is_relative_to(root):
            return None
        return candidate

    def _send_file(self, path: Path, content_type: str) -> None:
        """Serve a file, honouring a single-range request.

        Video needs this: without 206 support the browser cannot seek, and
        `J`/`K`/`L` shuttling does nothing.
        """
        size = path.stat().st_size
        header = self.headers.get("Range", "")
        match = _RANGE.match(header) if header else None

        if match is None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with path.open("rb") as handle:
                self._pump(handle, size)
            return

        first, last = match.group(1), match.group(2)
        if first == "" and last != "":
            # Suffix form ("bytes=-500"): the last N bytes, not bytes 0..N.
            start = max(size - int(last), 0)
            end = size - 1
        else:
            start = int(first) if first else 0
            end = int(last) if last else size - 1
            end = min(end, size - 1)

        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            self._pump(handle, length)

    def _pump(self, handle, remaining: int) -> None:
        while remaining > 0:
            block = handle.read(min(_CHUNK, remaining))
            if not block:
                return
            self.wfile.write(block)
            remaining -= len(block)

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]

        if path == "/":
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/clips":
            conn = self._db()
            self._send_json(store.clips_for_review(
                conn,
                video=self.config["video"],
                preset=self.config["preset"],
                unrated_only=self.config["unrated_only"],
            ))
            return

        match = _CLIP.match(path)
        if match:
            detail = store.clip_detail(self._db(), int(match.group(1)))
            if detail is None:
                self._send_error(404, "no such clip")
                return
            self._send_json(detail)
            return

        match = _MEDIA_CLIP.match(path)
        if match:
            relative = store.clip_path(self._db(), int(match.group(1)))
            if relative is None:
                self._send_error(404, "no such clip")
                return
            clip_path = self._resolve_within_root(relative)
            if clip_path is None or not clip_path.exists():
                self._send_error(404, "clip file is missing")
                return
            self._send_file(clip_path, "video/mp4")
            return

        match = _MEDIA_THUMB.match(path)
        if match:
            self._serve_thumb(int(match.group(1)))
            return

        self._send_error(404, "not found")

    def _serve_thumb(self, segment_id: int) -> None:
        conn = self._db()
        segment = store.segment_by_id(conn, segment_id)
        if segment is None:
            self._send_error(404, "no such segment")
            return

        source = self._source_for(segment["video_hash"], conn)
        if source is None:
            self._send_error(404, "source video not found")
            return

        cache = self.config["root"] / "cache" / "thumbs"
        midpoint = (segment["seg_start_s"] + segment["seg_end_s"]) / 2
        dest = thumbnail(source, midpoint, cache / f"segment_{segment_id}.jpg")
        self._send_file(dest, "image/jpeg")

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path.split("?", 1)[0] != "/api/ratings":
            self._send_error(404, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_error(400, "Content-Length must be an integer")
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_error(400, "body is not valid JSON")
            return

        conn = self._db()
        clip_id = payload.get("clip_id")
        verdict = payload.get("verdict")
        marks = payload.get("marks") or []

        # Everything is validated before anything is written, so a bad mark
        # never leaves a verdict recorded without it, and no earlier mark in
        # the same list is left committed while a later one fails.
        if not _is_id(clip_id) or store.clip_detail(conn, clip_id) is None:
            self._send_error(400, "clip_id must name a recorded clip")
            return
        if verdict is not None and verdict not in store.VERDICTS:
            self._send_error(400, f"verdict must be one of {', '.join(store.VERDICTS)}")
            return
        for entry in marks:
            if not isinstance(entry, dict) or entry.get("mark") not in store.MARKS:
                self._send_error(400, f"mark must be one of {', '.join(store.MARKS)}")
                return
            segment_id = entry.get("segment_id")
            if not _is_id(segment_id):
                self._send_error(400, "each mark needs an integer segment_id")
                return
            if store.segment_by_id(conn, segment_id) is None:
                self._send_error(400, f"no such segment: {segment_id}")
                return

        try:
            for entry in marks:
                store.mark_shot(
                    conn, segment_id=entry["segment_id"], mark=entry["mark"]
                )
            if verdict is not None:
                store.rate_clip(conn, clip_id=clip_id, verdict=verdict)
        # The store's own rating errors only. Catching bare ValueError or
        # LookupError would dress a genuine bug inside store up as a 400.
        except (store.RatingError, store.RatingNotFound) as exc:
            self._send_error(400, str(exc))
            return

        self._send_json({"ok": True})


def build_server(
    *,
    root: Path,
    port: int,
    video: str | None = None,
    preset: str | None = None,
    unrated_only: bool = True,
) -> ThreadingHTTPServer:
    """Bind the review server without starting it.

    Returned unstarted so tests can run it on an ephemeral port in a thread
    and the CLI can print the URL before blocking on serve_forever().
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ReviewHandler)
    httpd.cutlist = {  # type: ignore[attr-defined]
        "root": Path(root),
        "video": video,
        "preset": preset,
        "unrated_only": unrated_only,
    }
    return httpd
