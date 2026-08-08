import json
import random
from pathlib import Path

import typer

from cutlist.media.caption import render_caption
from cutlist.media.probe import probe as probe_film
from cutlist.media.render import render_clip
from cutlist.media.shots import detect_shots
from cutlist.paths import Workspace
from cutlist.presets import load_preset
from cutlist.select.naive import NotEnoughFootage, draft_segments
from cutlist.shell import ToolError

app = typer.Typer(help="Assemble short captioned clips from a feature film.")


@app.command()
def probe(film: Path) -> None:
    """Show what ffprobe makes of a film."""
    try:
        info = probe_film(film)
    except ToolError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"{info.width}x{info.height} @ {info.fps:g}fps")
    typer.echo(f"{info.duration:.2f}s, audio: {'yes' if info.has_audio else 'no'}")


@app.command()
def shots(
    film: Path,
    as_json: bool = typer.Option(False, "--json", help="Emit the shot list as JSON."),
) -> None:
    """Detect cuts and report the shots between them."""
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
def draft(
    film: Path,
    preset: Path = typer.Option(..., "--preset", help="Path to a preset YAML."),
    count: int = typer.Option(10, "--count", help="How many clips to produce."),
    caption: str | None = typer.Option(None, "--caption", help="Override the preset's text."),
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
    seed: int | None = typer.Option(None, "--seed", help="Fix the RNG for reproducible drafts."),
) -> None:
    """Cut clips using random shot selection, with no scoring or judging."""
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

    for n in range(1, count + 1):
        try:
            segments = draft_segments(found, spec.rhythm, rng)
        except NotEnoughFootage as exc:
            raise typer.BadParameter(str(exc)) from exc

        clip = destination / f"{n:02d}.mp4"
        render_clip(
            film, segments, caption_png, spec.output, clip, destination / f".scratch_{n:02d}"
        )
        length = sum(s.duration for s in segments)
        typer.echo(f"{clip.name}  {len(segments)} segments  {length:.1f}s")

    typer.echo(f"\nwrote {count} clips to {destination}")
