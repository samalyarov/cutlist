import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_repository_carries_an_mit_licence():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Sam Maliarov" in text


def test_pyproject_declares_the_licence_and_the_repository():
    project = _pyproject()["project"]
    assert project["license"] == "MIT"
    assert "github.com" in project["urls"]["Repository"]


def test_pyproject_points_at_the_readme():
    assert _pyproject()["project"]["readme"] == "README.md"


def test_line_endings_are_pinned():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in text
