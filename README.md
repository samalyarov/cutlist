# cutlist

Point it at a feature video and it produces short captioned clips: silent
854x480 MP4s, 9-15 seconds each, assembled from segments cut out of
different parts of the video, with a caption burned into the video. Originally built
for Telegram, where "GIFs" are really silent MP4s.

## Install

Requires Python 3.12 and ffmpeg/ffprobe on PATH.

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

(`source .venv/bin/activate` instead of the `.venv\Scripts\...` prefix on
Linux/macOS.) Once installed, `cutlist` is on the venv's PATH as its own
command. Run the tests with:

```
.venv\Scripts\python.exe -m pytest
```

## Commands

```
cutlist probe <video>
cutlist shots <video> [--json]
cutlist draft <video> --preset <preset.yaml> [--count N] [--caption "..."] [--root DIR] [--seed N]
cutlist review [--video HASH] [--preset NAME] [--port N] [--all]
cutlist rate <clip-path> <fire|ok|no> [--segments "1:good,3:veto"]
cutlist ratings [--json]
```

`probe` reports what ffprobe makes of the file: dimensions, fps, duration,
whether it has audio.

`shots` runs scene detection and reports the cuts found.

`draft` is the one that produces output. It detects shots, picks segments
according to the preset's rhythm rules, and renders `--count` clips (10 by
default) to `output/<video>/<preset>/<run-id>/`. Each draft gets its own
run directory, so re-drafting the same video and preset never overwrites an
earlier run's clips -- the ratings recorded against them stay attached to
the footage they were made about. Example:

```
cutlist draft "input/my-video.mp4" \
  --preset presets/sample_preset.yaml \
  --count 5 \
  --caption "SAMPLE CAPTION"
```

`--seed` fixes the RNG for a reproducible draft. `--root` sets where
`input/`, `cache/`, `work/` and `output/` live (defaults to the current
directory).

## Presets

A preset is a YAML file with three blocks: `caption`, `rhythm`, `output`.
See `presets/sample_preset.yaml` for a working example.

```yaml
name: sample_preset
caption:
  text: "SAMPLE CAPTION"        # overridable with --caption
  position: top_center          # top_center | bottom_center
  size_frac: 0.125               # fraction of output height
  fill: "#FFFFFF"
  outline: "#000000"
  outline_frac: 0.009
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

`rhythm` controls how many segments a clip has, how long each one is, and
the total clip duration. All three must be mutually reachable -- a preset
asking for a total no segment count can actually hit is rejected at load
time with an explanation of what totals are achievable.

To add a preset, copy `sample_preset.yaml`, change the caption and rhythm
numbers, and pass its path to `--preset`. No code changes needed.

Only the sample preset is tracked; `presets/` is otherwise ignored, so your own
presets stay yours.

## Rating

`draft` records what each clip was made of into `cutlist.sqlite` at the
workspace root: the run's seed and resolved preset, every clip, and every
segment with both its own timecodes and those of the shot it came from.

`cutlist review` serves a local page for watching a batch and rating it --
`f`/`o`/`n` for the clip verdict, `1`-`9` then `g`/`b`/`v` to mark individual
segments, `z` to undo, `?` for the full list. It opens a browser unless you
pass `--no-open`, and refuses a port that is already taken. `cutlist rate`
does the same from the terminal.

Nothing consumes the ratings yet. This release collects them; scoring uses
them later.

## Current state

Segment selection is currently random, subject only to the rhythm
constraints in the preset -- it does not look at the footage at all. This
is a deliberate walking skeleton: it proves out shot detection, captioning
and rendering end to end before content-aware scoring (saliency, diversity,
an agent doing the judging) is layered on top in a later phase.

There is deliberately no single `run` command. The design this is heading
toward involves an agent judging shortlisted footage between two separate
commands; a `run` command would hide that seam.
