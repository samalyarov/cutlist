import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20


def film_id(path: Path) -> str:
    """Identify a film by size plus its first and last megabyte.

    Hashing the whole file would mean reading gigabytes just to look something
    up in the cache. Size plus both ends is enough to tell films apart while
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
    def work(self) -> Path:
        return self.root / "work"

    @property
    def output(self) -> Path:
        return self.root / "output"

    def cache_for(self, film: Path) -> Path:
        path = self.cache / f"{film.stem}__{film_id(film)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def output_for(self, film: Path, preset_name: str, run_id: int) -> Path:
        """Where one run's clips are written.

        Scoped by run_id because clips are named by ordinal (`01.mp4`, ...):
        without it, re-drafting the same film and preset would overwrite an
        earlier run's files while its `clip` rows still claimed those paths,
        attaching ratings to footage no longer in the file. One directory per
        run makes a clip path name exactly one assembly.
        """
        path = self.output / film.stem / preset_name / str(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def database(self) -> Path:
        """The ratings store.

        Deliberately at the workspace root rather than under cache/: taste
        generalises across films, and the cache is regenerable. Deleting it
        must not destroy a month of judgements.
        """
        return self.root / "cutlist.sqlite"
