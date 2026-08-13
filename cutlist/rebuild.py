import json
import tempfile
from pathlib import Path

from cutlist.db import store
from cutlist.media.caption import render_caption
from cutlist.media.render import Segment, render_clip
from cutlist.media.sources import find_source
from cutlist.paths import resolve_within
from cutlist.presets import preset_from_dict


class RebuildError(RuntimeError):
    """A clip cannot be rebuilt from its record."""


def rebuild_clip(conn, *, root: Path, clip_id: int) -> Path:
    """Re-cut a clip from what the database recorded about it.

    Written back to the path `clip.path` already claims, so the ratings
    attached to that clip still describe what is now on disk. That only holds
    if the footage is the same footage, so the source is required to match by
    content hash: a file carrying the recorded name but different bytes is
    refused rather than rendered over a clip that already has a verdict.

    Perceptually identical to the original, not byte-identical: different
    ffmpeg and x264 builds produce different bytes from the same input.
    """
    detail = store.clip_detail(conn, clip_id)
    if detail is None:
        raise RebuildError(f"no recorded clip with id {clip_id}")

    segments = detail["segments"]
    hashes = {segment["video_hash"] for segment in segments}
    if len(hashes) > 1:
        raise RebuildError(
            "this clip draws on more than one source video, which rerender "
            "cannot rebuild -- render_clip takes a single source"
        )

    video_hash = hashes.pop()
    display_name = segments[0]["display_name"]
    match = find_source(root, video_hash, display_name)
    if match is None:
        raise RebuildError(
            f"source video not found for {display_name!r} "
            f"({video_hash[:12]}); put it back under input/ and try again"
        )
    if not match.by_hash:
        # A display-name match is enough to show a thumbnail and nowhere near
        # enough to rebuild. Rendering it over dest would put footage nobody
        # has watched under a verdict somebody already gave.
        raise RebuildError(
            f"the file at {match.path} has the right name for {display_name!r} "
            f"but not the right content ({video_hash[:12]}); rebuilding from it "
            f"would not reproduce what was rated, so nothing was written"
        )

    dest = resolve_within(root, detail["path"])
    if dest is None:
        raise RebuildError(f"recorded path escapes the workspace: {detail['path']}")

    spec = preset_from_dict(json.loads(detail["preset_json"]))
    spec = spec.with_caption(detail["caption_text"])

    with tempfile.TemporaryDirectory() as scratch:
        scratch_root = Path(scratch)
        caption_png = render_caption(
            spec.caption, spec.output, scratch_root / "caption.png"
        )
        render_clip(
            match.path,
            [
                Segment(
                    start=segment["seg_start_s"],
                    duration=segment["seg_end_s"] - segment["seg_start_s"],
                )
                for segment in segments
            ],
            caption_png,
            spec.output,
            dest,
            scratch_root / "parts",
        )
    return dest
