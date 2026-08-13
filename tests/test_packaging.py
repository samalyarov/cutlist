import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_repository_carries_the_agpl():
    """Assert on operative clauses, not the title.

    The previous version checked only that "MIT License" and the author's name
    appeared, so a licence with tampered permission or warranty clauses under
    an intact heading would have passed.
    """
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text
    # The clause that makes AGPL differ from GPL: network use is distribution.
    # The heading alone is not enough -- a corruption that stripped section
    # 13's operative paragraphs but left the heading would still match it.
    # Assert on the obligation itself, not just the section title.
    assert "remote network interaction" in text.lower()
    assert "your modified version must prominently offer all users" in text
    assert "THERE IS NO WARRANTY FOR THE PROGRAM" in text
    assert "Sam Maliarov" in text


def test_pyproject_declares_the_licence_and_the_repository():
    project = _pyproject()["project"]
    assert project["license"] == "AGPL-3.0-only"
    assert "github.com" in project["urls"]["Repository"]


def test_pyproject_points_at_the_readme():
    assert _pyproject()["project"]["readme"] == "README.md"


def test_line_endings_are_pinned():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in text
