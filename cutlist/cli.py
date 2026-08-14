import functools
import hashlib
import json
import random
import shutil
import socket
import uuid
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import typer

from cutlist.assemble import AssembleError, assemble_clips, parse_ids
from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.demo import build_demo_source
from cutlist.feedback.rate import parse_segment_marks
from cutlist.library import estimate as estimate_extraction
from cutlist.library import extract_all
from cutlist.media.caption import FontError, render_caption
from cutlist.media.probe import probe as probe_video
from cutlist.media.render import render_clip
from cutlist.media.shots import detect_shots
from cutlist.media.thumbs import thumbnail_bytes
from cutlist.paths import Workspace, resolve_within, video_id
from cutlist.presets import PresetError, load_preset
from cutlist.rebuild import RebuildError, rebuild_clip
from cutlist.select.naive import NotEnoughFootage, draft_picks
from cutlist.shell import ToolError

app = typer.Typer(help="Assemble short captioned clips from a long video.")

# Known failure modes get one clean line on stderr; everything else keeps its
# traceback. Deliberately an allowlist rather than bare ValueError/LookupError,
# which would also swallow concat()'s empty-input check and probe.py's
# unguarded ffprobe parsing -- those are bugs and should look like bugs.
HANDLED_ERRORS = (
    ToolError, PresetError, FontError, NotEnoughFootage, FileNotFoundError,
    store.RatingError, store.RatingNotFound, RebuildError, AssembleError,
)


def handle_errors(fn):
    """Report handled exceptions as `error: ...` on stderr instead of a traceback.

    A decorator on each command beats a try/except in each one -- new
    commands get the same boundary for free. functools.wraps keeps the
    original signature visible to typer, which inspects it to build the CLI.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HANDLED_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from None
    return wrapper


def _require(path: Path) -> None:
    """Fail fast on a missing file with the typo the user actually made.

    Without this, a mistyped path surfaces several calls deep as whatever
    exception the first thing to touch it happens to raise.
    """
    if not path.exists():
        raise FileNotFoundError(f"no such file: {path}")


@app.command()
@handle_errors
def probe(video: Path) -> None:
    """Show what ffprobe makes of a video."""
    _require(video)
    info = probe_video(video)

    typer.echo(f"{info.width}x{info.height} @ {info.fps:g}fps")
    typer.echo(f"{info.duration:.2f}s, audio: {'yes' if info.has_audio else 'no'}")


@app.command()
@handle_errors
def shots(
    video: Path,
    as_json: bool = typer.Option(False, "--json", help="Emit the shot list as JSON."),
) -> None:
    """Detect cuts and report the shots between them."""
    _require(video)
    found = detect_shots(video)

    if as_json:
        typer.echo(json.dumps(
            [{"index": s.index, "start": s.start, "end": s.end} for s in found],
            indent=2,
        ))
        return

    typer.echo(f"{len(found)} shots")
    lengths = sorted(s.duration for s in found)
    typer.echo(f"median {lengths[len(lengths) // 2]:.2f}s, longest {lengths[-1]:.2f}s")


def _cutlist_version() -> str:
    """Which build produced a run.

    Recorded so ratings from the random-selection era are never silently
    pooled with ratings from a later scoring era -- they measure different
    things.
    """
    try:
        return version("cutlist")
    except PackageNotFoundError:
        return "unknown"


def _preset_fingerprint(path: Path, spec) -> tuple[str, str]:
    """Hash the preset file, and serialise the resolved preset.

    The hash groups runs that used an identical preset. The JSON makes each
    run self-describing after the YAML is edited or deleted.
    """
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest, json.dumps(asdict(spec), sort_keys=True)


def _open_run(
    conn, *, video: Path, spec, preset_path: Path, seed: int
) -> tuple[str, int]:
    """Record the source and open a run, before anything is rendered.

    Opened first so a run that dies partway still records which source it was
    pointed at and which preset it was going to use.
    """
    info = probe_video(video)
    video_hash = video_id(video)
    store.record_video(
        conn,
        video_hash=video_hash,
        display_name=video.name,
        duration_s=info.duration,
        fps=info.fps,
        width=info.width,
        height=info.height,
    )
    preset_sha256, preset_json = _preset_fingerprint(preset_path, spec)
    run_id = store.start_run(
        conn,
        preset_name=spec.name,
        preset_sha256=preset_sha256,
        preset_json=preset_json,
        caption_text=spec.caption.text,
        seed=seed,
        cutlist_version=_cutlist_version(),
        video_hashes=[video_hash],
    )
    return video_hash, run_id


def _record_picks(
    conn, *, run_id: int, ordinal: int, clip: Path, root: Path,
    picks, video_hash: str, video: Path,
) -> None:
    """Record one rendered clip, the segments it was made of, and their frames.

    Thumbnails are captured now rather than on demand because they have to
    outlive the source video: once it is deleted, a frame from it cannot be
    recovered, and a segment mark with no picture is a judgement about nothing.
    """
    store.record_clip(
        conn,
        run_id=run_id,
        ordinal=ordinal,
        path=clip.relative_to(root).as_posix(),
        duration_s=sum(pick.segment.duration for pick in picks),
        segments=[
            store.SegmentRecord(
                video_hash=video_hash,
                seg_start_s=pick.segment.start,
                seg_end_s=pick.segment.end,
                shot_start_s=pick.shot.start,
                shot_end_s=pick.shot.end,
                shot_index=pick.shot.index,
                thumbnail=thumbnail_bytes(
                    video, (pick.segment.start + pick.segment.end) / 2
                ),
            )
            for pick in picks
        ],
    )


def _draft_clips(
    conn, *, video: Path, spec, workspace: Workspace,
    count: int, seed: int, preset_path: Path, keep_shots: bool = False,
) -> Path:
    """Detect shots, render `count` clips, and record what each was made of.

    Returns the directory the clips were written to.
    """
    typer.echo(f"caption: {spec.caption.text}")
    typer.echo("detecting shots...")
    found = detect_shots(video)
    typer.echo(f"{len(found)} shots")

    rng = random.Random(seed)
    video_hash, run_id = _open_run(
        conn, video=video, spec=spec, preset_path=preset_path, seed=seed
    )

    # Derived from run_id, so this run cannot write over an earlier one's clips.
    destination = workspace.output_for(video, spec.name, run_id)
    # Rooted in the cache dir and scoped by a random token: two concurrent
    # drafts of the same video and preset must not share a scratch path.
    scratch_root = workspace.cache_for(video) / f"scratch_{uuid.uuid4().hex[:8]}"

    try:
        # Keyed per run rather than per video: concurrent drafts of the same
        # video with different captions or presets must not overwrite each
        # other's PNG.
        caption_png = render_caption(
            spec.caption, spec.output, scratch_root / "caption.png"
        )

        written = 0
        for ordinal in range(1, count + 1):
            try:
                picks = draft_picks(found, spec.rhythm, rng)
                segments = [pick.segment for pick in picks]
                clip = destination / f"{ordinal:02d}.mp4"
                render_clip(
                    video, segments, caption_png, spec.output, clip,
                    scratch_root / f"{ordinal:02d}",
                )
                # Capturing thumbnails can fail the same way rendering can
                # (ffmpeg on a bad seek, a source that vanished mid-run), and a
                # clip on disk with no database row is exactly the state this
                # release exists to prevent. Kept inside the same boundary as
                # the render so a capture failure is reported and aborted
                # identically, not swallowed or left to record a clip missing
                # its thumbnails.
                _record_picks(
                    conn, run_id=run_id, ordinal=ordinal, clip=clip,
                    root=workspace.root, picks=picks, video_hash=video_hash,
                    video=video,
                )
                if keep_shots:
                    # The whole shots the picks came from, not the trimmed
                    # picks themselves -- a shot is the same shot whichever run
                    # found it, so the library keeps stable identities instead
                    # of near-duplicates at slightly different in and out
                    # points. Routed through extract_all so there is exactly
                    # one way footage enters the library, and it skips whatever
                    # is already there.
                    #
                    # Handled here rather than by the boundary below, because
                    # by this point the clip has landed: file, row, segments
                    # and thumbnails all exist. Letting an extraction failure
                    # out would count a clip that succeeded as failed, blame
                    # its ordinal for not landing, abort the clips after it,
                    # and suppress the closing line that says where the ones
                    # already written went. --keep-shots is a convenience; an
                    # optional side-effect does not get to do that.
                    try:
                        extract_all(
                            conn, video=video, workspace=workspace,
                            shots=[pick.shot for pick in picks],
                        )
                    except ToolError as exc:
                        typer.echo(
                            f"{ordinal:02d}: library copy skipped: {exc}", err=True
                        )
            except (NotEnoughFootage, ToolError) as exc:
                typer.echo(
                    f"wrote {written} of {count} clips; failed on {ordinal:02d}: {exc}",
                    err=True,
                )
                raise typer.Exit(code=1) from None

            length = sum(segment.duration for segment in segments)
            typer.echo(f"{clip.name}  {len(segments)} segments  {length:.1f}s")
            written += 1
    finally:
        # render_clip clears only its own per-ordinal subdirectory; the caption
        # PNG lives at the root of this one and has to outlast every clip, so
        # nothing below can remove it. Cleared here, on the failure path too,
        # or every draft leaves a scratch directory in cache/ for good.
        shutil.rmtree(scratch_root, ignore_errors=True)

    typer.echo(f"\nwrote {written} clips to {destination}  (seed {seed})")
    return destination


@app.command()
@handle_errors
def draft(
    video: Path,
    preset: Path = typer.Option(..., "--preset", help="Path to a preset YAML."),
    count: int = typer.Option(10, "--count", help="How many clips to produce."),
    caption: str | None = typer.Option(None, "--caption", help="Override the preset's text."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
    seed: int | None = typer.Option(None, "--seed", help="Fix the RNG for reproducible drafts."),
    keep_shots: bool = typer.Option(
        False, "--keep-shots",
        help="Also file the shots these clips were cut from into the library.",
    ),
) -> None:
    """Cut clips using random shot selection, with no scoring or judging."""
    _require(video)
    _require(preset)

    spec = load_preset(preset)
    if caption:
        spec = spec.with_caption(caption)

    # A run with no recorded seed cannot be reproduced, and an unreproducible
    # run cannot have its provenance rebuilt. Generate one rather than leaving
    # it to chance.
    if seed is None:
        seed = random.randrange(2**31)

    workspace = Workspace(root=root)
    _draft_clips(
        connect(workspace.database),
        video=video, spec=spec, workspace=workspace,
        count=count, seed=seed, preset_path=preset, keep_shots=keep_shots,
    )


@app.command()
@handle_errors
def demo(
    count: int = typer.Option(3, "--count", help="How many clips to produce."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
    seed: int | None = typer.Option(None, "--seed", help="Fix the RNG."),
) -> None:
    """Draft clips from a synthesised source, with no input file needed."""
    workspace = Workspace(root=root)
    source = workspace.input / "demo-source.mp4"

    if source.exists():
        typer.echo(f"using {source}")
    else:
        typer.echo("building a demo source video...")
        build_demo_source(source)

    preset_path = Path(__file__).with_name("demo.yaml")
    spec = load_preset(preset_path)
    if seed is None:
        seed = random.randrange(2**31)

    destination = _draft_clips(
        connect(workspace.database),
        video=source, spec=spec, workspace=workspace,
        count=count, seed=seed, preset_path=preset_path,
    )
    typer.echo(f"\nnow run:  cutlist review --root {root}")
    typer.echo(f"clips are in {destination}")


@app.command()
@handle_errors
def rate(
    clip: str = typer.Argument(..., help="Path of the clip, as written by draft."),
    verdict: str = typer.Argument(..., help="fire, ok or no."),
    segments: str | None = typer.Option(
        None, "--segments", help='Marks by position, e.g. "1:good,3:veto".'
    ),
    note: str | None = typer.Option(None, "--note", help="Free text to store with the verdict."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Rate a clip, and optionally mark the segments inside it."""
    workspace = Workspace(root=root)
    conn = connect(workspace.database)

    # Normalised so `output/01.mp4` and `output\01.mp4` both resolve.
    wanted = Path(clip).as_posix()
    row = store.clip_by_path(conn, wanted)
    if row is None:
        raise store.RatingNotFound(f"no recorded clip at {wanted}")

    marks = parse_segment_marks(segments) if segments else []
    detail = store.clip_detail(conn, row["id"])
    by_position = {s["position"] + 1: s["id"] for s in detail["segments"]}

    # Validated before anything is written, so a typo in one pair does not
    # leave half the marks recorded.
    for position, _ in marks:
        if position not in by_position:
            raise store.RatingNotFound(
                f"clip has {len(by_position)} segments; no segment {position}"
            )

    resolved = resolve_within(root, row["path"])
    if resolved is None or not resolved.exists():
        raise store.RatingNotFound(
            f"clip file is missing at {row['path']}; it cannot be given a verdict "
            f"(rerender it first)"
        )

    store.rate_clip(conn, clip_id=row["id"], verdict=verdict, note=note)
    for position, mark in marks:
        store.mark_shot(conn, segment_id=by_position[position], mark=mark)

    typer.echo(f"{wanted}: {verdict}" + (f", {len(marks)} segment marks" if marks else ""))


@app.command()
@handle_errors
def assemble(
    clips: str = typer.Argument(..., help='Library clip ids, e.g. "2,3,4" or "2-5,9".'),
    preset: Path = typer.Option(..., "--preset", help="Path to a preset YAML."),
    caption: str | None = typer.Option(None, "--caption", help="Override the preset's text."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Build a video from library clips you name, in the order you name them.

    The preset's caption and output settings apply; its rhythm does not -- you
    chose these clips, so a duration rule that dropped some of them would be
    answering a question you did not ask.
    """
    _require(preset)

    spec = load_preset(preset)
    if caption:
        spec = spec.with_caption(caption)

    ids = parse_ids(clips)
    workspace = Workspace(root=root)
    preset_sha256, preset_json = _preset_fingerprint(preset, spec)

    typer.echo(f"caption: {spec.caption.text}")
    typer.echo(f"assembling {len(ids)} clips...")

    written = assemble_clips(
        connect(workspace.database),
        ids=ids,
        spec=spec,
        workspace=workspace,
        preset_sha256=preset_sha256,
        preset_json=preset_json,
        cutlist_version=_cutlist_version(),
    )
    typer.echo(f"wrote {written.relative_to(root).as_posix()}")


@app.command()
@handle_errors
def ratings(
    as_json: bool = typer.Option(False, "--json", help="Emit the summary as JSON."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Report what has been rated so far."""
    conn = connect(Workspace(root=root).database)
    result = store.summary(conn)

    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return

    typer.echo(
        f"{result['videos']} videos, {result['runs']} runs, "
        f"{result['clips']} clips, {result['segments']} segments"
    )
    verdicts = result["verdicts"] or {}
    marks = result["marks"] or {}
    typer.echo("clips:    " + ", ".join(
        f"{k} {verdicts.get(k, 0)}" for k in store.VERDICTS
    ))
    typer.echo("segments: " + ", ".join(
        f"{k} {marks.get(k, 0)}" for k in store.MARKS
    ))


@app.command()
@handle_errors
def review(
    video: str | None = typer.Option(None, "--video", help="Filter by video hash."),
    preset: str | None = typer.Option(None, "--preset", help="Filter by preset name."),
    port: int = typer.Option(8731, "--port", help="Port to serve on."),
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="Interface to bind. Use 0.0.0.0 in a container; this exposes an "
             "unauthenticated server to your network.",
    ),
    all_clips: bool = typer.Option(False, "--all", help="Include clips already rated."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open a browser."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Serve the local review page."""
    import webbrowser

    from cutlist.review.server import build_server

    try:
        httpd = build_server(
            root=root, port=port, host=host, video=video, preset=preset,
            unrated_only=not all_clips,
        )
    # Checked before the general OSError below, which it subclasses: a host
    # that does not resolve fails here with nothing wrong with the port, and
    # blaming the port sends the user to fix the one thing they got right.
    except socket.gaierror as exc:
        typer.echo(f"error: cannot resolve host {host!r}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except OSError as exc:
        # Refuse rather than silently picking another port: a review URL you
        # did not ask for is worse than a clear failure.
        typer.echo(f"error: cannot bind port {port}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    # A browser cannot usefully open http://0.0.0.0; loopback is what a
    # container's published port actually resolves to on the host.
    shown = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{shown}:{httpd.server_address[1]}"
    typer.echo(f"review at {url}  (ctrl-c to stop)")
    if open_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nstopped")
    finally:
        httpd.server_close()


@app.command()
@handle_errors
def fonts(
    search: str | None = typer.Option(None, "--search", help="Filter by substring."),
) -> None:
    """List fonts a preset's caption.font can name."""
    from cutlist.media.caption import available_fonts

    found = available_fonts()
    if search:
        needle = search.lower()
        found = [(family, path) for family, path in found if needle in family.lower()]

    if not found:
        typer.echo("no fonts found" + (f" matching {search!r}" if search else ""))
        return

    for family, path in found:
        typer.echo(f"{family}\n    {path}")
    typer.echo(f"\n{len(found)} fonts")


@app.command()
@handle_errors
def rerender(
    clip: str = typer.Argument(..., help="Path of the clip, as written by draft."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Rebuild a clip from its recorded segments and preset."""
    conn = connect(Workspace(root=root).database)
    wanted = Path(clip).as_posix()
    row = store.clip_by_path(conn, wanted)
    if row is None:
        raise store.RatingNotFound(f"no recorded clip at {wanted}")

    written = rebuild_clip(conn, root=root, clip_id=row["id"])
    typer.echo(f"rebuilt {wanted} ({written.stat().st_size / 1024:.0f} KB)")


@app.command()
@handle_errors
def extract(
    video: Path,
    crf: int = typer.Option(18, "--crf", help="x264 quality for library masters (lower = better)."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Extract every detected shot into the reusable clip library."""
    _require(video)
    workspace = Workspace(root=root)
    conn = connect(workspace.database)

    typer.echo("detecting shots...")
    found = detect_shots(video)
    typer.echo(f"{len(found)} shots  ({estimate_extraction(video, found)})")

    def report(index: int, total: int, shot, status: str) -> None:
        typer.echo(f"[{index}/{total}] shot {shot.index}  {shot.duration:.2f}s  {status}")

    added, skipped = extract_all(
        conn, video=video, workspace=workspace, crf=crf, shots=found, on_progress=report
    )
    # Not the exact per-clip directory: library_path folds the source's
    # content hash into it, and re-deriving that here would duplicate a
    # naming scheme this module does not own. `cutlist library` has the
    # real paths.
    typer.echo(
        f"\n{added} added, {skipped} skipped -> {workspace.library}  (see `cutlist library`)"
    )


@app.command()
@handle_errors
def library(
    video: str | None = typer.Option(None, "--video", help="Filter by source video hash."),
    as_json: bool = typer.Option(False, "--json", help="Emit the list as JSON."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """List extracted library clips: id, source, timecode, duration, path.

    Exists so the ids `assemble` needs are actually discoverable -- an id
    nobody can look up is as good as no id at all.
    """
    conn = connect(Workspace(root=root).database)
    clips = store.library_clips(conn, video=video)

    if as_json:
        typer.echo(json.dumps(clips, indent=2))
        return

    if not clips:
        typer.echo("no library clips yet -- run `cutlist extract <video>` first")
        return

    for clip in clips:
        typer.echo(
            f"{clip['id']:>4}  {clip['display_name']:<24}  "
            f"{clip['start_s']:>9.3f}s  {clip['duration_s']:>6.2f}s  {clip['path']}"
        )
    typer.echo(f"\n{len(clips)} library clips")


# `python -m cutlist.cli` runs this file as __main__, where the console-script
# entry point never fires. Without this it exits 0 having done nothing, which
# reads like success.
if __name__ == "__main__":
    app()
