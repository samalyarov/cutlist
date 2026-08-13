import pytest

from cutlist.media.caption import (
    FontError,
    available_fonts,
    font_directories,
    resolve_font,
)


def test_font_directories_are_absolute_and_platform_appropriate():
    directories = font_directories()
    assert directories
    assert all(d.is_absolute() for d in directories)


def test_available_fonts_returns_family_and_path_pairs():
    fonts = available_fonts()
    if not fonts:
        pytest.skip("no fonts installed on this machine")
    family, path = fonts[0]
    assert isinstance(family, str) and family
    assert path.suffix.lower() in {".ttf", ".otf", ".ttc"}


def test_an_explicit_path_still_wins(tmp_path):
    fonts = available_fonts()
    if not fonts:
        pytest.skip("no fonts installed on this machine")
    _, path = fonts[0]
    assert resolve_font(str(path)) == path


def test_a_family_name_resolves_without_a_path():
    fonts = available_fonts()
    if not fonts:
        pytest.skip("no fonts installed on this machine")
    family, _ = fonts[0]
    assert resolve_font(family).exists()


def test_a_family_name_is_matched_ignoring_case_and_spacing():
    fonts = available_fonts()
    if not fonts:
        pytest.skip("no fonts installed on this machine")
    family, _ = fonts[0]
    mangled = family.upper().replace(" ", "").replace("-", "")
    assert resolve_font(mangled).exists()


def test_an_unknown_font_names_where_it_looked():
    with pytest.raises(FontError) as excinfo:
        resolve_font("ThisFontDoesNotExistAnywhere12345")
    message = str(excinfo.value)
    assert "ThisFontDoesNotExistAnywhere12345" in message
    # The point of the error: say where it searched, not merely that it failed.
    assert any(str(d) in message for d in font_directories())
