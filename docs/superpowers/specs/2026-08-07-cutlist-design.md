# cutlist — short clip generator

**Date:** 2026-08-07
**Status:** approved, pending implementation plan

## What it does

Point it at a feature film and it produces around ten candidate short clips: silent
854×480 MP4s, 9–15 seconds each, assembled from segments cut out of different parts of
the film, with a caption burned across the top.

The caption and the montage style come from a preset. Adding a new kind of clip means
writing a preset, not writing code. The caption text lives in the preset and can also
be overridden per run from the command line, so one preset covers a whole family of
captions.

Output targets Telegram, where "GIFs" are really silent MP4s, so nothing extra is
needed at the end.

## What the reference clips told us

Two reference clips were measured with ffprobe and ffmpeg scene detection
(`select='gt(scene,0.12)'`) rather than judged by eye. Both are in `references/`.

The Once Upon a Time in Hollywood clip is 854×480, 25fps, 10.48s, silent. Five shots
of 1.7–2.6s, mean 2.1s. It follows one character through a day-into-night arc: on the
street in daylight, driving in daylight, driving at night through neon, then at home
with a beer.

The Twin Peaks clip is 320×262, 11.19s, silent. About 31 cuts, mean 0.36s, including
one 2.5s hold and a six-cut burst spaced 0.06s apart. Content is associative and
ominous — the Black Lodge, a body on a table, a woman screaming in blue light, the
WELCOME TO TWIN PEAKS sign.

### Why mood matching is the wrong objective

Almost nothing in the Twin Peaks clip would survive a filter looking for carefree,
happy, party-ish scenes. The humour comes from incongruity: an everyday caption
stapled over a film's most recognisable imagery. It lands *because* the mood is wrong.

So the pipeline does not try to match a mood. What actually holds across both clips is
narrower: identical caption styling (white, bold, all-caps, top-centre, thin dark
outline), silence, 10–11s total, segments pulled from across the whole film and
concatenated, and shots that are visually distinctive for that particular film.

What varies is rhythm, whether there's a narrative arc, and mood. The Twin Peaks pace
was judged too fast to be worth modelling, so v1 implements one rhythm regime close to
the Hollywood clip: 4–10 segments, 1.2–2.8s each, 9–15s total. No bursts, no holds.

## Key decisions

### Narrow the field in stages

```
~2000 shots  ->  ~60-80 scored candidates  ->  10 assemblies  ->  10 clips
```

Each step costs far more per item than the one before it, so the cheap steps do the
bulk of the filtering. Handing a whole film to a multimodal API is impractical anyway:
sampled at ~1fps and ~258 tokens per frame (~66 at low media resolution), a two-hour
film runs to roughly 1.8M tokens, or ~475K low-res — at or past context limits, and
paid again every time a prompt changes.

### Index once per film, query many times per preset

Stage 1 knows nothing about presets and its output is cached permanently. A fifth
preset never re-touches the video file, so iterating on a preset takes seconds and
costs nothing.

A film is identified by `blake2(filesize ‖ first 1MB ‖ last 1MB)`. Renaming or moving
it keeps the index, and a two-hour re-index never happens by accident.

### The judge is Claude Code, not an API

The expensive visual judgement is done by the agent already running on the user's
subscription, reading contact-sheet PNGs. No API key, no per-token cost.

The consequence is that it cannot look at two thousand shots, which is what makes the
cheap local prefilter essential rather than merely nice. The judge is a swappable
stage: it reads `pool.json` plus sheets and writes `scores.json`. Swapping in Gemini
or a local VLM later touches nothing else.

### Saliency instead of mood

Saliency is the cosine distance between a shot's embedding and the mean embedding of
the whole film. The Black Lodge is unusual for Twin Peaks; neon night driving is
unusual in a mostly-daylight film. That unusualness is most of what makes an image
recognisable, it needs no per-film tuning, and it comes free from an index we're
building anyway.

Combine that with a diversity term — maximise pairwise embedding distance among the
chosen segments — and most of the selection problem is covered without modelling mood
at all. Mood survives as an optional per-preset bias. The hard rejections are
technical: too dark, credits, on-screen text, blank frames.

### Sample rather than take the top N

Score the shots, keep everything above a low bar, then draw ten assemblies with
weighted randomness. This gives ten varied clips instead of ten near-identical ones,
returns something new on a re-run, and tolerates a mediocre scorer — which ours will
be, at least at first. Being lenient is built into the algorithm instead of living in
a threshold that needs constant fiddling.

### Two commands, with the agent in between

The pipeline can't be one call, because the judge is an agent. `cutlist shortlist`
writes a workdir and stops; `cutlist render` refuses to start until a valid
`scores.json` is there. A `/cutlist` skill chains the two and does the looking in
between. There's deliberately no `run` command that hides the seam — the agent is the
only thing that can cross it, and pretending otherwise just makes failures confusing.

## Architecture

```
                 STAGE 1: INDEX  (once per film, preset-agnostic, cached)
film.mkv  ---->  ffprobe -> PySceneDetect -> 3 keyframes per shot
                 -> frame stats (luma, contrast, motion, faces)
                 -> subtitles -> dialogue density
                 -> CLIP embeddings
                 => cache/<hash>/index.sqlite + embeddings.npy      ~2000 shots
                            |
                 STAGE 2: SHORTLIST  (per preset, local, seconds, free)
                 technical gates, then
                 saliency + clip similarity + soft beat bias - penalties
                 => work/.../pool.json + sheets/*.png               ~60-80 shots
                            |
                 STAGE 3: JUDGE  (Claude Code)
                 agent reads the sheets against the preset's rubric
                 => work/.../scores.json                            {shot_id, score, reason}
                            |
                 STAGE 4: ASSEMBLE + RENDER  (local, deterministic)
                 sample 10 assemblies, trim shots into segments
                 Pillow caption PNG -> per-segment encode -> concat
                 => output/<film>/<preset>/01.mp4 ...               10 clips
                            |
                 STAGE 5: FEEDBACK
                 cutlist rate 03 fire -> ratings.jsonl
                 (consumed by scoring in v2)
```

## Vocabulary

A **shot** is one uninterrupted take between two cuts, as found by PySceneDetect. It's
the atom of the index; a two-hour film has 1500–2500 of them.

A **segment** is a 1.2–2.8s trim taken from a shot. Shots are usually longer than we
want, so the segment is the good part of one.

A **beat** is an optional named slot in a preset's story template.

An **assembly** is an ordered set of segments concatenated into one output clip.

## Presets

Three independent blocks: caption, rhythm, selection. The shipped example reproduces
the Hollywood reference.

```yaml
name: real_saturday
caption:
  text: "ЗАВТРА РИЛ СУББОТА"   # overridable with --caption
  position: top_center
  font: sans_bold               # must cover Cyrillic
  size_frac: 0.065              # fraction of output height
  fill: "#FFFFFF"
  outline: "#000000"
  outline_frac: 0.006
rhythm:
  segments: {min: 4, max: 10}
  seg_duration: {min: 1.2, target: 2.0, max: 2.8}
  total: {min: 9, max: 15}
selection:
  mode: beats                   # beats | iconic
  beats:
    - out_in_the_world_day
    - going_somewhere
    - night_city
    - arrival_reward
  weights:
    saliency: 0.35
    beat_match: 0.35
    diversity: 0.30
  prefer_same_character: 0.8    # 0.0 disables
  clip_prompts:
    positive:
      - "a person walking confidently outdoors in daylight"
      - "driving a car, view from inside the car"
      - "city street at night lit by neon signs"
      - "someone relaxing with a drink, smoking"
    negative:
      - "opening credits, title card, text on a black screen"
      - "a dark frame where nothing is visible"
      - "two people sitting and talking to each other"
  penalties:
    dark: 0.4
    dialogue: 0.3
    onscreen_text: 0.5
  dedupe_similarity: 0.93       # pooled shots closer than this collapse into one
output:
  width: 854
  height: 480
  fps: 25
  audio: none
  crf: 20
```

In `beats` mode each beat is filled once, in the order declared. When the sampled
segment count exceeds the number of beats — four beats but up to ten segments — the
surplus is drawn from the general pool and placed next to whichever beat it scores
highest against, so the arc stays monotonic.

In `iconic` mode beats are ignored: rank flat by saliency and diversity, then order
the winners by their timecode in the film.

Beats are a preference, not a requirement. If nothing clears the floor for a beat, the
slot is filled from the general pool rather than failing the whole assembly.

## Scoring

Per shot, after the technical gates:

```
score = w_sal  * saliency(shot)
      + w_beat * max_beat_similarity(shot, preset)
      + w_div  * (applied at assembly time, see below)
      - p_dark * darkness_penalty(shot)
      - p_talk * dialogue_density(shot)      # zero when no subtitles exist
      - p_text * onscreen_text_penalty(shot)
```

Weights and penalty coefficients come from the preset's `selection` block.

The gates are deliberately few and forgiving: mean luma outside `[8, 247]`; shot
shorter than `seg_duration.min`; inside the first 2% or last 5% of runtime, which
catches logos and credits; or cosine similarity above `dedupe_similarity` to something
already pooled.

Diversity applies during assembly sampling rather than per shot. A candidate's weight
drops in proportion to its highest embedding similarity against segments already
picked for that assembly.

`prefer_same_character` biases sampling toward segments whose face cluster matches the
ones already chosen. Face clustering runs during indexing regardless — it's cheap on
the target GPU — and simply goes unused when the weight is zero.

## Rendering

The caption doesn't change during a clip, so it gets burned in while each segment is
encoded, and the segments are then joined without re-encoding. That's one encode pass
total, and because each segment was encoded separately it starts on a keyframe, which
makes the concat safe.

```
per segment:
  ffmpeg -ss <start> -i <film> -i caption.png -t <dur> -an \
    -filter_complex "[0:v]scale=854:480:force_original_aspect_ratio=decrease,
                     pad=854:480:-1:-1,fps=25,setsar=1[v];[v][1:v]overlay=0:0" \
    -c:v libx264 -crf 20 -pix_fmt yuv420p  seg_NN.mp4

then:
  ffmpeg -f concat -safe 0 -i segments.txt -c copy final.mp4
```

`-ss` goes before `-i` for fast seek, which matters on a two-hour source.

The caption is drawn with Pillow, not ffmpeg's `drawtext`. Confirmed on this machine:
ffmpeg reports `Fontconfig error: Cannot load default config file` on Windows and falls
back unpredictably. Pillow gives exact stroke width and placement, handles Cyrillic
reliably, and avoids filtergraph escaping entirely.

## Layout on disk

Split by what invalidates it:

```
cache/<film_hash>/              index.sqlite, embeddings.npy, thumbs/, meta.json
work/<film>__<preset>__<run>/   pool.json, sheets/, scores.json, plan.json
output/<film>/<preset>/         01_score94.mp4 ..., candidates.json
input/                          source films
references/                     the two reference clips
```

## Modules

```
cutlist/
  cli.py            index, shortlist, render, rate
  presets.py        Preset dataclass, YAML loading and validation
  paths.py          directory layout, film hashing
  media/
    probe.py        ffprobe -> VideoInfo
    shots.py        PySceneDetect -> Shot[]
    frames.py       keyframe extraction, 3 per shot at 15/50/85%
    subs.py         embedded or sidecar subtitles -> dialogue density
    caption.py      Pillow -> transparent PNG
    render.py       per-segment encode, then concat
  analysis/
    stats.py        luma, contrast, motion, colourfulness, faces
    embed.py        open_clip encoding
    index.py        MovieIndex build/load/save
  select/
    score.py        gates and per-shot scoring
    assemble.py     weighted sampling of assemblies
    sheets.py       contact sheet generation
    judge.py        scores.json validation and join
  feedback/
    rate.py         ratings.jsonl
presets/
  real_saturday.yaml
.claude/skills/
  cutlist/          run the pipeline and do the judging
  cutlist-preset/   write a preset YAML from a description
tests/
```

## Commands

```
cutlist index     <film>
cutlist shortlist <film> --preset real_saturday [--caption "..."]
cutlist render    <workdir> [--count 10]
cutlist rate      <clip> {fire|good|bad}
```

## When things go wrong

Every stage is idempotent — it checks for its own output and skips unless `--force` —
so a failed run resumes from the workdir.

An ffprobe or ffmpeg failure surfaces the command that was run plus the tail of
stderr. A missing subtitle track disables the dialogue signal with one warning rather
than an error, since plenty of rips have no subs. Missing CUDA falls back to CPU with
a warning. A malformed `scores.json` names the record that broke and leaves the
workdir intact. A preset asking for more segments than the pool can supply renders a
shorter assembly and says so.

## Testing

The judge is stubbed with a fixture `scores.json`; no agent runs inside the test loop.

Shot detection is checked against a synthetic 60-second fixture built by ffmpeg out of
solid-colour scenes of known length, which gives exact ground-truth cut positions.

Assembly gets property tests: every sampled assembly respects segment count,
per-segment duration and total duration, and every segment sits strictly inside a real
shot.

Hashing is checked for stability across rename and move, and for distinguishing
different films.

Caption tests assert the PNG matches the output frame size, has an alpha channel, and
has non-transparent pixels in the top band. Render tests assert zero audio streams,
correct dimensions and fps, and total duration within tolerance of the plan.

Preset validation is tested against malformed and self-contradictory presets — for
instance a `total.min` that `segments.max * seg_duration.max` can never reach.

## Environment

Python 3.12 in `.venv`, pinned for wheel availability rather than preference; 3.14 is
the system default but PyTorch and PySceneDetect support for it is unreliable.

PyTorch must come from the `cu128` index. The target GPU is an RTX 5080 (Blackwell,
sm_120) and default wheels won't run on it.

ffmpeg and ffprobe are already on PATH.

Dependencies: pyscenedetect, opencv-python, open_clip_torch, torch, pillow, numpy,
pyyaml, typer, pysrt, insightface, pytest.

## Out of scope for v1

Audio in the output. Vertical or multi-format rendering. A web review UI — rating goes
through `cutlist rate`. Taste learning: stage 5 only collects ratings in v1, consuming
them is v2. The fast-montage rhythm from the Twin Peaks clip. Any agent framework —
the workflow is fixed and the agent occupies exactly one stage.

## Later

Feed `ratings.jsonl` into scoring: `score += a*sim(liked) - b*sim(disliked)`.

Alternative judges, so the tool can run end to end without an agent. The stage already
has a fixed contract — read `pool.json` and the sheets, write `scores.json` — so this
is a new implementation behind `--judge`, not a refactor:

- a local VLM such as Qwen2.5-VL through Ollama, unattended and offline
- a hosted API (Gemini, Claude) for people who'd rather pay per token
- Claude Code reranking the top twenty after a local first pass

That's also the point where a `cutlist run` command starts making sense, since a
non-agent judge can be called inline.

A Telegram bot front end. Vertical rendering and per-platform variants.
