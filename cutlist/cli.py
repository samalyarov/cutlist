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
# caption, footage too thin to draft from, or an external tool dying.
# A person running this by hand should see one clean line, not a stack
# trace through scenedetect/opencv/ffmpeg internals.
HANDLED_ERRORS = (ToolError, PresetError, FontError, NotEnoughFootage, FileNotFoundError)


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
