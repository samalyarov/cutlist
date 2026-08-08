import functools
import json
import random
import uuid
from pathlib import Path

import typer

from cutlist.media.caption import FontError, render_caption
from cutlist.media.probe import probe as probe_film
from cutlist.media.render import render_clip
from cutlist.media.shots import detect_shots
from cutlist.paths import Workspace
from cutlist.presets import PresetError, load_preset
from cutlist.select.naive import NotEnoughFootage, draft_segments
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

    caption_png = render_caption(
        spec.caption, spec.output, workspace.cache_for(film) / "caption.png"
    )
    rng = random.Random(seed)

    # Scoped by a random token rather than just the clip index, and rooted
    # in the cache dir rather than the (shared, user-facing) output dir --
    # two concurrent drafts of the same film+preset used to both reach for
    # `.scratch_01` and race on each other's segment writes and rmtree.
    scratch_root = workspace.cache_for(film) / f"scratch_{uuid.uuid4().hex[:8]}"

    written = 0
    for n in range(1, count + 1):
        try:
            segments = draft_segments(found, spec.rhythm, rng)
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
        typer.echo(f"{clip.name}  {len(segments)} segments  {length:.1f}s")
        written += 1

    typer.echo(f"\nwrote {written} clips to {destination}")
