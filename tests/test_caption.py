from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from cutlist.media import caption as caption_module
from cutlist.media.caption import MAX_WIDTH_FRAC, FontError, render_caption, resolve_font
from cutlist.presets import CaptionSpec, OutputSpec

OUTPUT = OutputSpec(width=854, height=480, fps=25, crf=20)

# Ships with Windows and has no Cyrillic coverage -- exactly the "font draws
# tofu" scenario the glyph-coverage check exists to catch.
FONT_WITHOUT_CYRILLIC = Path(r"C:\Windows\Fonts\OCRAEXT.TTF")


def render(tmp_path, text="ЗАВТРА РИЛ СУББОТА"):
    dest = tmp_path / "caption.png"
    return Image.open(render_caption(CaptionSpec(text=text), OUTPUT, dest))


def test_matches_output_frame_size(tmp_path):
    image = render(tmp_path)
    assert image.size == (OUTPUT.width, OUTPUT.height)


def test_has_an_alpha_channel(tmp_path):
    assert render(tmp_path).mode == "RGBA"


def test_draws_in_the_top_band(tmp_path):
    image = render(tmp_path)
    alpha = image.getchannel("A")
    top = alpha.crop((0, 0, image.width, int(image.height * 0.25)))
    assert top.getbbox() is not None


def test_leaves_the_bottom_untouched(tmp_path):
    image = render(tmp_path)
    alpha = image.getchannel("A")
    bottom = alpha.crop((0, image.height // 2, image.width, image.height))
    assert bottom.getbbox() is None


def test_is_horizontally_centred(tmp_path):
    image = render(tmp_path)
    box = image.getchannel("A").getbbox()
    left_gap = box[0]
    right_gap = image.width - box[2]
    assert abs(left_gap - right_gap) <= 2


def test_resolve_font_finds_something():
    assert resolve_font(None).exists()


def test_resolve_font_raises_for_a_named_font_that_does_not_exist():
    with pytest.raises(FontError):
        resolve_font("no-such-font-anywhere.ttf")


def test_resolve_font_raises_when_no_candidate_exists(monkeypatch):
    monkeypatch.setattr(caption_module, "FONT_CANDIDATES", [])
    with pytest.raises(FontError):
        resolve_font(None)


@pytest.mark.skipif(
    not FONT_WITHOUT_CYRILLIC.exists(), reason="OCRAEXT.TTF not present on this machine"
)
def test_font_missing_cyrillic_glyphs_is_rejected(tmp_path):
    # An ink-count comparison can't catch this: OCRAEXT.TTF's tofu boxes for
    # Cyrillic are *denser* than real letterforms, not sparser, so a
    # threshold on ink alone would pass this font. Glyph-bitmap comparison
    # against .notdef is what actually detects the missing coverage.
    spec = CaptionSpec(text="ЗАВТРА РИЛ СУББОТА", font=str(FONT_WITHOUT_CYRILLIC))
    with pytest.raises(FontError):
        render_caption(spec, OUTPUT, tmp_path / "caption.png")


def test_long_caption_shrinks_to_fit_instead_of_clipping(tmp_path):
    text = "ЗАВТРА РИЛ СУББОТА " * 3
    image = render(tmp_path, text)
    box = image.getchannel("A").getbbox()
    assert box[2] - box[0] <= OUTPUT.width * MAX_WIDTH_FRAC + 1


def test_unrenderably_long_caption_raises_font_error(tmp_path):
    text = "ЗАВТРА РИЛ СУББОТА " * 20
    with pytest.raises(FontError):
        render_caption(CaptionSpec(text=text), OUTPUT, tmp_path / "caption.png")


def test_large_outline_does_not_clip_at_the_top(tmp_path):
    text = "ЗАВТРА РИЛ СУББОТА"
    spec = CaptionSpec(text=text, outline_frac=0.05)
    dest = render_caption(spec, OUTPUT, tmp_path / "caption.png")
    image = Image.open(dest)
    box = image.getchannel("A").getbbox()

    # Independently measure the glyphs' natural (un-clamped) height. If the
    # top got clipped against the canvas edge, the rendered bbox will be
    # shorter than this, regardless of where the text was positioned.
    font_path = resolve_font(spec.font)
    size = round(OUTPUT.height * spec.size_frac)
    stroke = round(size * (spec.outline_frac / spec.size_frac))
    font = ImageFont.truetype(str(font_path), size=size)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    top, bottom = probe.textbbox((0, 0), text, font=font, stroke_width=stroke, anchor="ma")[1::2]
    natural_height = bottom - top

    assert (box[3] - box[1]) >= natural_height - 1
