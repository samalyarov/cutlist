import functools
import hashlib
import json
import random
import uuid
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import typer

from cutlist.db import store
from cutlist.db.schema import connect
from cutlist.feedback.rate import parse_segment_marks
from cutlist.media.caption import FontError, render_caption
from cutlist.media.probe import probe as probe_film
from cutlist.media.render import render_clip
from cutlist.media.shots import detect_shots
from cutlist.paths import Workspace, film_id
from cutlist.presets import PresetError, load_preset
from cutlist.select.naive import NotEnoughFootage, draft_picks
from cutlist.shell import ToolError

app = typer.Typer(help="Assemble short captioned clips from a feature film.")

# Every failure mode a command can hit that isn't a click/typer usage error:
# a missing input file, a broken preset, a font that can't render the
# caption, footage too thin to draft from, an external tool dying, or a
# rating command asked to touch a clip/segment/verdict/mark that doesn't
# exist or doesn't validate. Deliberately a curated allowlist rather than
# bare ValueError/LookupError -- those also catch concat()'s "nothing to
# concatenate" invariant check and probe.py's unguarded ffprobe parsing
# (dict indexing and float()/int()/Fraction() on "N/A"), which are bugs
# that should traceback, not report as a clean one-liner. A person running
# this by hand should see one clean line for a *known* failure, not a
# stack trace through scenedetect/opencv/ffmpeg internals -- and a stack
# trace for anything else.
HANDLED_ERRORS = (
    ToolError, PresetError, FontError, NotEnoughFootage, FileNotFoundError,
    store.RatingError, store.RatingNotFound,
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
def probe(film: Path) -> None:
    """Show what ffprobe makes of a film."""
    _require(film)
    info = probe_film(film)

    typer.echo(f"{info.width}x{info.height} @ {info.fps:g}fps")
    typer.echo(f"{info.duration:.2f}s, audio: {'yes' if info.has_audio else 'no'}")


@app.command()
@handle_errors
def shots(
    film: Path,
    as_json: bool = typer.Option(False, "--json", help="Emit the shot list as JSON."),
) -> None:
    """Detect cuts and report the shots between them."""
    _require(film)
    found = detect_shots(film)

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


@app.command()
@handle_errors
def draft(
    film: Path,
    preset: Path = typer.Option(..., "--preset", help="Path to a preset YAML."),
    count: int = typer.Option(10, "--count", help="How many clips to produce."),
    caption: str | None = typer.Option(None, "--caption", help="Override the preset's text."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
    seed: int | None = typer.Option(None, "--seed", help="Fix the RNG for reproducible drafts."),
) -> None:
    """Cut clips using random shot selection, with no scoring or judging."""
    _require(film)
    _require(preset)

    spec = load_preset(preset)
    if caption:
        spec = spec.with_caption(caption)

    workspace = Workspace(root=root)
    destination = workspace.output_for(film, spec.name)

    typer.echo(f"caption: {spec.caption.text}")
    typer.echo("detecting shots...")
    found = detect_shots(film)
    typer.echo(f"{len(found)} shots")

    # A run with no recorded seed cannot be reproduced, and an unreproducible
    # run cannot have its provenance rebuilt if anything downstream is lost.
    # Generate one rather than leaving it to chance.
    if seed is None:
        seed = random.randrange(2**31)
    rng = random.Random(seed)

    info = probe_film(film)
    film_hash = film_id(film)
    conn = connect(workspace.database)
    store.record_film(
        conn,
        film_hash=film_hash,
        display_name=film.name,
        duration_s=info.duration,
        fps=info.fps,
        width=info.width,
        height=info.height,
    )

    preset_sha256, preset_json = _preset_fingerprint(preset, spec)
    # Opened before the first render, so a run that dies partway still
    # records which source it was pointed at.
    run_id = store.start_run(
        conn,
        preset_name=spec.name,
        preset_sha256=preset_sha256,
        preset_json=preset_json,
        caption_text=spec.caption.text,
        seed=seed,
        cutlist_version=_cutlist_version(),
        film_hashes=[film_hash],
    )

    # Scoped by a random token rather than just the clip index, and rooted
    # in the cache dir rather than the (shared, user-facing) output dir --
    # two concurrent drafts of the same film+preset used to both reach for
    # `.scratch_01` and race on each other's segment writes and rmtree.
    scratch_root = workspace.cache_for(film) / f"scratch_{uuid.uuid4().hex[:8]}"

    # Same collision the scratch dir above was fixed for: caption.png used to
    # be keyed on the film alone, so two concurrent drafts of the same film
    # with different captions or presets would overwrite each other's PNG
    # mid-run and burn the wrong text into the other run's clips. Scoping it
    # under this run's own scratch_root removes the shared path entirely.
    caption_png = render_caption(spec.caption, spec.output, scratch_root / "caption.png")

    written = 0
    for n in range(1, count + 1):
        try:
            picks = draft_picks(found, spec.rhythm, rng)
            segments = [pick.segment for pick in picks]
            clip = destination / f"{n:02d}.mp4"
            render_clip(
                film, segments, caption_png, spec.output, clip, scratch_root / f"{n:02d}"
            )
        except (NotEnoughFootage, ToolError) as exc:
            typer.echo(
                f"wrote {written} of {count} clips; failed on {n:02d}: {exc}", err=True
            )
            raise typer.Exit(code=1) from None

        length = sum(s.duration for s in segments)
        store.record_clip(
            conn,
            run_id=run_id,
            ordinal=n,
            path=clip.relative_to(root).as_posix(),
            duration_s=length,
            segments=[
                store.SegmentRecord(
                    film_hash=film_hash,
                    seg_start_s=pick.segment.start,
                    seg_end_s=pick.segment.end,
                    shot_start_s=pick.shot.start,
                    shot_end_s=pick.shot.end,
                    shot_index=pick.shot.index,
                )
                for pick in picks
            ],
        )

        typer.echo(f"{clip.name}  {len(segments)} segments  {length:.1f}s")
        written += 1

    typer.echo(f"\nwrote {written} clips to {destination}  (seed {seed})")


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

    store.rate_clip(conn, clip_id=row["id"], verdict=verdict, note=note)
    for position, mark in marks:
        store.mark_shot(conn, segment_id=by_position[position], mark=mark)

    typer.echo(f"{wanted}: {verdict}" + (f", {len(marks)} segment marks" if marks else ""))


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
        f"{result['films']} films, {result['runs']} runs, "
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
    film: str | None = typer.Option(None, "--film", help="Filter by film hash."),
    preset: str | None = typer.Option(None, "--preset", help="Filter by preset name."),
    port: int = typer.Option(8731, "--port", help="Port to serve on."),
    all_clips: bool = typer.Option(False, "--all", help="Include clips already rated."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open a browser."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
) -> None:
    """Serve the local review page."""
    import webbrowser

    from cutlist.review.server import build_server

    try:
        httpd = build_server(
            root=root, port=port, film=film, preset=preset,
            unrated_only=not all_clips,
        )
    except OSError as exc:
        # Refuse rather than silently picking another port: a review URL you
        # did not ask for is worse than a clear failure.
        typer.echo(f"error: cannot bind port {port}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    typer.echo(f"review at {url}  (ctrl-c to stop)")
    if open_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nstopped")
    finally:
        httpd.server_close()
