# cutlist v1.1: durability, packaging and legibility

**Goal:** make the ratings store survive the deletion of the media it describes,
make the tool runnable by someone who has neither a source video nor a Python
environment, and make the code read the way it should before anyone is invited
to read it.

**Non-goal:** anything that consumes ratings. Selection stays random. Scoring,
embeddings and the taste model remain deferred to v2, and nothing here should be
designed around guesses about them.

## Why now

The provenance store landed and works: 474 tests, every clip decomposable into
segments with both its own timecodes and its parent shot's. What it does not yet
survive is the thing it was built for. The owner deletes rendered clips over
time and will eventually delete source videos too. Three consequences follow,
and one of them is a race:

- A clip whose file is gone is still offered for rating. The page renders a dead
  `<video>` element and accepts a verdict anyway. A verdict recorded without
  watching is a corrupt row in the only table that cannot be regenerated.
- Nothing can rebuild a deleted clip, so deletion is one-way in practice even
  though the database holds everything needed to re-cut it.
- Segment thumbnails are generated on demand from the source video and cached
  under `cache/`. When a source is deleted, every segment strip that referenced
  it goes blank permanently. No later change can recover it — the pixels are
  gone. Capture has to happen before deletion, which means it has to happen now.

The rest of the release is presentation: the code says `film` while the project
says `video`, the tool cannot be run without a source video, and the repository
carries none of the signals a reader looks for first.

## Principles

- **The database is the master; files are a cache.** Every design choice here
  resolves in favour of the `.sqlite` file being independently sufficient.
- **Availability is observed, never asserted.** A stored "this file is gone"
  flag can only be a stale claim about a filesystem that changed without asking.
- **Never delete rows.** Rows are bytes; a thousand clips is a couple hundred
  kilobytes. `clip_rating` cascades from `clip`, so any row-deleting command
  would destroy verdicts. Availability is tracked; history is not pruned.
- **Do not make one judgement easier to reach than another.** The review page
  collects opinions, and any affordance that lowers the cost of one verdict
  relative to its neighbours biases the data it exists to gather.

---

## Part 1 — Rename `film` to `video`

The public vocabulary is "video" — not every source is a film, and the narrower
word invites assumptions about provenance that the project does not want to
invite. The code has not followed. Roughly 130 identifiers in the package and
250 references in tests still say `film`, including four schema objects.

### Surface

**Python identifiers:**

| Current | New |
| --- | --- |
| `paths.film_id` | `paths.video_id` |
| `cli.probe_film` (import alias) | `cli.probe_video` |
| `store.record_film` | `store.record_video` |
| `store.film_display_name` | `store.video_display_name` |
| `store.SegmentRecord.film_hash` | `store.SegmentRecord.video_hash` |
| `start_run(film_hashes=...)` | `start_run(video_hashes=...)` |
| `clips_for_review(film=...)` | `clips_for_review(video=...)` |
| `ReviewHandler._source_for(film_hash)` | `_source_for(video_hash)` |
| CLI argument `film: Path` | `video: Path` |
| test fixtures `fixture_film`, `cutfree_film` | `fixture_video`, `cutfree_video` |

**CLI surface:** `review --film HASH` becomes `review --video HASH`. This is a
breaking change to a documented option, which is acceptable at 0.1.0 with a
single user. No alias is kept — a deprecated alias on a tool with one user is
maintenance for nobody.

**Schema:**

| Current | New |
| --- | --- |
| table `film` | table `video` |
| table `run_film` | table `run_video` |
| view `clip_film` | view `clip_video` |
| column `film_hash` (on `film`, `run_film`, `segment`, `shot_rating`) | `video_hash` |
| index `idx_segment_film` | `idx_segment_video` |

`page.html` and the JSON payloads it consumes carry `film_hash` in segment
detail; those rename with the column.

### Migration mechanics

Applied as `_V2` in `MIGRATIONS`, so `SCHEMA_VERSION` becomes 2 by derivation
(it is `len(MIGRATIONS)` and must stay that way).

Order is load-bearing:

1. `DROP VIEW clip_film` — first, and for a subtler reason than it appears.
   SQLite does *not* refuse a `RENAME COLUMN` that a view depends on: with
   `PRAGMA legacy_alter_table` off (the default, and it must stay off) it
   silently rewrites the view's stored body to use the new column name. Left in
   place, `clip_film` would survive the rename as a view still called
   `clip_film` whose internals now say `video_hash` — half-migrated, and
   invisible unless someone reads `sqlite_master`. There is no `ALTER VIEW`, so
   the view has to be dropped and recreated regardless of the rename.
2. `ALTER TABLE film RENAME TO video`, then `run_film` → `run_video`.
3. `ALTER TABLE … RENAME COLUMN film_hash TO video_hash` on `video`,
   `run_video`, `segment` and `shot_rating`.
4. Drop `idx_segment_film`, create `idx_segment_video`.
5. `CREATE VIEW clip_video` with the renamed column.
6. `CREATE TABLE segment_thumbnail` (Part 2b).

Steps 2 and 3 need no manual repair of the foreign-key graph: SQLite rewrites
the `REFERENCES` clauses of dependent tables through both table and column
renames. After the migration, `segment` reads
`REFERENCES "video"(video_hash)` and the constraint is still enforced.

This sequence was executed against the real `_V1` schema, populated through
every table, before being written down. Verified: every row survives,
`PRAGMA foreign_key_check` is clean, the recreated view returns correct rows,
and — the invariant that matters most — the asymmetric delete semantics are
intact. Deleting a clip still cascades to its segments, verdicts and
thumbnails, while its shot marks survive with `segment_id` set to NULL.

`_V1` is left exactly as written. A migration list is an append-only history of
what a database has been through; editing it to "fix" the old names would leave
existing databases with no path to the new ones.

Requires SQLite ≥ 3.25 for `RENAME COLUMN`. The bundled interpreter has 3.49.
The floor is asserted in a test rather than assumed.

---

## Part 2 — Durability

### 2a. Availability is computed, not stored

No new column. `clips_for_review` and `clip_detail` are not the right place for
filesystem access either — `store.py` is the only module permitted to contain
SQL and should not also become the module that stats files. Availability is
resolved at the boundary that already knows about the workspace root:

- The review server resolves each clip's path and reports `available: false` in
  the `/api/clips` payload when the file is absent.
- The page renders unavailable clips as *missing* in the queue, does not attempt
  to load the video element, and refuses `f`/`o`/`n` with an explanatory status
  line. Segment marks stay permitted — a mark describes footage in the source
  video, which is still there, and the thumbnail strip still renders.
- `POST /api/ratings` rejects a verdict for a clip whose file is missing with a
  400. Client-side refusal alone is not a boundary.
- `cutlist rate` applies the same rule and fails with a clear message.

This closes the deferred follow-up recorded against the previous branch.

### 2b. Thumbnails captured at draft time, stored in the database

New table:

```sql
CREATE TABLE segment_thumbnail (
    segment_id  INTEGER PRIMARY KEY REFERENCES segment(id) ON DELETE CASCADE,
    image       BLOB NOT NULL,
    captured_at TEXT NOT NULL
);
```

A separate table rather than a column on `segment`, because `store.mark_shot`
and `store.segment_by_id` both issue `SELECT *` against `segment`; a BLOB column
there would drag image bytes through every mark written.

`draft` captures one JPEG per segment, from the segment's midpoint, at the
existing 160px width, and writes it inside the same transaction that records the
clip. Cost is roughly one ffmpeg seek per segment — `-ss` before `-i`, so each
is a seek rather than a decode — adding an estimated 10-15 seconds to a run of
ten clips. At roughly 6 KB per thumbnail, two thousand segments is about 12 MB.

`ON DELETE CASCADE` is correct here and is not a contradiction of "never delete
rows": nothing deletes segments, and if a future migration ever did, an orphaned
image would be unreferenceable bytes rather than a lost judgement.

Read path: `/media/thumb/<segment_id>` serves the stored blob. When no row
exists — every segment recorded before this release — it falls back to
generating from the source video as today, and stores the result so the
fallback is paid once. The on-disk `cache/thumbs/` path is removed; the database
is the only home.

### 2c. Source resolution by content hash

`ReviewHandler._source_for` currently guesses `<root>/input/<display_name>` and
silently 404s when the file has been renamed. This is an undocumented assumption
serving the exact surface where segment marks are made, and both thumbnail
capture and re-rendering now depend on it.

Replacement: scan the workspace `input/` directory recursively for known video
extensions, compute `video_id()` for each, and match on hash. `video_id` reads
the first and last megabyte plus the file size, so scanning a dozen sources
costs well under a second. Fall back to a display-name match when no hash
matches, so a source that was re-encoded (new hash, same name) still resolves.
Results are memoised per process.

This lives in a new `cutlist/media/sources.py` rather than in the review server,
because `draft`, `rerender` and `review` all need it.

### 2d. `cutlist rerender`

```
cutlist rerender <clip-path> [--root DIR]
```

Rebuilds a clip from its record: segment timecodes from `segment`, the resolved
preset from `run.preset_json`, the caption from `run.caption_text`, written back
to the path `clip.path` already claims — so the ratings attached to that clip
still describe what is now on disk.

It requires the source video. When the source cannot be resolved it fails with a
message naming the video's display name and hash, not a traceback.

Output is perceptually identical, not byte-identical. Different ffmpeg or x264
builds produce different bytes from the same input, and promising reproducible
bytes would be a promise the tool cannot keep.

**Limit, stated deliberately:** a clip whose segments come from more than one
source video cannot be re-rendered, because `render_clip` takes a single source
path. No such clip can currently exist — `draft` works from one video — so
`rerender` detects the case and refuses with a clear message rather than
motivating a render-path refactor for a case that does not occur. When
multi-source drafting arrives, this is the second place that has to change, and
the error message says so.

---

## Part 3 — Docker

**Image:** `python:3.12-slim`, plus `ffmpeg` and `fonts-dejavu-core` from apt.
No code change is needed for captions: `caption.FONT_CANDIDATES` already lists
`/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf`, which is exactly where
`fonts-dejavu-core` installs it, and DejaVu covers Cyrillic. `opencv-python-
headless` is already the headless build, so there is no X11 dependency.

Runs as a non-root user. `WORKDIR /work`; the workspace root is the mount point,
so `-v "$PWD:/work"` gives the container the same `input/`, `output/` and
`cutlist.sqlite` the host sees. `cutlist` is the entrypoint, so
`docker run … cutlist:latest draft input/x.mp4 --preset …` reads naturally.

**Required code change — `review --host`.** `build_server` binds `127.0.0.1`
unconditionally. Inside a container that is the container's own loopback, so a
published port reaches nothing and `cutlist review` is simply broken under
Docker. A `--host` option defaulting to `127.0.0.1` fixes it while leaving local
behaviour byte-for-byte unchanged. The README states plainly that
`--host 0.0.0.0` exposes an unauthenticated server that reads files from the
workspace to the local network; the existing `_resolve_within_root` guard
confines it to the workspace but does not authenticate anything.

**Documented platform limit.** SQLite file locking over Docker Desktop's
gRPC-FUSE bind mounts on Windows is unreliable, and the failure mode is a
corrupted ratings database — the one artifact with no backup. The README will
say that Docker targets Linux and demo reproducibility, and that Windows should
run natively. A named volume for the database is offered as the mitigation for
anyone who needs Docker on Windows regardless.

No GPU. When v2 introduces local embedding models this becomes a second, much
heavier image; that is noted in the README as future work and not built.

---

## Part 4 — `cutlist demo`

```
cutlist demo [--root DIR] [--count N]
```

Synthesises a multi-shot source video with ffmpeg, drafts clips from it using a
bundled `presets/demo.yaml`, and prints the next command to run.

The technique is already proven in the test suite: `tests/conftest.py` builds a
cut-detectable video by concatenating flat colour segments chosen for BT.601
luma spread, so every cut clears the detector's threshold. The demo source
extends this to roughly a dozen shots of varying length, so the rhythm
constraints have real choices to make rather than one forced answer.

Why it matters more than it looks:

- It is the only way a reader can run the tool. No source video ships, and none
  can — that is a deliberate legal position, not an oversight.
- It is what makes the Docker image demonstrable: `docker run` produces watchable
  output with no input file and no download.
- It exercises the whole pipeline through the real CLI, which nothing currently
  does end to end.

The demo caption is Latin text so it renders under any font, rather than
depending on the Cyrillic coverage the real presets need.

---

## Part 5 — Legibility

No behaviour changes. The 474 existing tests must stay green throughout and are
the definition of "no behaviour change."

**`select/naive.py:_redistribute`.** Currently two functions fused by a boolean
flag and a runtime type switch: `bound` arrives as a float when shrinking and a
list when growing, and `shrink` inverts the sign on every expression in the
body. It is also the only unannotated function in its module. Split into
`_shrink_toward(durations, target, floor)` and
`_grow_toward(durations, target, ceilings)`. The flag and the `isinstance` check
both disappear, and each function reads in one pass.

**`cli.py:draft`.** 115 lines covering validation, probing, hashing, database
connection, preset fingerprinting, run creation, path derivation, caption
rendering, the render loop, provenance writes and reporting. Extract the
run-opening sequence and the per-clip render-and-record step so the command
body reads as an outline of what a draft is.

**Comment density.** A substantial share of comments narrate bugs that no longer
exist — "two concurrent drafts used to both reach for `.scratch_01`", "drafting
the same film and preset twice used to overwrite", "so the two cannot drift
apart again". `cli.py` opens with a twelve-line comment before its first
statement. The history is in git, which is where history belongs; a reader
meeting `draft` for the first time should not read forty lines of incident
report before learning what it does. Every comment explaining a live,
non-obvious constraint stays. Past-tense narration of fixed incidents is reduced
to the constraint it implies.

**Review page help text.** `undo()` does not undo. It navigates back to the
previous clip so it can be re-rated, and the append-only store lets the newer
row win. The status line says this correctly ("reopened — re-rate to
overwrite"); the help modal calls `z` "undo the last verdict", which is wrong.
Segment marks self-correct the same way — press `g` then `b` and the later mark
wins — and nothing says so anywhere. Two lines in the keyboard reference.

This last item is the only change arising from the GoodUI review. That research
concluded the page is already correct on the patterns that transfer (undo over
confirmation, keyboard shortcuts, showing state, direct manipulation,
single-column focus) and rejected fifteen patterns as actively harmful here:
GoodUI's evidence base is checkout and signup flows optimising toward a
business-preferred outcome, whereas this tool has no preferred outcome, only an
honest one. Anything that lowers the cost of one verdict relative to another
corrupts the data the page exists to collect.

---

## Part 6 — Repository credibility

- **`LICENSE`: MIT.** The repository is currently unlicensed, which means all
  rights reserved by default — a reader who likes the work cannot legally use
  it. `pyproject.toml` gains the matching `license` field.
- **`pyproject.toml` metadata:** repository URL, classifiers, README reference.
- **GitHub Actions**, on push and pull request to `main`: install ffmpeg and
  `fonts-dejavu-core`, run pytest on Python 3.12 on ubuntu-latest, run ruff, and
  build the Docker image. Running the suite on Linux also gives the first real
  check that the Linux font path in `caption.py` resolves — nothing tests that
  today, and it is load-bearing for both CI and Docker.
- **ruff** in dev dependencies and CI: default rules plus import sorting,
  `line-length = 100`. That limit is the longest line currently in the
  repository, chosen so the first run reports substantive findings instead of
  fifty reflows.
- **`.gitattributes`** with `* text=auto eol=lf`. Development happens on Windows
  and publishing happens to GitHub; without it, line endings eventually produce
  a diff that means nothing.
- **Dependabot** for GitHub Actions and pip.
- **README:** a one-line statement that no video is shipped or distributed and
  the user supplies their own source; an architecture diagram; sections for
  Docker, `demo`, and `rerender`.

Explicitly out of scope: deployment CI/CD, coverage badges, issue and PR
templates, and screenshots or demo video — the owner will capture the media
themselves, so no placeholders are added.

---

## Ordering

Three dependencies constrain task order:

1. **Legibility (Part 5) before the rename (Part 1).** Both touch `naive.py` and
   `cli.py`. Renamed first, the mechanical diff would bury the refactor's real
   one and make review meaningless.
2. **One `_V2` migration, not two.** Parts 1 and 2 both change the schema. Landing
   them as a single migration gives one upgrade path to test rather than an
   ordering between two.
3. **Source resolution (2c) before thumbnails (2b) and `rerender` (2d),** which
   both depend on it.

Everything else is independent. Docker, demo, and repository credibility can
land in any order relative to the rest, though Docker's `--host` change should
precede the README section that documents it.

## Testing

- Every existing test stays green. The rename touches roughly 250 test
  references; a failure there is a genuine signal, not noise to be edited away.
- **Migration:** a database created at `_V1`, populated through every table, then
  migrated, retains every row under the renamed identifiers, passes
  `PRAGMA foreign_key_check`, and still enforces its foreign keys on new writes.
  Separately and most importantly, the asymmetric delete rule is re-asserted
  *after* migration: deleting a clip cascades to segments, verdicts and
  thumbnails while shot marks survive with a NULL `segment_id`. That invariant
  is the whole reason the schema is shaped as it is, and a rename is exactly the
  kind of change that could quietly drop an `ON DELETE` clause.
  A fresh-versus-migrated schema comparison is deliberately *not* used as the
  proof: both paths run the same `MIGRATIONS` list in the same order, so they
  match by construction and the test would assert nothing. The risk being
  guarded against is data loss during the rename, not schema divergence.
- **Availability:** a clip whose file is deleted is reported unavailable, refuses
  a verdict at the HTTP boundary and in `rate`, and still accepts segment marks.
- **Thumbnails:** captured during `draft`; served from the database with the
  source video deleted; the fallback path generates and persists exactly once
  for a pre-existing segment.
- **`rerender`:** reproduces a deleted clip at its recorded path with the recorded
  duration; fails clearly when the source is missing; refuses a multi-source
  clip.
- **Source resolution:** matches by hash after the file is renamed; falls back to
  display name when the content has changed.
- **`demo`:** runs end to end and produces the requested number of playable clips
  with no input file present.
- **Docker:** the image builds in CI. Running the suite inside the container is
  not attempted — it needs a workspace mount and buys little beyond the build.
