# cutlist

Point it at a feature film and it produces short captioned clips: silent
854x480 MP4s, 9-15 seconds each, assembled from segments cut out of
different parts of the film, with a caption burned across the top. Built
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
cutlist probe <film>
cutlist shots <film> [--json]
cutlist draft <film> --preset <preset.yaml> [--count N] [--caption "..."] [--root DIR] [--seed N]
```

`probe` reports what ffprobe makes of the file: dimensions, fps, duration,
whether it has audio.

`shots` runs scene detection and reports the cuts found.

`draft` is the one that produces output. It detects shots, picks segments
according to the preset's rhythm rules, and renders `--count` clips (10 by
default) to `output/<film>/<preset>/`. Example:

```
cutlist draft "input/The Big Lebowski 1998.1080p.BluRay.x264.anoXmous_.mp4" \
  --preset presets/real_saturday.yaml \
  --count 5 \
  --caption "ЗАВТРА РИЛ СУББОТА"
```

`--seed` fixes the RNG for a reproducible draft. `--root` sets where
`input/`, `cache/`, `work/` and `output/` live (defaults to the current
directory).

## Presets

A preset is a YAML file with three blocks: `caption`, `rhythm`, `output`.
See `presets/real_saturday.yaml` for a working example.

```yaml
name: real_saturday
caption:
  text: "ЗАВТРА РИЛ СУББОТА"   # overridable with --caption
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

To add a preset, copy `real_saturday.yaml`, change the caption and rhythm
numbers, and pass its path to `--preset`. No code changes needed.

## Current state

Segment selection is currently random, subject only to the rhythm
constraints in the preset -- it does not look at the footage at all. This
is a deliberate walking skeleton: it proves out shot detection, captioning
and rendering end to end before content-aware scoring (saliency, diversity,
an agent doing the judging) is layered on top in a later phase.

There is deliberately no single `run` command. The design this is heading
toward involves an agent judging shortlisted footage between two separate
commands; a `run` command would hide that seam.
