import pytest
from PIL import Image

from cutlist.media.caption import render_caption, resolve_font
from cutlist.presets import CaptionSpec, OutputSpec

OUTPUT = OutputSpec(width=854, height=480, fps=25, crf=20)


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


def test_cyrillic_renders_as_much_ink_as_latin(tmp_path):
    cyrillic = render(tmp_path / "a", "ЗАВТРА РИЛ СУББОТА")
    latin = render(tmp_path / "b", "ZAVTRA RIL SUBBOTA")
    cyrillic_ink = sum(cyrillic.getchannel("A").point(lambda v: v > 0 and 255).getdata())
    latin_ink = sum(latin.getchannel("A").point(lambda v: v > 0 and 255).getdata())
    assert cyrillic_ink > latin_ink * 0.5


def test_is_horizontally_centred(tmp_path):
    image = render(tmp_path)
    box = image.getchannel("A").getbbox()
    left_gap = box[0]
    right_gap = image.width - box[2]
    assert abs(left_gap - right_gap) <= 2


def test_resolve_font_finds_something():
    assert resolve_font(None).exists()
