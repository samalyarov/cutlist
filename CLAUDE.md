# CLAUDE.md

Guidance for working in this repository. Read this before changing anything.

`README.md` is the user-facing manual — install, usage, examples. This file is
the developer-facing one: architecture, invariants, conventions, and the traps
that have already cost time here.

Current status, open items and next steps live in `docs/PROJECT-STATE.md`,
which is gitignored and local-only. Read it too when it is present.

---

## What this is

`cutlist` assembles short captioned clips from a long video. It detects shots,
picks segments according to a preset's rhythm rules, burns in a caption,
concatenates, and records everything it did in SQLite so a human can rate the
results and so any clip can be rebuilt from its record.

Python 3.12 only (`requires-python = ">=3.12,<3.13"`). CLI-only, no service.
ffmpeg and ffprobe do all the media work and must be on `PATH`. AGPL-3.0-only.

The premise is a walking skeleton that still holds: **prove the pipeline before
layering judgement on it.** Selection is still random within the preset's
rhythm. Nothing consumes the ratings yet — collecting them honestly is the
point of the current design, and spending them is v2.

---

## Verifying a change

```
FORCE_COLOR=1 .venv/Scripts/python.exe -m pytest    # matches CI's colour mode
.venv/Scripts/python.exe -m ruff check .
docker build -t cutlist .                           # matches the image job
```

CI (`.github/workflows/ci.yml`) runs two required jobs on every PR to `main`:

- **test** — ubuntu, Python 3.12, `apt install ffmpeg fonts-dejavu-core libgl1`,
  then `ruff check .` and `pytest -q`.
- **image** — `docker build`, then a smoke test that runs `cutlist demo
  --count 2` *inside* the container and asserts two MP4s exist. `--help` would
  only prove the entrypoint resolves; `demo` is the one command needing no
  input file, so it exercises ffmpeg, scenedetect, opencv, the font and the
  packaged `demo.yaml` for real.

Media tests are slow (the suite takes ~2 minutes) because they run real ffmpeg
against a real fixture video. That is deliberate — a mocked ffmpeg proves
nothing about a pipeline whose entire job is calling ffmpeg correctly.

---

## Layout

```
cutlist/
  cli.py              every command; argument parsing, reporting, the
                      `working()` progress spinner, and (still) the draft
                      pipeline itself
  paths.py            Workspace (input/cache/output/library/database), video_id,
                      resolve_within
  presets.py          Preset/CaptionSpec/RhythmSpec/OutputSpec, YAML loading,
                      validation, round-tripping from the stored JSON
  rebuild.py          rerender: re-cut a clip from its database record
  assemble.py         assemble: build a clip from hand-picked library ids
  library.py          extract: whole shots into library/, at source quality
  demo.py             a synthetic source so `cutlist demo` needs no input
  shell.py            run() -> stdout, ToolError on failure/timeout/not-found
  db/
    schema.py         DDL, MIGRATIONS, migrate(), connect()
    store.py          every SQL statement in the project
  media/
    probe.py          ffprobe -> VideoInfo
    shots.py          PySceneDetect -> [Shot], short-shot merging
    render.py         encode_segment, concat, render_clip, DestinationBusy
    caption.py        font resolution and caption PNG rendering
    thumbs.py         a JPEG frame at a timecode
    sources.py        find_source: locate a video by content hash or by name
  select/naive.py     draft_picks: random segments that satisfy the rhythm
  feedback/rate.py    parse the --segments "1:good,3:veto" string
  review/
    server.py         stdlib ThreadingHTTPServer, the review API
    page.html         the whole review UI, one file, no build step
tests/                pytest, mirrors the modules
presets/              only sample_preset.yaml is tracked
```

Everything else at the root is workspace, not source: `input/`, `cache/`,
`output/`, `library/`, `cutlist.sqlite`. All gitignored.

---

## The pipeline

```
video ─> probe ──> dimensions, fps, duration
      └> detect ─> shots (PySceneDetect content detector, short shots merged)
                     │
                     ├──> extract ──> library/   whole shots, source quality,
                     │                           no caption, audio kept
                     ▼
                  select ──> segments obeying the preset's rhythm
                     │       (or assemble, from library ids you name)
                     ▼
                  render ──> one encode pass per segment with the caption
                     │       composited in, then a stream-copy concat
                     ▼
                   clip ──> output/<video>/<preset>/<run-id>/NN.mp4
                     │
                     ▼
              cutlist.sqlite
```

One encode pass per segment is why the caption is composited during
`encode_segment` rather than afterwards: every segment then starts on a
keyframe, which is what makes the later `concat -c copy` safe.

---

## The data model

`cutlist.sqlite` at the workspace root **is the only artifact that cannot be
regenerated.** Clips, caches, thumbnails on disk and shot detection are all
rebuildable; human judgement is not. Every design decision here resolves in
favour of that file being independently sufficient.

| table | holds |
|---|---|
| `video` | one row per source: content id, display name, duration, fps, size |
| `run` | seed, preset name/sha256/fully resolved JSON, caption, `kind` (`draft` or `assemble`), cutlist version |
| `run_video` | which sources a run was *aimed at* — intent, not outcome |
| `clip` | one rendered file: `run_id`, `ordinal`, workspace-relative `path`, duration |
| `segment` | per clip and position: source hash, its own timecodes **and its parent shot's** |
| `segment_thumbnail` | one JPEG per segment, captured at render time |
| `clip_rating` | `fire` / `ok` / `no`, append-only |
| `shot_rating` | `good` / `bad` / `veto`, keyed on video hash plus timecodes |
| `library_clip` | extracted whole shots; `UNIQUE (video_hash, start_s, end_s)` |
| `clip_video` (view) | a clip's composition, derived from its segments, never cached |

A **clip verdict dies with its clip** (it describes one specific assembly); a
**shot mark outlives any clip** that happened to contain it, so it carries its
own timecodes and merely loses the `segment_id` pointer (`ON DELETE SET NULL`).
That asymmetry is why `shot_rating` duplicates timecodes.

`connect()` sets `journal_mode = WAL` (both `review` and `rate` can write) and
`foreign_keys = ON` (SQLite defaults it off, and the `ON DELETE` rules are
load-bearing).

### Migration rules

- `MIGRATIONS = [_V1, _V2, _V3]`; `SCHEMA_VERSION = len(MIGRATIONS)` is derived,
  never hand-written.
- Migrations are **append-only and never edited.** A shipped migration has
  already run against a database somewhere.
- Each migration's DDL **and** its `user_version` stamp are wrapped in one
  transaction inside the script text. Do not undo this — see the traps below.
- `PRAGMA` takes no bound parameters; the version comes from `enumerate` over a
  module-level list, so it is always an int.

### Identity

- `video_id()` hashes size plus the first and last megabyte — stable across
  renames and moves, cheap on gigabyte files.
- `store.ms()` rounds timecodes to 3 decimals. `library_clip`'s UNIQUE
  constraint compares them for identity, and float equality is not identity.
  **Round on write and on lookup, always both.**

---

## Commands

```
cutlist demo [--count N] [--root DIR] [--seed N]
cutlist probe <video>
cutlist shots <video> [--json]
cutlist draft <video> --preset <p.yaml> [--count N] [--caption "..."] [--seed N] [--keep-shots] [--root DIR]
cutlist extract <video> [--crf N] [--root DIR]
cutlist library [--video HASH] [--json]
cutlist assemble <ids> --preset <p.yaml> [--caption "..."] [--root DIR]
cutlist review [--video HASH] [--preset NAME] [--port N] [--host ADDR] [--all] [--no-open] [--root DIR]
cutlist rate <clip-path> <fire|ok|no> [--segments "1:good,3:veto"]
cutlist rerender <clip-path> [--root DIR]
cutlist ratings [--json]
cutlist fonts [--search TEXT]
```

`assemble` takes ids as a comma list with ranges: `"1,4,7-9"`. It ignores the
preset's `rhythm` — those clips were chosen deliberately.

`draft` writes to `output/<video>/<preset>/<run-id>/`. The run id is in the path
so re-drafting the same video and preset never overwrites an earlier run's files
while its `clip` rows still claim those paths.

**There is deliberately no single `run` command.** The design has always had a
judge sitting between two commands, and one command would hide that seam.

### Error boundary

`cli.HANDLED_ERRORS` is a deliberate **allowlist**, and `@handle_errors`
decorates every command. Anything in the tuple prints one `error: ...` line on
stderr and exits 1; anything else keeps its traceback, because it is a bug and
should look like one. Do not widen it to bare `ValueError`/`OSError` — that
would swallow `concat`'s empty-input check and `probe.py`'s unguarded ffprobe
parsing. To make a new failure mode presentable, raise a narrow named exception
and add that class.

---

## Review server endpoints

`cutlist review` serves a stdlib `ThreadingHTTPServer` bound to
`127.0.0.1:8731` by default. It is **unauthenticated by design** and expects to
be loopback-only; `--host 0.0.0.0` (needed in a container) exposes it to the
network and says so in its own help text. Each request gets its own thread and
therefore its own SQLite connection — `sqlite3` objects are not shareable
across threads.

| method | path | returns |
|---|---|---|
| GET | `/` | `review/page.html`, the whole UI |
| GET | `/api/clips` | clips matching the server's `--video` / `--preset` / unrated filters, each with a computed `available` |
| GET | `/api/clip/<id>` | one clip with its segments, verdict and marks; 404 if unknown |
| GET | `/media/clip/<id>` | the MP4, with `Range` support (`Accept-Ranges`, 206, 416); 404 if the row or the file is missing |
| GET | `/media/thumb/<segment_id>` | the segment's JPEG, generated from the source if the row predates thumbnail capture |
| POST | `/api/ratings` | `{"ok": true}` |

Anything else is a 404. `POST /api/ratings` body:

```json
{"clip_id": 12, "verdict": "fire", "marks": [{"segment_id": 34, "mark": "good"}]}
```

`verdict` and `marks` are both optional, but **everything is validated before
anything is written**, so a bad mark never leaves a verdict recorded without it
and no earlier mark is committed while a later one fails. A verdict is refused
when the clip file is missing — you cannot judge footage you cannot watch. Only
`store.RatingError` and `store.RatingNotFound` become 400s; catching bare
`ValueError`/`LookupError` would dress a genuine bug up as a client error.

The thumbnail fallback is **deliberately asymmetric**: a frame from a
display-name-only source match is served (it keeps the segment strip legible)
but never written to the database (unprovenanced bytes must not become the
permanent record of what was judged).

---

## Presets

YAML, three blocks — `caption`, `rhythm`, `output`. `presets/sample_preset.yaml`
is the only tracked one; the rest of `presets/` is gitignored because presets
are personal.

```yaml
name: sample_preset
caption:  {text, position: top_center|bottom_center, size_frac, fill, outline, outline_frac, font?}
rhythm:   {segments: {min,max}, seg_duration: {min,target,max}, total: {min,max}}
output:   {width, height, fps, crf}
```

`rhythm`'s three constraints must be mutually reachable; a preset asking for a
total no segment count can hit is rejected at load time, with the achievable
range in the message. Unknown keys are rejected too — a typo'd preset key that
is silently ignored produces a clip that is quietly wrong.

The **fully resolved** preset is stored as JSON on the run, and
`preset_from_dict` round-trips it. That is what lets `rerender` reproduce a clip
after the preset file on disk has changed or been deleted.

`font` is a family name or a `.ttf`/`.otf`/`.ttc` path; `cutlist fonts` lists
what is resolvable. Linux falls back to DejaVuSans-Bold (installed in CI and in
the image, and it covers Cyrillic).

---

## Invariants — do not break these without a reason worth writing down

- **The database is the master; the files are a cache.** Deleting `output/`
  costs nothing, because `rerender` rebuilds from the record.
- **`concat` is the only writer of a clip's final path**, always by `os.replace`
  of a fully written staging file that is a *sibling* of `dest` (atomicity is
  per-filesystem, and scratch lives under `cache/`). `rerender` writes over a
  clip that may already carry a verdict; a failed join must not take it.
- **Never delete rows as cleanup.** `clip_rating` cascades from `clip`.
- **Availability is computed, never stored.** A recorded "this file is gone"
  flag can only be a stale claim about a filesystem that changed without asking.
- **A rebuild requires a content-hash match on every source.** A file with the
  right name and different bytes is refused, not rendered — otherwise footage
  nobody watched ends up under a verdict somebody gave.
- **Every source is resolved before anything is encoded**, so a clip with one
  missing video fails fast and without touching `dest`.
- **The library stores whole shots, not trimmed picks** — a shot is the same
  shot whichever run found it, so ids stay durable.
- **Library clips carry no caption and no letterbox, and keep audio.** They are
  masters for reuse elsewhere.
- **Assembled clips record the original source and timecodes**, not the library
  file — which is exactly why `rerender` works on them.
- **`resolve_within` guards every path that came out of the database.** Nothing
  enforces that a writer put a relative path in `clip.path`.
- **The review page stays achromatic and keyboard-first**, colour spent only on
  the three verdict and three mark signals. (A GoodUI review concluded its
  patterns mostly do not transfer: they optimise a stranger toward a
  business-preferred outcome, and a rating tool has no preferred outcome, only
  an honest one.)

---

## Traps this codebase has already sprung

Each cost real time. They are written down so they cost it once.

**Rich splits option names across ANSI codes.** With colour on, `--preset` is
emitted as `\x1b[1;36m-\x1b[0m\x1b[1;36m-preset\x1b[0m` — never contiguous. A
test asserting `"--preset" in result.stdout` passes locally (colour off) and
fails in CI (colour on). Strip styling before matching; `tests/test_cli.py` has
a `plain()` helper and a test that forces colour on.

**FORCE_COLOR makes rich claim a terminal that is not there.** `Console.is_terminal`
returns true for *any* value of `FORCE_COLOR`, and CI and the documented test
command both set `FORCE_COLOR=1`. A progress spinner gated on it would start a
refresh thread writing ANSI frames into pytest's captured stderr in every test
that runs a command. `cli.working()` gates on `sys.stderr.isatty()` instead --
plus `is_dumb_terminal`, a tty that cannot move the cursor, where rich draws
nothing at all rather than a still frame.

**`executescript()` auto-commits each statement.** A migration without an
explicit `BEGIN` applies statement-by-statement while `user_version` is stamped
only at the end. A process killed mid-migration left a half-renamed schema that
every later `connect()` replayed and died on — ratings intact on disk,
permanently unreachable.

**A function's contract belongs to its first caller until you check.** Three
separate defects shared this shape: `render_clip` deleted `dest` on failure
(safe for `draft`, destructive for `rerender`); `find_source` returned a
display-name match indistinguishably from a content-hash match (fine for a
thumbnail, wrong for rebuilding rated footage); `library_path` keyed on filename
stem (fine for one video, silently destroyed masters when two sources shared a
name). **When a task adds a second caller to an existing function, ask what the
first caller's assumptions let that function get away with.**

**Tests can pass while proving nothing.** Repeatedly. A durability test deleted
a *copy* while the real file survived. A dedup test wrote the exact value first,
so unrounded-write and unrounded-lookup mutations both passed. Fourteen
`assemble` tests verified the database and not the video — three mutations
survived all of them, including *reversing part order*, because the `segment`
rows are built from the same id list and cannot disagree with it whatever the
encoder did. **Mutate the code and watch the test fail** before believing it.

**A self-review cannot find the author's blind spots.** v1.2 Tasks 5-7 were
implemented and self-reviewed by one agent; the independent pass afterwards
found eight issues, three serious, two sitting exactly where the plan text
asserted the property that the code broke. Do not merge a slice without an
independent review of it.

**Windows ffmpeg is more permissive than Linux ffmpeg.** A fixture passing a
global `-pix_fmt yuv420p` also hit an MJPEG cover-art stream, which Linux
ffmpeg 6.1 rejects and Windows ffmpeg 8.1 accepts. CI was red on its first run.

**Windows will not replace a file another process holds open.** `os.replace`
raises `PermissionError` (WinError 5), which is an `OSError` but not a
`FileNotFoundError`. `concat` catches it and raises `DestinationBusy`.

**`scenedetect` drags in non-headless `opencv-python`** alongside the pinned
headless build; the full one wins the `cv2` namespace and needs `libGL`. CI and
the Dockerfile install `libgl1` explicitly. The obvious fix,
`scenedetect[opencv-headless]`, is a **silent no-op** — that extra was removed
in scenedetect 0.7.0, so pip warns and installs the full build anyway. Decision:
keep the duplicate, accept ~8% image size. Settled; do not re-open.

**Verify packaging from a pristine export.** A stale `cutlist.egg-info/` made a
wheel appear to contain `page.html` when it did not. Use `git archive`.
`page.html` and `demo.yaml` are declared `package-data`; a wheel missing either
serves `GET /` into a `FileNotFoundError` or breaks `demo` in the container.

---

## Conventions

**Branching.** `main` is protected: PRs required (0 approvals), `test` and
`image` checks required, force pushes and deletions blocked. Branch, open a PR,
merge only a whole feature, and only once CI is verified green
(`gh run watch <id> --exit-status`). `enforce_admins` is off, so an emergency
valve exists — use the PR flow anyway.

**Commits.** Imperative mood, `type: summary` (`fix:`, `test:`, `docs:`,
`build:`). Explain *why* in the body when it is not obvious. **No AI
attribution anywhere** — no trailers, no "Generated with", no self-mentions in
commits, code or docs.

**Comments explain why, not what.** This codebase's comments are dense and
argumentative on purpose: most of them record a decision and the failure that
forced it. Match that density. A comment restating the line below it is noise;
a comment naming the alternative that was rejected, and why, is the point.

**Lint, not format.** `ruff check` with `E`, `F`, `I` at line-length 100.
`ruff format` is deliberately **not** adopted — the code is hand-wrapped so that
related arguments group on a line, and a formatter flattens that back out.

**Tests.** pytest, no mocking of ffmpeg. Every test names the behaviour it
protects, and its docstring says what breaks if it regresses. New non-trivial
logic leaves behind a test that fails when the logic is mutated.
