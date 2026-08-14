import json
import tempfile
from pathlib import Path

from cutlist.db import store
from cutlist.media.caption import render_caption
from cutlist.media.render import Segment, concat, encode_segment
from cutlist.media.sources import find_source
from cutlist.paths import resolve_within
from cutlist.presets import preset_from_dict


class RebuildError(RuntimeError):
    """A clip cannot be rebuilt from its record."""


def _locate(root: Path, video_hash: str, display_name: str) -> Path:
    """Find the file a segment was cut from, or say which one is missing.

    Named in every message: a clip can draw on several sources, and "a source
    is missing" is not something anybody can act on when there are two.
    """
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
    return match.path


def rebuild_clip(conn, *, root: Path, clip_id: int) -> Path:
    """Re-cut a clip from what the database recorded about it.

    Written back to the path `clip.path` already claims, so the ratings
    attached to that clip still describe what is now on disk. That only holds
    if the footage is the same footage, so every source is required to match
    by content hash: a file carrying the recorded name but different bytes is
    refused rather than rendered over a clip that already has a verdict.

    Each segment is encoded from its own source, so a clip drawing on several
    videos rebuilds like any other -- which `assemble` makes in a single
    command, and refusing it deadlocked the pair (`rate` sends a missing clip
    to `rerender`, and `rerender` would have sent it straight back).

    Every source is resolved before anything is encoded, so a clip with one
    missing video fails without spending minutes on the parts it could have
    rendered, and without touching `dest`.

    Perceptually identical to the original, not byte-identical: different
    ffmpeg and x264 builds produce different bytes from the same input.
    """
    detail = store.clip_detail(conn, clip_id)
    if detail is None:
        raise RebuildError(f"no recorded clip with id {clip_id}")

    segments = detail["segments"]
    # Resolved once per distinct source rather than once per segment: a clip
    # can name the same video repeatedly, and find_source walks input/ and
    # hashes what it finds.
    sources: dict[str, Path] = {}
    for segment in segments:
        video_hash = segment["video_hash"]
        if video_hash not in sources:
            sources[video_hash] = _locate(root, video_hash, segment["display_name"])

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
        parts = [
            encode_segment(
                sources[segment["video_hash"]],
                Segment(
                    start=segment["seg_start_s"],
                    duration=segment["seg_end_s"] - segment["seg_start_s"],
                ),
                caption_png,
                spec.output,
                scratch_root / f"part_{position:03d}.mp4",
            )
            for position, segment in enumerate(segments)
        ]
        # Atomic: dest is by definition a clip that may already carry a
        # verdict, and a failed join must not take that footage with it.
        concat(parts, dest)
    return dest
