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


def find_source(
    root: Path, video_hash: str, display_name: str | None = None
) -> Path | None:
    """Locate the source video with this content hash under `root/input`.

    Matched on content rather than path, so a source that has been renamed or
    moved within the input directory still resolves -- the database records
    what a video *is*, not where it was that day.

    Falls back to a display-name match when no hash matches, which covers a
    source that was re-encoded: same name, different bytes. That is a weaker
    claim than a hash match and is only made when the strong one fails.
    """
    key = (str(root), video_hash)
    cached = _resolved.get(key)
    if cached is not None and cached.exists() and video_id(cached) == video_hash:
        return cached

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
            return candidate

    return named
