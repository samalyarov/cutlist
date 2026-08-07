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


# segments 1..3 at 1.0-1.05s each can only total 1.00-1.05, 2.00-2.10 or
# 3.00-3.15 -- the gap in between must be rejected even though the global
# envelope (1 segment at 1.0s .. 3 segments at 1.05s) spans 1.0 to 3.15.
GAPPY = """
name: gappy
caption:
  text: "HELLO"
rhythm:
  segments: {min: 1, max: 3}
  seg_duration: {min: 1.0, target: 1.02, max: 1.05}
  total: {min: 1.5, max: 1.8}
output:
  width: 854
  height: 480
  fps: 25
  crf: 20
"""


def test_rejects_total_that_falls_in_a_gap_between_segment_counts(tmp_path):
    with pytest.raises(PresetError, match="total"):
        load_preset(write(tmp_path, GAPPY))


def test_accepts_total_reachable_only_at_one_segment_count(tmp_path):
    body = GAPPY.replace("total: {min: 1.5, max: 1.8}", "total: {min: 2.05, max: 2.08}")
    preset = load_preset(write(tmp_path, body))
    assert preset.rhythm.min_total == 2.05


def test_rejects_unknown_caption_key(tmp_path):
    body = VALID.replace("  outline_frac: 0.006\n", '  outline_frac: 0.006\n  colour: "red"\n')
    with pytest.raises(PresetError, match="colour"):
        load_preset(write(tmp_path, body))


def test_rejects_unknown_output_key(tmp_path):
    body = VALID.replace("  crf: 20\n", "  crf: 20\n  bitrate: 5000\n")
    with pytest.raises(PresetError, match="bitrate"):
        load_preset(write(tmp_path, body))


def test_rejects_unknown_rhythm_subkey(tmp_path):
    body = VALID.replace(
        "segments: {min: 4, max: 10}", "segments: {min: 4, max: 10, step: 1}"
    )
    with pytest.raises(PresetError, match="step"):
        load_preset(write(tmp_path, body))
