from dataclasses import dataclass
from pathlib import Path

from cutlist.paths import video_id

# What `draft` can be pointed at. Matched case-insensitively.
VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"})

# A cache hit saves the directory walk, not the content check: every hit is
# revalidated against `video_id` before it is trusted, since a file can be
# overwritten in place (a corrected re-encode saved over the same path) and
# stop matching the hash it was cached under without ever moving. Misses are
# not cached: a source that was absent a moment ago may have just been copied
# in.
_resolved: dict[tuple[str, str], Path] = {}


@dataclass(frozen=True)
class SourceMatch:
    """A located source video, and how strongly it was matched.

    `by_hash` is False when only the recorded display name matched: a file
    that shares the name but not the bytes of the video a clip was cut from.
    Reported rather than hidden because the two are not interchangeable, and
    which one a caller can accept depends entirely on what it does next --
    showing a frame survives a near-enough source; reproducing a rated clip,
    or writing to the database, does not.
    """

    path: Path
    by_hash: bool


def find_source(
    root: Path, video_hash: str, display_name: str | None = None
) -> SourceMatch | None:
    """Locate the source video with this content hash under `root/input`.

    Matched on content rather than path, so a source that has been renamed or
    moved within the input directory still resolves -- the database records
    what a video *is*, not where it was that day.

    Falls back to a display-name match when no hash matches, which covers a
    source that was re-encoded: same name, different bytes. That fallback is
    returned with `by_hash` False rather than as an equal answer, so no
    caller can inherit it by accident.
    """
    key = (str(root), video_hash)
    cached = _resolved.get(key)
    if cached is not None and cached.exists() and video_id(cached) == video_hash:
        return SourceMatch(cached, by_hash=True)

    directory = Path(root) / "input"
    if not directory.is_dir():
        return None

    named: Path | None = None
    for candidate in sorted(directory.rglob("*")):
        if not candidate.is_file() or candidate.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        if display_name is not None and named is None and candidate.name == display_name:
            named = candidate
        if video_id(candidate) == video_hash:
            _resolved[key] = candidate
            return SourceMatch(candidate, by_hash=True)

    return None if named is None else SourceMatch(named, by_hash=False)
