import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cutlist.cli import app

runner = CliRunner()
PAGE = Path("cutlist/review/page.html").read_text(encoding="utf-8")

FORBIDDEN = [
    "#6366f1", "#8b5cf6", "radial-gradient", "backdrop-filter",
    "cdn.", "googleapis", "unpkg", "jsdelivr",
]


@pytest.mark.parametrize("marker", FORBIDDEN)
def test_page_avoids_generated_default_markers(marker):
    assert marker.lower() not in PAGE.lower()


def test_page_does_not_use_the_inter_typeface():
    """Word boundary, deliberately -- do not fold this into FORBIDDEN.

    A substring check for "Inter" also matches `cursor: pointer`, which cost
    the page a real click affordance once already. `\\b` will not match inside
    "pointer" because `o` and `i` are both word characters.
    """
    assert not re.search(r"\bInter\b", PAGE, re.IGNORECASE)


def test_the_only_gradient_is_the_veto_hatch():
    """Decorative gradients are the slop marker; a 45-degree hatch is texture.

    `veto` has to remove presence rather than add colour, and a hatch is how
    that reads without spending the colour budget.
    """
    gradients = re.findall(r"[a-z-]*gradient\(", PAGE)
    assert set(gradients) <= {"repeating-linear-gradient("}, gradients


def test_page_inlines_its_css_and_js():
    assert "<style>" in PAGE and "<script>" in PAGE
    assert "<link" not in PAGE
    assert 'src="http' not in PAGE


def test_page_uses_tabular_numerals():
    assert "tabular-nums" in PAGE


def test_page_declares_every_keybinding():
    for key in ["KeyF", "KeyO", "KeyN", "KeyG", "KeyB", "KeyV", "KeyZ", "Space"]:
        assert key in PAGE, f"missing binding for {key}"


def test_page_has_no_large_border_radius():
    radii = [int(v) for v in re.findall(r"border-radius:\s*(\d+)px", PAGE)]
    assert all(r <= 3 for r in radii), radii


def test_review_reports_a_taken_port(tmp_path):
    import socket

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.listen(1)
    try:
        result = runner.invoke(app, [
            "review", "--root", str(tmp_path), "--port", str(port), "--no-open",
        ])
        assert result.exit_code == 1
        assert str(port) in result.output
    finally:
        holder.close()


def test_build_server_binds_loopback_by_default(tmp_path):
    from cutlist.review.server import build_server

    httpd = build_server(root=tmp_path, port=0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_build_server_accepts_an_explicit_host(tmp_path):
    """Without this, a published container port reaches the container's own
    loopback and cutlist review is unreachable from the host."""
    from cutlist.review.server import build_server

    httpd = build_server(root=tmp_path, port=0, host="0.0.0.0")
    try:
        assert httpd.server_address[0] == "0.0.0.0"
    finally:
        httpd.server_close()
