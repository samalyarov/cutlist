from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from cutlist.presets import CaptionSpec, OutputSpec

TOP_MARGIN_FRAC = 0.015

FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path(r"C:\Windows\Fonts\segoeuib.ttf"),
    Path(r"C:\Windows\Fonts\calibrib.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


class FontError(RuntimeError):
    """No usable font was found."""


def resolve_font(name: str | None) -> Path:
    """Find a bold font that covers Cyrillic.

    A preset may name an explicit file; otherwise fall back to whatever the
    platform ships. Arial Bold is the safe default on Windows.
    """
    if name:
        explicit = Path(name)
        if explicit.exists():
            return explicit
        raise FontError(f"font not found: {name}")

    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FontError(
        "no usable font found; set caption.font to a .ttf path in the preset"
    )


def render_caption(spec: CaptionSpec, output: OutputSpec, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)

    font = ImageFont.truetype(
        str(resolve_font(spec.font)),
        size=max(1, round(output.height * spec.size_frac)),
    )
    stroke = max(1, round(output.height * spec.outline_frac))

    canvas = Image.new("RGBA", (output.width, output.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (output.width // 2, round(output.height * TOP_MARGIN_FRAC)),
        spec.text,
        font=font,
        fill=spec.fill,
        stroke_width=stroke,
        stroke_fill=spec.outline,
        anchor="ma",
    )

    canvas.save(dest)
    return dest
