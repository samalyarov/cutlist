"""Building a video from library clips a person named, in the order they named.

The counterpart to `draft`: where drafting picks shots at random subject to a
preset's rhythm, assembling takes an explicit list and honours it. That is why
this module ignores `rhythm` entirely -- the clips were chosen deliberately, and
a duration rule that overrode the choice would be answering a question nobody
asked.
"""

import re
import tempfile
from pathlib import Path
from typing import NamedTuple

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

# ASCII digits and an optional fraction, nothing else. `float` also accepts
# "inf", "1e400", "nan" and non-ASCII digits, none of which anybody means as a
# number of seconds -- and "nan" compares false against everything, so it would
# sail through every bounds check below.
_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")

# A library filename and `cutlist library` both print durations to two
# decimals, so a 27.527s shot shows as "27.53s" and 27.53 is the number a person
# types. Half a hundredth would cover one rounding, but the two displays round
# slightly different values (the filename the unrounded shot length, the
# listing the stored one) and can disagree by a digit at NTSC frame rates. A
# whole hundredth accepts either as "to the end".
TRIM_SLACK = 0.01

# Ids are stored as SQLite INTEGER, a signed 64-bit value. A longer string of
# digits would pass `isdecimal` and then raise OverflowError at the query --
# or ValueError in `int` itself past Python's digit limit -- neither of which
# is a handled error. Eighteen digits is safely under 2**63.
_MAX_ID_DIGITS = 18


def _is_id(text: str) -> bool:
    return text.isdecimal() and len(text) <= _MAX_ID_DIGITS


class Pick(NamedTuple):
    """One library clip to play, and optionally which stretch of it.

    `trim` is `(start, end)` in seconds from the start of the library clip --
    offsets into the file a person watched while choosing, not source
    timecodes, which they never saw. `None` plays the whole shot.
    """

    id: int
    trim: tuple[float, float] | None = None


def _parse_trim(text: str, chunk: str) -> tuple[float, float]:
    start, dash, end = text.partition("-")
    start, end = start.strip(), end.strip()
    if not (dash and _SECONDS.fullmatch(start) and _SECONDS.fullmatch(end)):
        raise AssembleError(
            f"not a trim: {chunk!r}. Write it as id:start-end in seconds from "
            f"the start of that clip, e.g. 12:1.5-4."
        )
    if float(start) >= float(end):
        raise AssembleError(
            f"trim {chunk!r} is empty or runs backwards; its end must come after "
            f"its start."
        )
    return float(start), float(end)


def parse_ids(text: str) -> list[Pick]:
    """Parse `"2,3"`, `"2-5"` and `"2:1.5-4"` into picks of library clips.

    Order is preserved and repeats are kept, because the order is the edit:
    `3,1,3` means play clip 3, then 1, then 3 again -- and `3:0-2,1,3:4-6`
    plays two different stretches of clip 3 either side of clip 1.

    A trim names one clip. `2-5:0-1` is refused rather than applied to four
    shots alike: shots differ in length, so one trim across a range would mean
    something different for each of them, and is better spelled out.

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

    ids: list[Pick] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise AssembleError(f"empty entry in the clip list: {text!r}")

        # Split off the trim first: it carries a "-" of its own, which the
        # range check below would otherwise read as a range of ids.
        clip, colon, trim = chunk.partition(":")
        if colon:
            clip = clip.strip()
            if not _is_id(clip):
                raise AssembleError(
                    f"a trim applies to one clip id: {chunk!r}. List the ids "
                    f"separately, each with its own trim."
                )
            ids.append(Pick(int(clip), _parse_trim(trim, chunk)))
            continue

        if "-" in chunk.lstrip("-"):
            low, _, high = chunk.partition("-")
            low, high = low.strip(), high.strip()
            if not (_is_id(low) and _is_id(high)):
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
            ids.extend(Pick(i) for i in range(int(low), int(high) + 1))
            continue

        if not _is_id(chunk):
            raise AssembleError(f"not a clip id: {chunk!r}")
        ids.append(Pick(int(chunk)))

    if not ids:
        raise AssembleError("no clip ids given")
    return ids


class _Part(NamedTuple):
    row: object
    path: Path
    # Offsets into the library file: what gets encoded.
    start: float
    end: float
    # The same stretch in the original source: what gets recorded.
    source_start: float
    source_end: float


def _resolve(conn, root: Path, picks: list[Pick], fps: float) -> list[_Part]:
    """Fetch each id's row, confirm its file is still on disk, and fit its trim.

    Everything is checked before anything is encoded, so a typo in the last id
    -- or a trim past the end of the last clip -- does not surface after
    minutes of rendering.
    """
    parts = []
    for clip_id, trim in picks:
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

        duration = float(row["duration_s"])
        if trim is None:
            # The recorded bounds verbatim rather than start + duration added
            # back up: an untrimmed pick must record exactly the timecodes it
            # always has, and float addition need not round back to them.
            parts.append(_Part(row, path, 0.0, duration, row["start_s"], row["end_s"]))
            continue

        requested_start, requested_end = trim
        # Compared after rounding: 1.835 + 0.005 is 1.8399999999999999 in
        # floats, which refused exactly the end the slack exists to accept.
        if store.ms(requested_end - duration) > TRIM_SLACK:
            raise AssembleError(
                f"trim {requested_start:g}-{requested_end:g} runs past the end of "
                f"library clip {clip_id}, which is {duration:.3f}s long."
            )
        start, end = requested_start, min(requested_end, duration)
        # At least one output frame. Shorter, and the encoder writes one frame
        # anyway (a file longer than its record) or none at all (a late ffmpeg
        # error after the run row exists) -- and a sub-millisecond trim records
        # a zero-length segment that can still take a shot mark: a judgement
        # on footage nobody saw. Also catches a start the clamp put past the end.
        if end - start < 1 / fps:
            raise AssembleError(
                f"trim {requested_start:g}-{requested_end:g} on library clip "
                f"{clip_id} leaves {max(end - start, 0):.3f}s, shorter than one "
                f"frame at {fps:g} fps (clip is {duration:.3f}s long)."
            )
        parts.append(_Part(
            row, path, start, end,
            store.ms(row["start_s"] + start), store.ms(row["start_s"] + end),
        ))
    return parts


def assemble_clips(
    conn,
    *,
    ids: list[Pick],
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
    a new kind of source. A trimmed pick records its own stretch as the
    segment and the whole shot as the parent, exactly as a drafted segment
    does -- so a shot mark lands on the shot, and `rerender` cuts the same
    stretch from the source without knowing a trim was ever involved.
    """
    resolved = _resolve(conn, workspace.root, ids, spec.output.fps)

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
        video_hashes=sorted({part.row["video_hash"] for part in resolved}),
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
                part.path,
                # Untrimmed, this is the file's full span: the library clip is
                # already the shot.
                Segment(start=part.start, duration=part.end - part.start),
                caption_png,
                spec.output,
                scratch_root / f"part_{position:03d}.mp4",
            )
            for position, part in enumerate(resolved)
        ]
        concat(parts, dest)

    store.record_clip(
        conn,
        run_id=run_id,
        ordinal=1,
        path=dest.relative_to(workspace.root).as_posix(),
        # The recorded spans, not the encoded offsets, so the clip's length is
        # exactly the sum of its segments' and a consumer adding them agrees.
        duration_s=store.ms(sum(part.source_end - part.source_start for part in resolved)),
        segments=[
            store.SegmentRecord(
                video_hash=part.row["video_hash"],
                seg_start_s=part.source_start,
                seg_end_s=part.source_end,
                shot_start_s=part.row["start_s"],
                shot_end_s=part.row["end_s"],
                shot_index=part.row["shot_index"],
                # From the library master, not the original source. It is the
                # exact footage that was just encoded, and it is still here
                # when the source is not -- which "masters for reuse" is an
                # open invitation to arrange. A mark with no picture is a
                # judgement about nothing, and a frame from a deleted file
                # cannot be recovered later by any change to this code.
                # The master starts at zero, so the trim's own midpoint is
                # the middle of the footage actually used.
                thumbnail=thumbnail_bytes(part.path, (part.start + part.end) / 2),
            )
            for part in resolved
        ],
    )
    return dest
