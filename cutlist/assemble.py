"""Building a video from library clips a person named, in the order they named.

The counterpart to `draft`: where drafting picks shots at random subject to a
preset's rhythm, assembling takes an explicit list and honours it. That is why
this module ignores `rhythm` entirely -- the clips were chosen deliberately, and
a duration rule that overrode the choice would be answering a question nobody
asked.
"""

import tempfile
from pathlib import Path

from cutlist.db import store
from cutlist.media.caption import render_caption
from cutlist.media.render import Segment, concat, encode_segment
from cutlist.media.thumbs import thumbnail_bytes
from cutlist.paths import resolve_within


class AssembleError(RuntimeError):
    """A video cannot be assembled from the clips it was given."""


# A range is a convenience for naming neighbouring shots, not a way to ask for
# a library nobody has. Checked before the range is materialised: "0-100000000"
# is one keystroke away from "0-10" and would otherwise allocate a hundred
# million ints -- about 800 MB and over a second -- before anything looked at
# them, turning a typo into a hang instead of a sentence.
MAX_RANGE = 10_000


def parse_ids(text: str) -> list[int]:
    """Parse `"2,3"` and `"2-5"` into library clip ids.

    Order is preserved and repeats are kept, because the order is the edit:
    `3,1,3` means play clip 3, then 1, then 3 again.

    Digits are tested with `isdecimal` rather than `isdigit`, which is the
    wider set: `"²".isdigit()` is True and `int("²")` raises ValueError, which
    is not a handled error and so reaches the user as a traceback. `isdecimal`
    admits exactly what `int` accepts.
    """
    # Checked before splitting: "" splits to [""], which would otherwise be
    # reported as an empty *entry* in a list -- true, but a confusing way to
    # say that no ids were given at all.
    if not text.strip():
        raise AssembleError("no clip ids given")

    ids: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise AssembleError(f"empty entry in the clip list: {text!r}")

        if "-" in chunk.lstrip("-"):
            low, _, high = chunk.partition("-")
            low, high = low.strip(), high.strip()
            if not (low.isdecimal() and high.isdecimal()):
                raise AssembleError(f"not a range of clip ids: {chunk!r}")
            if int(low) > int(high):
                raise AssembleError(
                    f"range runs backwards: {chunk!r}. Write it low-to-high, and "
                    f"list ids separately if you want them in that order."
                )
            width = int(high) - int(low) + 1
            if width > MAX_RANGE:
                raise AssembleError(
                    f"range {chunk!r} spans {width} clip ids; a single range is "
                    f"limited to {MAX_RANGE}. Check for a stray digit -- `cutlist "
                    f"library` lists the ids that exist."
                )
            ids.extend(range(int(low), int(high) + 1))
            continue

        if not chunk.isdecimal():
            raise AssembleError(f"not a clip id: {chunk!r}")
        ids.append(int(chunk))

    if not ids:
        raise AssembleError("no clip ids given")
    return ids


def _resolve(conn, root: Path, ids: list[int]) -> list:
    """Fetch each id's row and confirm its file is still on disk.

    Everything is checked before anything is encoded, so a typo in the last id
    does not surface after minutes of rendering.
    """
    rows = []
    for clip_id in ids:
        row = store.library_clip(conn, clip_id)
        if row is None:
            raise AssembleError(
                f"no library clip with id {clip_id}. Run `cutlist library` to "
                f"see what is stored."
            )
        path = resolve_within(root, row["path"])
        if path is None or not path.exists():
            raise AssembleError(
                f"library clip {clip_id} is recorded at {row['path']} but the "
                f"file is missing. Re-run `cutlist extract` on its source."
            )
        rows.append((row, path))
    return rows


def assemble_clips(
    conn,
    *,
    ids: list[int],
    spec,
    workspace,
    preset_sha256: str,
    preset_json: str,
    cutlist_version: str,
) -> Path:
    """Render the named library clips into one captioned video.

    The preset's `caption` and `output` blocks apply; its `rhythm` does not.

    Provenance records the *original* source and timecodes, not the library
    file, so an assembled clip decomposes exactly like a drafted one and a
    rating on it means the same thing. The library is a cache of footage, not
    a new kind of source.
    """
    resolved = _resolve(conn, workspace.root, ids)

    run_id = store.start_run(
        conn,
        preset_name=spec.name,
        preset_sha256=preset_sha256,
        preset_json=preset_json,
        caption_text=spec.caption.text,
        # An assembly is a list of choices a person made; there is no seed that
        # reproduces it. `kind` is what tells the two apart.
        seed=0,
        cutlist_version=cutlist_version,
        video_hashes=sorted({row["video_hash"] for row, _ in resolved}),
        kind="assemble",
    )

    destination = workspace.output / "assembled" / str(run_id)
    destination.mkdir(parents=True, exist_ok=True)
    dest = destination / "01.mp4"

    with tempfile.TemporaryDirectory() as scratch:
        scratch_root = Path(scratch)
        caption_png = render_caption(
            spec.caption, spec.output, scratch_root / "caption.png"
        )
        parts = [
            encode_segment(
                path,
                # The whole library clip: it is already the shot, so the trim
                # is the file's full span.
                Segment(start=0.0, duration=float(row["duration_s"])),
                caption_png,
                spec.output,
                scratch_root / f"part_{position:03d}.mp4",
            )
            for position, (row, path) in enumerate(resolved)
        ]
        concat(parts, dest)

    store.record_clip(
        conn,
        run_id=run_id,
        ordinal=1,
        path=dest.relative_to(workspace.root).as_posix(),
        duration_s=sum(float(row["duration_s"]) for row, _ in resolved),
        segments=[
            store.SegmentRecord(
                video_hash=row["video_hash"],
                seg_start_s=row["start_s"],
                seg_end_s=row["end_s"],
                shot_start_s=row["start_s"],
                shot_end_s=row["end_s"],
                shot_index=row["shot_index"],
                # From the library master, not the original source. It is the
                # exact footage that was just encoded, and it is still here
                # when the source is not -- which "masters for reuse" is an
                # open invitation to arrange. A mark with no picture is a
                # judgement about nothing, and a frame from a deleted file
                # cannot be recovered later by any change to this code.
                # The master starts at zero, so its own midpoint is the
                # middle of the shot.
                thumbnail=thumbnail_bytes(path, float(row["duration_s"]) / 2),
            )
            for row, path in resolved
        ],
    )
    return dest

