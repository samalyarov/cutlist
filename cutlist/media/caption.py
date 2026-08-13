import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from cutlist.presets import CaptionSpec, OutputSpec, PresetError

TOP_MARGIN_FRAC = 0.015
BOTTOM_MARGIN_FRAC = 0.015
MAX_WIDTH_FRAC = 0.92
MIN_FONT_SIZE = 8

# (margin fraction, PIL anchor) for each position a preset can request.
# Anchor's second letter picks which edge of the text the (x, y) point
# pins to: "a" (ascender) for top placement, "d" (descender) for bottom --
# so the margin is measured from the same edge the text is anchored on.
_POSITIONS = {
    "top_center": (TOP_MARGIN_FRAC, "ma"),
    "bottom_center": (BOTTOM_MARGIN_FRAC, "md"),
}

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


FONT_SUFFIXES = frozenset({".ttf", ".otf", ".ttc"})


def font_directories() -> list[Path]:
    """Where this platform keeps fonts.

    Only directories that exist are returned, so the list doubles as the thing
    an error message can honestly claim to have searched.
    """
    home = Path.home()
    candidates = [
        Path(r"C:\Windows\Fonts"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        home / ".fonts",
        home / ".local" / "share" / "fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        home / "Library" / "Fonts",
    ]
    return [d for d in candidates if d.is_dir()]


def _normalised(name: str) -> str:
    """Fold the differences that do not distinguish one font from another."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _family_of(path: Path) -> str | None:
    """The font's own idea of its family name, or None if unreadable."""
    try:
        return ImageFont.truetype(str(path), size=12).getname()[0]
    except Exception:
        # A corrupt or unsupported file must not break enumeration.
        return None


def available_fonts() -> list[tuple[str, Path]]:
    """Every readable font on this machine, as (family, path), family-sorted."""
    seen: dict[str, Path] = {}
    for directory in font_directories():
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in FONT_SUFFIXES:
                continue
            family = _family_of(path)
            if family and family not in seen:
                seen[family] = path
    return sorted(seen.items())


def resolve_font(name: str | None) -> Path:
    """Find a font: an explicit path, a family name, or the platform default.

    A path is the strong claim and wins outright. A bare name is matched
    against installed fonts -- first on filename, then on the family name the
    font declares -- so a preset can say `font: "Impact"` without anyone having
    to know where the platform hides its fonts.
    """
    if not name:
        for candidate in FONT_CANDIDATES:
            if candidate.exists():
                return candidate
        raise FontError(
            "no usable font found; set caption.font to a font name or a .ttf "
            "path in the preset"
        )

    explicit = Path(name)
    if explicit.exists():
        return explicit

    wanted = _normalised(name)
    for directory in font_directories():
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in FONT_SUFFIXES:
                if _normalised(path.stem) == wanted:
                    return path

    for family, path in available_fonts():
        if _normalised(family) == wanted:
            return path

    searched = ", ".join(str(d) for d in font_directories())
    raise FontError(
        f"font not found: {name!r}. Looked for a file at that path, then for a "
        f"matching font in: {searched}. Run `cutlist fonts` to see what is "
        f"available."
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
    if spec.position not in _POSITIONS:
        raise PresetError(
            f"caption.position must be one of {sorted(_POSITIONS)}, got {spec.position!r}"
        )
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
    # past the near edge of the text by up to a stroke width. A margin
    # thinner than the stroke lets that overflow run past the canvas edge
    # and clip -- true whichever edge the caption is anchored to.
    margin_frac, anchor = _POSITIONS[spec.position]
    margin = max(round(output.height * margin_frac), stroke)
    y = margin if spec.position == "top_center" else output.height - margin

    canvas = Image.new("RGBA", (output.width, output.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (output.width // 2, y),
        spec.text,
        font=font,
        fill=spec.fill,
        stroke_width=stroke,
        stroke_fill=spec.outline,
        anchor=anchor,
    )

    canvas.save(dest)
    return dest
