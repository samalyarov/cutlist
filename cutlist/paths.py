import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20


def video_id(path: Path) -> str:
    """Identify a video by size plus its first and last megabyte.

    Hashing the whole file would mean reading gigabytes just to look something
    up in the cache. Size plus both ends is enough to tell videos apart while
    staying stable when the file is renamed or moved.
    """
    size = path.stat().st_size
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(size).encode())

    with path.open("rb") as handle:
        digest.update(handle.read(CHUNK))
        if size > 2 * CHUNK:
            # large file: middle is untouched, skip straight to the tail
            handle.seek(-CHUNK, os.SEEK_END)
            digest.update(handle.read(CHUNK))
        elif size > CHUNK:
            # first chunk overlaps the tail here, so just take what's left
            digest.update(handle.read())

    return digest.hexdigest()


def resolve_within(root: Path, relative: str) -> Path | None:
    """Join a workspace-relative path, refusing to leave the workspace.

    `relative` comes from `clip.path` in the database. Nothing enforces that a
    writer put a relative path there, so an absolute path or a `..` segment
    must not gain filesystem access outside root.
    """
    resolved_root = Path(root).resolve()
    candidate = (Path(root) / relative).resolve()
    return candidate if candidate.is_relative_to(resolved_root) else None


@dataclass(frozen=True)
class Workspace:
    """Where cutlist keeps things, split by what invalidates each directory."""

    root: Path

    @property
    def input(self) -> Path:
        return self.root / "input"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def library(self) -> Path:
        return self.root / "library"

    def cache_for(self, video: Path) -> Path:
        path = self.cache / f"{video.stem}__{video_id(video)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def output_for(self, video: Path, preset_name: str, run_id: int) -> Path:
        """Where one run's clips are written.

        Scoped by run_id because clips are named by ordinal (`01.mp4`, ...):
        without it, re-drafting the same video and preset would overwrite an
        earlier run's files while its `clip` rows still claimed those paths,
        attaching ratings to footage no longer in the file. One directory per
        run makes a clip path name exactly one assembly.
        """
        path = self.output / video.stem / preset_name / str(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def database(self) -> Path:
        """The ratings store.

        Deliberately at the workspace root rather than under cache/: taste
        generalises across videos, and the cache is regenerable. Deleting it
        must not destroy a month of judgements.
        """
        return self.root / "cutlist.sqlite"
