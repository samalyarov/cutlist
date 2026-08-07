from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from cutlist.presets import CaptionSpec, OutputSpec

TOP_MARGIN_FRAC = 0.015
MAX_WIDTH_FRAC = 0.92
MIN_FONT_SIZE = 8

FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path(r"C:\Windows\Fonts\segoeuib.ttf"),
    Path(r"C:\Windows\Fonts\calibrib.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]

# A codepoint from the Private Use Area is essentially guaranteed to be
# unmapped, so asking any font to render it returns that font's .notdef
# fallback glyph. Comparing a caption character's rendered bitmap against
# that fallback tells us the font substituted tofu for it -- something an
# ink count can't: tofu boxes are frequently *denser* than the letterforms
# they replace, so a missing-glyph font can still pass an ink threshold.
_PROBE_CODEPOINT = "\ue000"


class FontError(RuntimeError):
    """No usable font was found, or it can't render the caption text."""


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


def _glyph_bitmap(font: ImageFont.FreeTypeFont, char: str) -> tuple[tuple[int, int], bytes]:
    mask = font.getmask(char, mode="L")
    return mask.size, bytes(mask)


def _missing_glyphs(font: ImageFont.FreeTypeFont, text: str) -> set[str]:
    """Characters in `text` that `font` has no real glyph for."""
    notdef = _glyph_bitmap(font, _PROBE_CODEPOINT)
    return {
        char
        for char in set(text)
        if not char.isspace() and _glyph_bitmap(font, char) == notdef
    }


def _fit_font(
    path: Path, text: str, nominal_size: int, max_width: float, probe: ImageDraw.ImageDraw
) -> ImageFont.FreeTypeFont:
    """Step the font size down until `text` fits within `max_width`.

    Caption text is user-supplied, so nothing bounds its length. Rather than
    let a long one clip silently at the frame edge, shrink to fit -- and
    refuse outright once shrinking would make it illegible.
    """
    size = nominal_size
    while size >= MIN_FONT_SIZE:
        font = ImageFont.truetype(str(path), size=size)
        if probe.textlength(text, font=font) <= max_width:
            return font
        size -= 1
    raise FontError(
        f"caption doesn't fit within {MAX_WIDTH_FRAC:.0%} of the frame width "
        f"even at the {MIN_FONT_SIZE}px floor: {text!r}"
    )


def render_caption(spec: CaptionSpec, output: OutputSpec, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)

    font_path = resolve_font(spec.font)
    nominal_size = max(1, round(output.height * spec.size_frac))
    # Outline width as a fraction of the font size, not of the frame height --
    # so a caption that shrinks to fit keeps a proportional outline instead of
    # one sized for the nominal (unshrunk) font.
    stroke_ratio = spec.outline_frac / spec.size_frac

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    font = _fit_font(font_path, spec.text, nominal_size, output.width * MAX_WIDTH_FRAC, probe)

    missing = _missing_glyphs(font, spec.text)
    if missing:
        raise FontError(f"{font_path} has no glyph for: {''.join(sorted(missing))!r}")

    stroke = max(1, round(font.size * stroke_ratio))

    # The outline is drawn around the glyph's own edges, so it can extend
    # above the top of the text by up to a stroke width. A margin thinner
    # than the stroke lets that overflow run past the canvas edge and clip.
    top_margin = max(round(output.height * TOP_MARGIN_FRAC), stroke)

    canvas = Image.new("RGBA", (output.width, output.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (output.width // 2, top_margin),
        spec.text,
        font=font,
        fill=spec.fill,
        stroke_width=stroke,
        stroke_fill=spec.outline,
        anchor="ma",
    )

    canvas.save(dest)
    return dest
