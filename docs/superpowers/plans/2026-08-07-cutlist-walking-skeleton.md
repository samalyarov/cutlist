# cutlist Walking Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `cutlist draft film.mkv --preset real_saturday` produces silent 854×480 captioned MP4s assembled from segments cut out of different parts of the film, using random shot selection.

**Architecture:** A Python CLI over ffmpeg and PySceneDetect. Detect shots, pick some at random subject to duration rules, draw the caption once with Pillow, burn it in while encoding each segment, then concatenate without re-encoding. No ML and no agent in this plan — selection is deliberately dumb so the render path can be proven first.

**Tech Stack:** Python 3.12, typer, PySceneDetect, Pillow, PyYAML, ffmpeg/ffprobe (already on PATH), pytest.

## Global Constraints

- Python 3.12 only. `requires-python = ">=3.12,<3.13"`. The system default is 3.14 but PyTorch and PySceneDetect wheels for it are unreliable. The venv at `.venv` is already 3.12.10.
- No torch, open_clip, opencv or insightface in this plan. They arrive in plan 2. Keep the install fast.
- Output defaults: 854×480, 25fps, no audio, libx264, `-crf 20`, `-pix_fmt yuv420p`.
- Rhythm defaults: 4–10 segments, 1.2–2.8s each (target 2.0s), 9–15s total.
- Caption text lives in the preset and is overridable with `--caption`. Never hardcode a specific phrase in Python.
- Comments explain *why*, not *what*. No `# ----` separator bars, no section-divider comments, no walls of prose. Let names carry the explanation.
- Commit freely as each task completes; no sign-off gate. Work happens on `feature/walking-skeleton`, so review happens over the branch rather than per commit.
- Commit messages are short and meaningful. **No `Co-Authored-By` trailer, no "Generated with" line, no AI attribution of any kind.**
- Tests must not depend on anything in `references/`, `input/` or any other gitignored directory. Use the synthetic fixture from Task 2.

---

### Task 1: Project scaffolding and the subprocess helper

Everything downstream shells out to ffmpeg or ffprobe, so the error contract for that comes first.

**Files:**
- Create: `pyproject.toml`
- Create: `cutlist/__init__.py`
- Create: `cutlist/shell.py`
- Test: `tests/test_shell.py`

**Interfaces:**
- Consumes: nothing
- Produces: `cutlist.shell.run(cmd: list[str], *, timeout: int = 600) -> str` returning stdout; `cutlist.shell.ToolError(RuntimeError)`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "cutlist"
version = "0.1.0"
description = "Assemble short captioned clips from a feature film"
requires-python = ">=3.12,<3.13"
dependencies = [
    "typer>=0.12",
    "pyyaml>=6.0",
    "pillow>=10.3",
    "scenedetect>=0.6.4",
    "opencv-python-headless>=4.9",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
cutlist = "cutlist.cli:app"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["cutlist*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`opencv-python-headless` is PySceneDetect's decode backend. Headless avoids pulling in Qt.

- [ ] **Step 2: Create the package and install it**

Create `cutlist/__init__.py` containing only:

```python
__version__ = "0.1.0"
```

Run: `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`
Expected: installs cleanly, `cutlist` script appears in `.venv\Scripts`.

- [ ] **Step 3: Write the failing test**

`tests/test_shell.py`:

```python
import pytest

from cutlist.shell import ToolError, run


def test_run_returns_stdout():
    assert "ffprobe version" in run(["ffprobe", "-version"])


def test_run_raises_with_stderr_tail():
    with pytest.raises(ToolError) as excinfo:
        run(["ffprobe", "-i", "no-such-file.mp4"])
    message = str(excinfo.value)
    assert "no-such-file.mp4" in message
    assert "ffprobe" in message


def test_run_raises_when_binary_missing():
    with pytest.raises(ToolError):
        run(["definitely-not-a-real-binary"])
```

- [ ] **Step 4: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_shell.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cutlist.shell'`

- [ ] **Step 5: Write `cutlist/shell.py`**

```python
import subprocess

STDERR_TAIL_LINES = 15


class ToolError(RuntimeError):
    """An external tool failed, or could not be found."""


def run(cmd: list[str], *, timeout: int = 600) -> str:
    """Run a command and return its stdout.

    ffmpeg puts everything interesting on stderr and often fails deep into a
    long filtergraph, so failures report the command plus the tail of stderr
    rather than a bare exit code.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{cmd[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{cmd[0]} timed out after {timeout}s") from exc

    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-STDERR_TAIL_LINES:])
        raise ToolError(f"{' '.join(cmd)}\nexited {proc.returncode}\n{tail}")
    return proc.stdout
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_shell.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit (checkpoint — get sign-off first)**

```bash
git add pyproject.toml cutlist/__init__.py cutlist/shell.py tests/test_shell.py
git commit -m "feat: project scaffolding and subprocess helper"
```

---

### Task 2: Synthetic test fixture

Shot detection and rendering need a video with known cut positions. Generating one is better than committing a real clip: it's exact, it's tiny, and it isn't someone's copyrighted film.

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Test: `tests/test_fixture.py`

**Interfaces:**
- Consumes: `cutlist.shell.run`
- Produces: pytest fixtures `fixture_film` (a `Path` to a 30s video with cuts at 5/10/15/20/25s) and `FIXTURE_CUTS: list[float]`, `FIXTURE_DURATION: float`

- [ ] **Step 1: Make `tests` an importable package**

Create an empty `tests/__init__.py`.

Several later tests do `from tests.conftest import FIXTURE_CUTS` to avoid restating the fixture's known cut positions. Under pytest's default prepend import mode that only resolves if `tests` is a real package, so without this file Tasks 4 and 5 fail with `ModuleNotFoundError: No module named 'tests'`.

- [ ] **Step 2: Write the fixture**

`tests/conftest.py`:

```python
import pytest

from cutlist.shell import run

# Hex, not colour names, and chosen for luma separation: scene-score
# heuristics are luma-dominant, and red against ffmpeg's X11 "green"
# (0,128,0) scores zero because both land near Y=80.
FIXTURE_HEX_COLORS = ["0xFF0000", "0xFFFFFF", "0x000080", "0xFFFF00", "0x404040", "0x00FFFF"]
FIXTURE_SHOT_SECONDS = 5.0
FIXTURE_DURATION = FIXTURE_SHOT_SECONDS * len(FIXTURE_HEX_COLORS)
FIXTURE_CUTS = [FIXTURE_SHOT_SECONDS * i for i in range(1, len(FIXTURE_HEX_COLORS))]


@pytest.fixture(scope="session")
def fixture_film(tmp_path_factory):
    """A 30s video of six flat colours, cutting every 5s.

    Session-scoped because encoding it takes a second or two and nothing
    mutates it.
    """
    out = tmp_path_factory.mktemp("media") / "fixture.mp4"

    inputs = []
    for colour in FIXTURE_HEX_COLORS:
        inputs += [
            "-f", "lavfi",
            "-i", f"color=c={colour}:s=320x240:d={FIXTURE_SHOT_SECONDS}:r=25",
        ]
    labels = "".join(f"[{i}:v]" for i in range(len(FIXTURE_HEX_COLORS)))

    run([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", f"{labels}concat=n={len(FIXTURE_HEX_COLORS)}:v=1:a=0[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out),
    ])
    return out
```

- [ ] **Step 3: Write the failing test**

`tests/test_fixture.py`:

```python
from tests.conftest import FIXTURE_DURATION
from cutlist.shell import run


def test_fixture_film_has_expected_shape(fixture_film):
    assert fixture_film.exists()
    out = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=width,height",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(fixture_film),
    ])
    values = out.split()
    assert values[0] == "320"
    assert values[1] == "240"
    assert abs(float(values[2]) - FIXTURE_DURATION) < 0.5
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fixture.py -v`
Expected: PASS. This task has no implementation of its own — the fixture *is* the deliverable, so it goes green immediately.

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add tests/__init__.py tests/conftest.py tests/test_fixture.py
git commit -m "test: synthetic fixture film with known cut positions"
```

---

### Task 3: Film identity and directory layout

**Files:**
- Create: `cutlist/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `cutlist.paths.film_id(path: Path) -> str` — 32-char hex
  - `cutlist.paths.Workspace` dataclass with `root: Path` and properties `cache`, `work`, `output`, `input`
  - `Workspace.cache_for(path: Path) -> Path`
  - `Workspace.output_for(film: Path, preset_name: str) -> Path`

- [ ] **Step 1: Write the failing test**

`tests/test_paths.py`:

```python
import shutil

from cutlist.paths import Workspace, film_id


def test_film_id_is_stable_across_rename(fixture_film, tmp_path):
    moved = tmp_path / "renamed.mp4"
    shutil.copy(fixture_film, moved)
    assert film_id(moved) == film_id(fixture_film)


def test_film_id_differs_for_different_content(fixture_film, tmp_path):
    other = tmp_path / "other.mp4"
    other.write_bytes(fixture_film.read_bytes() + b"padding")
    assert film_id(other) != film_id(fixture_film)


def test_film_id_sees_content_past_the_first_megabyte(tmp_path):
    """Same size, differing only near the end.

    Without this, a film_id that hashed nothing but the file size would pass
    every other test here.
    """
    head = b"\x00" * (1 << 20)
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(head + b"\x01" * (1 << 19))
    second.write_bytes(head + b"\x02" * (1 << 19))
    assert first.stat().st_size == second.stat().st_size
    assert film_id(first) != film_id(second)


def test_film_id_reads_the_tail_of_large_files(tmp_path):
    head = b"\x00" * (3 << 20)
    first = tmp_path / "big_a.mp4"
    second = tmp_path / "big_b.mp4"
    first.write_bytes(head + b"\x01" * (1 << 20))
    second.write_bytes(head + b"\x02" * (1 << 20))
    assert film_id(first) != film_id(second)


def test_film_id_handles_tiny_files(tmp_path):
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"abc")
    assert len(film_id(tiny)) == 32


def test_workspace_paths_are_created_on_demand(tmp_path, fixture_film):
    ws = Workspace(root=tmp_path)
    cache = ws.cache_for(fixture_film)
    assert cache.is_dir()
    assert cache.parent == ws.cache
    assert film_id(fixture_film) in cache.name


def test_output_dir_groups_by_film_then_preset(tmp_path, fixture_film):
    ws = Workspace(root=tmp_path)
    out = ws.output_for(fixture_film, "real_saturday")
    assert out.is_dir()
    assert out.name == "real_saturday"
    assert out.parent.name == fixture_film.stem
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cutlist.paths'`

- [ ] **Step 3: Write `cutlist/paths.py`**

```python
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20


def film_id(path: Path) -> str:
    """Identify a film by size plus its first and last megabyte.

    Hashing the whole file would mean reading gigabytes just to look something
    up in the cache. Size plus both ends is enough to tell films apart while
    staying stable when the file is renamed or moved.
    """
    size = path.stat().st_size
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(size).encode())

    with path.open("rb") as handle:
        digest.update(handle.read(CHUNK))
        if size > 2 * CHUNK:
            # Skip the middle rather than read gigabytes of it.
            handle.seek(-CHUNK, os.SEEK_END)
            digest.update(handle.read(CHUNK))
        elif size > CHUNK:
            # Small enough that the remainder is under a megabyte anyway.
            digest.update(handle.read())

    return digest.hexdigest()


@dataclass(frozen=True)
class Workspace:
    """Where cutlist keeps things, split by what invalidates each directory."""

    root: Path

    @property
    def input(self) -> Path:
        return self.root / "input"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def output(self) -> Path:
        return self.root / "output"

    def cache_for(self, film: Path) -> Path:
        path = self.cache / f"{film.stem}__{film_id(film)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def output_for(self, film: Path, preset_name: str) -> Path:
        path = self.output / film.stem / preset_name
        path.mkdir(parents=True, exist_ok=True)
        return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paths.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add cutlist/paths.py tests/test_paths.py
git commit -m "feat: film identity hashing and workspace layout"
```

---

### Task 4: ffprobe wrapper

**Files:**
- Create: `cutlist/media/__init__.py`
- Create: `cutlist/media/probe.py`
- Test: `tests/test_probe.py`

**Interfaces:**
- Consumes: `cutlist.shell.run`
- Produces: `cutlist.media.probe.VideoInfo` (frozen dataclass: `path: Path`, `duration: float`, `width: int`, `height: int`, `fps: float`, `has_audio: bool`) and `cutlist.media.probe.probe(path: Path) -> VideoInfo`

- [ ] **Step 1: Write the failing test**

`tests/test_probe.py`:

```python
import pytest

from cutlist.media.probe import probe
from cutlist.shell import ToolError
from tests.conftest import FIXTURE_DURATION


def test_probe_reads_dimensions_and_fps(fixture_film):
    info = probe(fixture_film)
    assert (info.width, info.height) == (320, 240)
    assert info.fps == pytest.approx(25.0)


def test_probe_reads_duration(fixture_film):
    info = probe(fixture_film)
    assert info.duration == pytest.approx(FIXTURE_DURATION, abs=0.5)


def test_probe_detects_absent_audio(fixture_film):
    assert probe(fixture_film).has_audio is False


def test_probe_raises_on_missing_file(tmp_path):
    with pytest.raises(ToolError):
        probe(tmp_path / "nope.mp4")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_probe.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cutlist.media'`

- [ ] **Step 3: Write the module**

Create an empty `cutlist/media/__init__.py`, then `cutlist/media/probe.py`:

```python
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from cutlist.shell import run


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(path: Path) -> VideoInfo:
    payload = json.loads(run([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]))

    streams = payload["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")

    return VideoInfo(
        path=path,
        duration=float(payload["format"]["duration"]),
        width=int(video["width"]),
        height=int(video["height"]),
        fps=float(Fraction(video["r_frame_rate"])),
        has_audio=any(s["codec_type"] == "audio" for s in streams),
    )
```

`r_frame_rate` comes back as a string like `"25/1"`, so `Fraction` parses it without a manual split.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_probe.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add cutlist/media/__init__.py cutlist/media/probe.py tests/test_probe.py
git commit -m "feat: ffprobe wrapper"
```

---

### Task 5: Shot detection

**Files:**
- Create: `cutlist/media/shots.py`
- Test: `tests/test_shots.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (PySceneDetect reads the file directly)
- Produces:
  - `cutlist.media.shots.Shot` (frozen dataclass: `index: int`, `start: float`, `end: float`, with a `duration` property)
  - `cutlist.media.shots.detect_shots(path: Path, *, threshold: float = 27.0, min_shot_seconds: float = 0.4) -> list[Shot]`

- [ ] **Step 1: Write the failing test**

`tests/test_shots.py`:

```python
from cutlist.media.shots import detect_shots
from tests.conftest import FIXTURE_CUTS, FIXTURE_DURATION


def test_detects_every_cut_in_the_fixture(fixture_film):
    shots = detect_shots(fixture_film)
    assert len(shots) == len(FIXTURE_CUTS) + 1


def test_shot_boundaries_land_on_the_real_cuts(fixture_film):
    shots = detect_shots(fixture_film)
    detected = [shot.start for shot in shots[1:]]
    for expected, actual in zip(FIXTURE_CUTS, detected):
        assert abs(actual - expected) < 0.25


def test_shots_tile_the_film_without_gaps(fixture_film):
    shots = detect_shots(fixture_film)
    assert shots[0].start == 0.0
    assert abs(shots[-1].end - FIXTURE_DURATION) < 0.5
    for earlier, later in zip(shots, shots[1:]):
        assert earlier.end == later.start


def test_indices_are_sequential(fixture_film):
    shots = detect_shots(fixture_film)
    assert [shot.index for shot in shots] == list(range(len(shots)))


def test_duration_property(fixture_film):
    for shot in detect_shots(fixture_film):
        assert shot.duration == shot.end - shot.start
        assert shot.duration > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_shots.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cutlist.media.shots'`

- [ ] **Step 3: Write `cutlist/media/shots.py`**

```python
from dataclasses import dataclass
from pathlib import Path

from scenedetect import ContentDetector, detect


@dataclass(frozen=True)
class Shot:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_shots(
    path: Path,
    *,
    threshold: float = 27.0,
    min_shot_seconds: float = 0.4,
) -> list[Shot]:
    """Split a film into uninterrupted takes.

    min_shot_seconds suppresses the sub-frame detections that camera flashes
    and fast motion produce — measuring the reference clips turned up several
    "cuts" only 0.06s apart that were strobing, not editing.
    """
    scenes = detect(
        str(path),
        ContentDetector(threshold=threshold, min_scene_len=1),
        show_progress=False,
    )

    shots = [
        Shot(index=0, start=start.get_seconds(), end=end.get_seconds())
        for start, end in scenes
    ]
    return _renumber(_merge_short(shots, min_shot_seconds))


def _merge_short(shots: list[Shot], minimum: float) -> list[Shot]:
    merged: list[Shot] = []
    for shot in shots:
        if merged and shot.duration < minimum:
            previous = merged[-1]
            merged[-1] = Shot(previous.index, previous.start, shot.end)
        else:
            merged.append(shot)

    # A short opening shot has no predecessor to absorb it, so fold it
    # forward instead. Loops because several can stack up at the head.
    # A lone short shot is left alone — returning nothing would be worse.
    while len(merged) > 1 and merged[0].duration < minimum:
        head, following = merged[0], merged[1]
        merged[0:2] = [Shot(head.index, head.start, following.end)]

    return merged


def _renumber(shots: list[Shot]) -> list[Shot]:
    return [Shot(i, shot.start, shot.end) for i, shot in enumerate(shots)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_shots.py -v`
Expected: 5 passed

If `test_detects_every_cut_in_the_fixture` finds too few shots, lower `threshold` toward 20.0 — flat colour transitions score differently from real footage. Do not change the fixture to suit the detector.

- [ ] **Step 5: Time it against a real film (no commit yet)**

Run in a Python shell against any film in `input/`:

```python
import time
from pathlib import Path
from cutlist.media.shots import detect_shots

start = time.time()
shots = detect_shots(Path(r"input\your_film.mkv"))
print(len(shots), "shots in", round(time.time() - start), "s")
```

Expected: 1500–2500 shots. Note the wall time in the commit message — if a two-hour film takes more than about ten minutes, say so, because plan 2 caches this and the cost matters.

- [ ] **Step 6: Commit (checkpoint)**

```bash
git add cutlist/media/shots.py tests/test_shots.py
git commit -m "feat: shot detection with short-shot merging"
```

---

### Task 6: Presets

Only the caption, rhythm and output blocks exist in this plan. The `selection` block arrives in plan 2, so unknown top-level keys are ignored rather than rejected.

**Files:**
- Create: `cutlist/presets.py`
- Create: `presets/real_saturday.yaml`
- Test: `tests/test_presets.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `cutlist.presets.CaptionSpec` — `text: str`, `position: str`, `font: str | None`, `size_frac: float`, `fill: str`, `outline: str`, `outline_frac: float`
  - `cutlist.presets.RhythmSpec` — `min_segments: int`, `max_segments: int`, `min_seconds: float`, `target_seconds: float`, `max_seconds: float`, `min_total: float`, `max_total: float`
  - `cutlist.presets.OutputSpec` — `width: int`, `height: int`, `fps: int`, `crf: int`
  - `cutlist.presets.Preset` — `name: str`, `caption: CaptionSpec`, `rhythm: RhythmSpec`, `output: OutputSpec`, plus `with_caption(text: str) -> Preset`
  - `cutlist.presets.load_preset(path: Path) -> Preset`
  - `cutlist.presets.PresetError(ValueError)`

- [ ] **Step 1: Write the failing test**

`tests/test_presets.py`:

```python
import textwrap

import pytest

from cutlist.presets import PresetError, load_preset

VALID = """
name: demo
caption:
  text: "HELLO"
  position: top_center
  size_frac: 0.065
  fill: "#FFFFFF"
  outline: "#000000"
  outline_frac: 0.006
rhythm:
  segments: {min: 4, max: 10}
  seg_duration: {min: 1.2, target: 2.0, max: 2.8}
  total: {min: 9, max: 15}
output:
  width: 854
  height: 480
  fps: 25
  crf: 20
"""


def write(tmp_path, body):
    path = tmp_path / "preset.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_loads_a_valid_preset(tmp_path):
    preset = load_preset(write(tmp_path, VALID))
    assert preset.name == "demo"
    assert preset.caption.text == "HELLO"
    assert preset.rhythm.max_segments == 10
    assert preset.output.width == 854


def test_with_caption_overrides_text_only(tmp_path):
    preset = load_preset(write(tmp_path, VALID))
    changed = preset.with_caption("ДРУГОЙ ТЕКСТ")
    assert changed.caption.text == "ДРУГОЙ ТЕКСТ"
    assert changed.caption.size_frac == preset.caption.size_frac
    assert preset.caption.text == "HELLO"


def test_rejects_unreachable_total_minimum(tmp_path):
    body = VALID.replace("total: {min: 9, max: 15}", "total: {min: 40, max: 60}")
    with pytest.raises(PresetError, match="total"):
        load_preset(write(tmp_path, body))


def test_rejects_inverted_segment_bounds(tmp_path):
    body = VALID.replace("segments: {min: 4, max: 10}", "segments: {min: 10, max: 4}")
    with pytest.raises(PresetError, match="segments"):
        load_preset(write(tmp_path, body))


def test_rejects_target_outside_duration_bounds(tmp_path):
    body = VALID.replace(
        "seg_duration: {min: 1.2, target: 2.0, max: 2.8}",
        "seg_duration: {min: 1.2, target: 9.0, max: 2.8}",
    )
    with pytest.raises(PresetError, match="target"):
        load_preset(write(tmp_path, body))


def test_rejects_missing_caption_text(tmp_path):
    body = VALID.replace('  text: "HELLO"\n', "")
    with pytest.raises(PresetError, match="text"):
        load_preset(write(tmp_path, body))


def test_ignores_unknown_blocks(tmp_path):
    preset = load_preset(write(tmp_path, VALID + "\nselection:\n  mode: beats\n"))
    assert preset.name == "demo"


def test_shipped_preset_loads():
    from pathlib import Path
    preset = load_preset(Path("presets/real_saturday.yaml"))
    assert preset.caption.text
    assert preset.output.height == 480
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_presets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cutlist.presets'`

- [ ] **Step 3: Write `cutlist/presets.py`**

```python
from dataclasses import dataclass, replace
from pathlib import Path

import yaml


class PresetError(ValueError):
    """A preset is missing something, or asks for something impossible."""


@dataclass(frozen=True)
class CaptionSpec:
    text: str
    position: str = "top_center"
    font: str | None = None
    size_frac: float = 0.065
    fill: str = "#FFFFFF"
    outline: str = "#000000"
    outline_frac: float = 0.006


@dataclass(frozen=True)
class RhythmSpec:
    min_segments: int
    max_segments: int
    min_seconds: float
    target_seconds: float
    max_seconds: float
    min_total: float
    max_total: float


@dataclass(frozen=True)
class OutputSpec:
    width: int = 854
    height: int = 480
    fps: int = 25
    crf: int = 20


@dataclass(frozen=True)
class Preset:
    name: str
    caption: CaptionSpec
    rhythm: RhythmSpec
    output: OutputSpec

    def with_caption(self, text: str) -> "Preset":
        return replace(self, caption=replace(self.caption, text=text))


def load_preset(path: Path) -> Preset:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    name = raw.get("name") or path.stem
    caption = _caption(raw.get("caption") or {})
    rhythm = _rhythm(raw.get("rhythm") or {})
    output = OutputSpec(**(raw.get("output") or {}))

    _validate(rhythm)
    return Preset(name=name, caption=caption, rhythm=rhythm, output=output)


def _caption(block: dict) -> CaptionSpec:
    if not block.get("text"):
        raise PresetError("caption.text is required and must not be empty")
    return CaptionSpec(**block)


def _rhythm(block: dict) -> RhythmSpec:
    try:
        segments = block["segments"]
        duration = block["seg_duration"]
        total = block["total"]
        return RhythmSpec(
            min_segments=int(segments["min"]),
            max_segments=int(segments["max"]),
            min_seconds=float(duration["min"]),
            target_seconds=float(duration["target"]),
            max_seconds=float(duration["max"]),
            min_total=float(total["min"]),
            max_total=float(total["max"]),
        )
    except KeyError as exc:
        raise PresetError(f"rhythm is missing {exc}") from exc


def _validate(rhythm: RhythmSpec) -> None:
    if rhythm.min_segments > rhythm.max_segments:
        raise PresetError("rhythm.segments.min exceeds segments.max")
    if rhythm.min_seconds > rhythm.max_seconds:
        raise PresetError("rhythm.seg_duration.min exceeds seg_duration.max")
    if not rhythm.min_seconds <= rhythm.target_seconds <= rhythm.max_seconds:
        raise PresetError("rhythm.seg_duration.target is outside min..max")
    if rhythm.min_total > rhythm.max_total:
        raise PresetError("rhythm.total.min exceeds total.max")

    longest = rhythm.max_segments * rhythm.max_seconds
    shortest = rhythm.min_segments * rhythm.min_seconds
    if rhythm.min_total > longest:
        raise PresetError(
            f"rhythm.total.min of {rhythm.min_total}s is unreachable: "
            f"at most {longest}s fits in {rhythm.max_segments} segments"
        )
    if rhythm.max_total < shortest:
        raise PresetError(
            f"rhythm.total.max of {rhythm.max_total}s is unreachable: "
            f"{rhythm.min_segments} segments run at least {shortest}s"
        )
```

- [ ] **Step 4: Write `presets/real_saturday.yaml`**

```yaml
name: real_saturday

caption:
  text: "ЗАВТРА РИЛ СУББОТА"
  position: top_center
  size_frac: 0.065
  fill: "#FFFFFF"
  outline: "#000000"
  outline_frac: 0.006

rhythm:
  segments: {min: 4, max: 10}
  seg_duration: {min: 1.2, target: 2.0, max: 2.8}
  total: {min: 9, max: 15}

output:
  width: 854
  height: 480
  fps: 25
  crf: 20
```

Save it as UTF-8 without a BOM. PyYAML reads the Cyrillic fine; a BOM breaks the first key.

- [ ] **Step 5: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_presets.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit (checkpoint)**

```bash
git add cutlist/presets.py presets/real_saturday.yaml tests/test_presets.py
git commit -m "feat: preset loading and validation"
```

---

### Task 7: Caption rendering with Pillow

ffmpeg's `drawtext` emits `Fontconfig error: Cannot load default config file` on this machine and falls back unpredictably, so the caption is drawn to a transparent PNG instead and composited later.

**Files:**
- Create: `cutlist/media/caption.py`
- Test: `tests/test_caption.py`

**Interfaces:**
- Consumes: `cutlist.presets.CaptionSpec`, `cutlist.presets.OutputSpec`
- Produces:
  - `cutlist.media.caption.render_caption(spec: CaptionSpec, output: OutputSpec, dest: Path) -> Path`
  - `cutlist.media.caption.resolve_font(name: str | None) -> Path`
  - `cutlist.media.caption.FontError(RuntimeError)`

- [ ] **Step 1: Write the failing test**

`tests/test_caption.py`:

```python
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
```

`test_cyrillic_renders_as_much_ink_as_latin` is the one that matters: a font missing Cyrillic glyphs draws tofu boxes or nothing, and the ink comparison catches both.

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_caption.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cutlist.media.caption'`

- [ ] **Step 3: Write `cutlist/media/caption.py`**

```python
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
```

`anchor="ma"` means middle-horizontal, ascender-vertical, which centres the text without measuring it by hand.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_caption.py -v`
Expected: 7 passed

- [ ] **Step 5: Eyeball it**

```python
from pathlib import Path
from cutlist.media.caption import render_caption
from cutlist.presets import load_preset

preset = load_preset(Path("presets/real_saturday.yaml"))
render_caption(preset.caption, preset.output, Path("caption_preview.png"))
```

Open `caption_preview.png` and compare against `references/hollywood_reference.mp4`. Adjust `size_frac`, `outline_frac` and `TOP_MARGIN_FRAC` until it matches. Delete the preview afterwards — it's gitignored anyway.

- [ ] **Step 6: Commit (checkpoint)**

```bash
git add cutlist/media/caption.py tests/test_caption.py
git commit -m "feat: caption rendering with Pillow"
```

---

### Task 8: Segment encoding and concatenation

**Files:**
- Create: `cutlist/media/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `cutlist.shell.run`, `cutlist.presets.OutputSpec`, `cutlist.media.probe.probe`
- Produces:
  - `cutlist.media.render.Segment` (frozen dataclass: `start: float`, `duration: float`) with an `end` property
  - `cutlist.media.render.encode_segment(film, segment, caption_png, output, dest) -> Path`
  - `cutlist.media.render.concat(parts: list[Path], dest: Path) -> Path`
  - `cutlist.media.render.render_clip(film, segments, caption_png, output, dest, scratch) -> Path`

- [ ] **Step 1: Write the failing test**

`tests/test_render.py`:

```python
import pytest

from cutlist.media.caption import render_caption
from cutlist.media.probe import probe
from cutlist.media.render import Segment, concat, encode_segment, render_clip
from cutlist.presets import CaptionSpec, OutputSpec

OUTPUT = OutputSpec(width=854, height=480, fps=25, crf=20)


@pytest.fixture
def caption(tmp_path):
    return render_caption(CaptionSpec(text="ТЕСТ"), OUTPUT, tmp_path / "caption.png")


def test_segment_end(tmp_path):
    assert Segment(start=3.0, duration=2.0).end == 5.0


def test_encoded_segment_matches_output_spec(fixture_film, caption, tmp_path):
    dest = encode_segment(
        fixture_film, Segment(2.0, 2.0), caption, OUTPUT, tmp_path / "seg.mp4"
    )
    info = probe(dest)
    assert (info.width, info.height) == (854, 480)
    assert info.fps == pytest.approx(25.0)
    assert info.duration == pytest.approx(2.0, abs=0.2)


def test_encoded_segment_is_silent(fixture_film, caption, tmp_path):
    dest = encode_segment(
        fixture_film, Segment(2.0, 2.0), caption, OUTPUT, tmp_path / "seg.mp4"
    )
    assert probe(dest).has_audio is False


def test_concat_sums_durations(fixture_film, caption, tmp_path):
    parts = [
        encode_segment(
            fixture_film, Segment(start, 2.0), caption, OUTPUT, tmp_path / f"s{i}.mp4"
        )
        for i, start in enumerate([2.0, 7.0, 12.0])
    ]
    dest = concat(parts, tmp_path / "joined.mp4")
    assert probe(dest).duration == pytest.approx(6.0, abs=0.4)


def test_render_clip_end_to_end(fixture_film, caption, tmp_path):
    segments = [Segment(2.0, 2.0), Segment(7.0, 2.5), Segment(17.0, 2.0)]
    dest = render_clip(
        fixture_film, segments, caption, OUTPUT, tmp_path / "clip.mp4", tmp_path / "scratch"
    )
    info = probe(dest)
    assert info.has_audio is False
    assert (info.width, info.height) == (854, 480)
    assert info.duration == pytest.approx(6.5, abs=0.4)


def test_render_clip_cleans_up_scratch(fixture_film, caption, tmp_path):
    scratch = tmp_path / "scratch"
    render_clip(
        fixture_film, [Segment(2.0, 2.0)], caption, OUTPUT, tmp_path / "clip.mp4", scratch
    )
    assert not scratch.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_render.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cutlist.media.render'`

- [ ] **Step 3: Write `cutlist/media/render.py`**

```python
import shutil
from dataclasses import dataclass
from pathlib import Path

from cutlist.presets import OutputSpec
from cutlist.shell import run


@dataclass(frozen=True)
class Segment:
    """A slice of the source film, in source timecodes."""

    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def encode_segment(
    film: Path,
    segment: Segment,
    caption_png: Path,
    output: OutputSpec,
    dest: Path,
) -> Path:
    """Cut one segment, letterbox it to the output size, and burn in the caption.

    The caption never changes within a clip, so compositing it here means the
    whole pipeline needs exactly one encode pass. Every segment then starts on
    a keyframe, which is what makes the later concat safe.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    scale = (
        f"[0:v]scale={output.width}:{output.height}"
        ":force_original_aspect_ratio=decrease,"
        f"pad={output.width}:{output.height}:-1:-1:color=black,"
        f"fps={output.fps},setsar=1[v];[v][1:v]overlay=0:0"
    )

    run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{segment.start:.3f}",
        "-i", str(film),
        "-i", str(caption_png),
        "-t", f"{segment.duration:.3f}",
        "-an",
        "-filter_complex", scale,
        "-c:v", "libx264", "-crf", str(output.crf), "-pix_fmt", "yuv420p",
        str(dest),
    ])
    return dest


def concat(parts: list[Path], dest: Path) -> Path:
    """Join encoded segments without re-encoding."""
    if not parts:
        raise ValueError("nothing to concatenate")

    dest.parent.mkdir(parents=True, exist_ok=True)
    listing = dest.parent / f"{dest.stem}_parts.txt"
    listing.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts),
        encoding="utf-8",
    )

    try:
        run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(listing),
            "-c", "copy",
            str(dest),
        ])
    finally:
        listing.unlink(missing_ok=True)
    return dest


def render_clip(
    film: Path,
    segments: list[Segment],
    caption_png: Path,
    output: OutputSpec,
    dest: Path,
    scratch: Path,
) -> Path:
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        parts = [
            encode_segment(
                film, segment, caption_png, output, scratch / f"seg_{i:02d}.mp4"
            )
            for i, segment in enumerate(segments)
        ]
        concat(parts, dest)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return dest
```

`as_posix()` matters: the concat demuxer chokes on Windows backslashes because it treats them as escapes.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_render.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add cutlist/media/render.py tests/test_render.py
git commit -m "feat: segment encoding and lossless concat"
```

---

### Task 9: Naive segment selection

Random selection, standing in for the scored version that arrives in plan 2. It exists so the render path can be exercised end to end, and so the duration rules have somewhere to live and be tested.

**Files:**
- Create: `cutlist/select/__init__.py`
- Create: `cutlist/select/naive.py`
- Test: `tests/test_naive.py`

**Interfaces:**
- Consumes: `cutlist.media.shots.Shot`, `cutlist.media.render.Segment`, `cutlist.presets.RhythmSpec`
- Produces:
  - `cutlist.select.naive.draft_segments(shots: list[Shot], rhythm: RhythmSpec, rng: random.Random) -> list[Segment]`
  - `cutlist.select.naive.NotEnoughFootage(RuntimeError)`

- [ ] **Step 1: Write the failing test**

`tests/test_naive.py`:

```python
import random

import pytest

from cutlist.media.shots import Shot
from cutlist.presets import RhythmSpec
from cutlist.select.naive import NotEnoughFootage, draft_segments

RHYTHM = RhythmSpec(
    min_segments=4, max_segments=10,
    min_seconds=1.2, target_seconds=2.0, max_seconds=2.8,
    min_total=9.0, max_total=15.0,
)


def make_shots(count, length=6.0):
    return [Shot(i, i * length, (i + 1) * length) for i in range(count)]


@pytest.mark.parametrize("seed", range(25))
def test_respects_every_duration_rule(seed):
    segments = draft_segments(make_shots(40), RHYTHM, random.Random(seed))

    assert RHYTHM.min_segments <= len(segments) <= RHYTHM.max_segments
    for segment in segments:
        assert RHYTHM.min_seconds - 1e-6 <= segment.duration <= RHYTHM.max_seconds + 1e-6
    total = sum(s.duration for s in segments)
    assert RHYTHM.min_total - 1e-6 <= total <= RHYTHM.max_total + 1e-6


@pytest.mark.parametrize("seed", range(25))
def test_every_segment_sits_inside_a_real_shot(seed):
    shots = make_shots(40)
    segments = draft_segments(shots, RHYTHM, random.Random(seed))

    for segment in segments:
        assert any(
            shot.start <= segment.start and segment.end <= shot.end for shot in shots
        )


@pytest.mark.parametrize("seed", range(10))
def test_segments_are_ordered_by_timecode(seed):
    segments = draft_segments(make_shots(40), RHYTHM, random.Random(seed))
    assert [s.start for s in segments] == sorted(s.start for s in segments)


def test_never_reuses_a_shot():
    shots = make_shots(40)
    segments = draft_segments(shots, RHYTHM, random.Random(0))
    owners = [
        next(s.index for s in shots if s.start <= seg.start and seg.end <= s.end)
        for seg in segments
    ]
    assert len(set(owners)) == len(owners)


def test_different_seeds_give_different_drafts():
    shots = make_shots(40)
    first = draft_segments(shots, RHYTHM, random.Random(1))
    second = draft_segments(shots, RHYTHM, random.Random(2))
    assert [s.start for s in first] != [s.start for s in second]


def test_ignores_shots_shorter_than_the_minimum():
    shots = [Shot(0, 0.0, 0.3), Shot(1, 0.3, 0.6)] + [
        Shot(i, i * 6.0, (i + 1) * 6.0) for i in range(2, 30)
    ]
    segments = draft_segments(shots, RHYTHM, random.Random(0))
    for segment in segments:
        assert segment.start >= 12.0


def test_raises_when_there_is_not_enough_footage():
    with pytest.raises(NotEnoughFootage):
        draft_segments(make_shots(2), RHYTHM, random.Random(0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_naive.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cutlist.select'`

- [ ] **Step 3: Write the module**

Create an empty `cutlist/select/__init__.py`, then `cutlist/select/naive.py`:

```python
import random

from cutlist.media.render import Segment
from cutlist.media.shots import Shot
from cutlist.presets import RhythmSpec


class NotEnoughFootage(RuntimeError):
    """The film has too few usable shots to fill an assembly."""


def draft_segments(
    shots: list[Shot],
    rhythm: RhythmSpec,
    rng: random.Random,
) -> list[Segment]:
    """Pick segments at random, subject to the preset's duration rules.

    Selection here is deliberately blind — it only enforces the rhythm. Scoring
    replaces the sampling step later without changing the duration logic.
    """
    usable = [shot for shot in shots if shot.duration >= rhythm.min_seconds]
    if len(usable) < rhythm.min_segments:
        raise NotEnoughFootage(
            f"need at least {rhythm.min_segments} shots of "
            f"{rhythm.min_seconds}s or more, found {len(usable)}"
        )

    count = rng.randint(rhythm.min_segments, min(rhythm.max_segments, len(usable)))
    chosen = sorted(rng.sample(usable, count), key=lambda shot: shot.start)

    durations = _fit_total(
        [min(rhythm.target_seconds, shot.duration) for shot in chosen],
        [min(rhythm.max_seconds, shot.duration) for shot in chosen],
        rhythm,
    )
    return [_centred(shot, length) for shot, length in zip(chosen, durations)]


def _centred(shot: Shot, length: float) -> Segment:
    """Take the middle of a shot, so the cut avoids the transition frames."""
    start = shot.start + (shot.duration - length) / 2
    return Segment(start=start, duration=length)


def _fit_total(
    durations: list[float],
    ceilings: list[float],
    rhythm: RhythmSpec,
) -> list[float]:
    """Stretch or squeeze segment lengths until the total lands in range.

    Each segment stays within its own floor and ceiling, so a short shot is
    never asked to give more than it has.
    """
    total = sum(durations)

    if total > rhythm.max_total:
        durations = _redistribute(durations, rhythm.max_total, rhythm.min_seconds, shrink=True)
    elif total < rhythm.min_total:
        durations = _redistribute(durations, rhythm.min_total, ceilings, shrink=False)

    total = sum(durations)
    if not rhythm.min_total - 1e-6 <= total <= rhythm.max_total + 1e-6:
        raise NotEnoughFootage(
            f"cannot reach a {rhythm.min_total}-{rhythm.max_total}s total "
            f"from {len(durations)} segments (best was {total:.2f}s)"
        )
    return durations


def _redistribute(durations, target, bound, *, shrink):
    """Move every segment toward its bound in proportion to its slack."""
    bounds = bound if isinstance(bound, list) else [bound] * len(durations)
    slack = [
        (d - b) if shrink else (b - d)
        for d, b in zip(durations, bounds)
    ]
    available = sum(slack)
    needed = abs(sum(durations) - target)

    if available <= 0:
        return durations

    share = min(1.0, needed / available)
    return [
        d - slack[i] * share if shrink else d + slack[i] * share
        for i, d in enumerate(durations)
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_naive.py -v`
Expected: all passed (7 test functions, 60 cases after parametrisation)

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add cutlist/select/__init__.py cutlist/select/naive.py tests/test_naive.py
git commit -m "feat: naive segment selection with rhythm constraints"
```

---

### Task 10: CLI

**Files:**
- Create: `cutlist/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above
- Produces: `cutlist.cli.app` (a `typer.Typer`) with commands `probe`, `shots`, `draft`

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
import json

from typer.testing import CliRunner

from cutlist.cli import app
from cutlist.media.probe import probe

runner = CliRunner()


def test_probe_command_reports_dimensions(fixture_film):
    result = runner.invoke(app, ["probe", str(fixture_film)])
    assert result.exit_code == 0
    assert "320x240" in result.stdout


def test_shots_command_counts_shots(fixture_film):
    result = runner.invoke(app, ["shots", str(fixture_film)])
    assert result.exit_code == 0
    assert "6 shots" in result.stdout


def test_shots_command_can_emit_json(fixture_film):
    result = runner.invoke(app, ["shots", str(fixture_film), "--json"])
    assert result.exit_code == 0
    shots = json.loads(result.stdout)
    assert len(shots) == 6
    assert {"index", "start", "end"} <= shots[0].keys()


def test_draft_writes_playable_clips(fixture_film, tmp_path):
    result = runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", "presets/real_saturday.yaml",
        "--count", "2",
        "--root", str(tmp_path),
        "--seed", "0",
    ])
    assert result.exit_code == 0, result.stdout

    clips = sorted((tmp_path / "output" / fixture_film.stem / "real_saturday").glob("*.mp4"))
    assert len(clips) == 2
    for clip in clips:
        info = probe(clip)
        assert info.has_audio is False
        assert (info.width, info.height) == (854, 480)
        assert 9.0 <= info.duration <= 15.5


def test_draft_caption_override_is_accepted(fixture_film, tmp_path):
    result = runner.invoke(app, [
        "draft", str(fixture_film),
        "--preset", "presets/real_saturday.yaml",
        "--caption", "ДРУГОЙ ТЕКСТ",
        "--count", "1",
        "--root", str(tmp_path),
        "--seed", "0",
    ])
    assert result.exit_code == 0, result.stdout
    assert "ДРУГОЙ ТЕКСТ" in result.stdout


def test_missing_film_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["probe", str(tmp_path / "nope.mp4")])
    assert result.exit_code != 0
```

The fixture has 6 shots of 5s each, so a 9–15s draft of 4–6 segments fits comfortably.

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cutlist.cli'`

- [ ] **Step 3: Write `cutlist/cli.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -v`
Expected: 6 passed

- [ ] **Step 5: Run the whole suite**

Run: `.\.venv\Scripts\python.exe -m pytest -v`
Expected: everything green.

- [ ] **Step 6: Run it against a real film**

```
.\.venv\Scripts\cutlist.exe draft "input\your_film.mkv" --preset presets\real_saturday.yaml --count 5
```

Watch the clips in `output\`. They will be badly chosen — selection is random — but check that the caption is positioned and styled like `references/hollywood_reference.mp4`, that playback is smooth across the concat joins, and that Telegram accepts one as a GIF.

- [ ] **Step 7: Commit (checkpoint)**

```bash
git add cutlist/cli.py tests/test_cli.py
git commit -m "feat: probe, shots and draft commands"
```

---

## Done when

`cutlist draft` turns a feature film into watchable captioned clips of the right shape, and the whole suite passes. Selection quality is explicitly not a goal here — that's plan 2 (the CLIP index and real scoring) and plan 3 (contact sheets, the judge, beats and feedback).

Carry these observations into plan 2:
- how long `detect_shots` takes on a real film, since plan 2 caches it
- whether the caption needed its fractions adjusted away from the preset defaults
- whether any concat joins glitched, which would mean per-segment keyframe assumptions need revisiting
