# cutlist — ratings and provenance

**Date:** 2026-08-09
**Status:** approved, pending implementation plan
**Follows:** `2026-08-07-cutlist-design.md` (stage 5, brought forward)

## What it does

Records what every clip was made of, and collects judgements about it.

`draft` gains provenance: each run, each clip, and each segment is written to a
local SQLite database along with the seed and preset that produced it. A new
`cutlist review` command serves a keyboard-driven local page for watching a
batch of clips and rating them. `cutlist rate` does the same from the terminal.

Nothing consumes the ratings. This phase collects; scoring is a later phase.

## Why now

The walking skeleton picks segments at random, and one clip in five was worth
using. The montage machinery — shot detection, captioning, encoding, concat —
works. Selection is the entire quality gap.

Closing that gap means scoring shots, and scoring needs training data. Right now
`draft` writes `01.mp4`–`05.mp4` and discards everything about how they were
built: no segment list, no timecodes, no seed. A rating attached to a clip you
cannot decompose is a number with nothing behind it.

So provenance is the substance of this phase and rating is the interface to it.

## Key decisions

### Ratings are durable; everything else is regenerable

Films can be re-probed, clips re-rendered, thumbnails re-extracted. A month of
judgements cannot be recovered from anything.

This asymmetry drives the schema. Ratings carry their own copy of what they
refer to rather than depending on a join surviving, and the delete rules differ
by rating kind: a clip verdict is about one specific assembly and is meaningless
without it, so it cascades; a shot mark is about *footage* and outlives any clip
that happened to contain it, so it survives with its segment reference nulled.

### Two rating levels, because they answer different questions

Shot marks train *what to pick*. Clip verdicts train *how to combine* — the
emergent quality where an assembly is better than the sum of its segments, which
is what separates the Hollywood reference's uniformly relaxed shots from Twin
Peaks' uniformly ominous ones.

Neither level answers the other's question. Collecting both also makes a third
quantity computable: a clip's verdict minus the mean of its shot marks isolates
what the *combination* contributed. Clips scoring well above their parts are the
ones worth mining for what they share.

Clip verdicts are `fire` / `ok` / `no`. Two values would collapse the distinction
between a clip worth posting and a clip that merely works, which is precisely the
distinction the reference batch produced.

Shot marks are `good` / `bad` / `veto`. The first two are soft preferences that
will eventually move sampling weights. `veto` is a permanent hard exclusion —
credits, black frames, burned-in subtitles — and is the one mark that stays valid
regardless of how taste changes.

### Ratings key to timecodes, not shot indices

Re-run detection with different parameters and every shot index shifts, silently
invalidating the whole rating history. Timecodes survive re-detection and can be
re-mapped by overlap.

### Both spans are recorded, because they are different claims

A segment is a 1.2–2.8s trim from a longer shot. You judge the seconds that were
on screen; the useful unit for future scoring is the whole take. "I liked this
moment" and "I liked this shot" cannot be derived from one another after the
fact, so both spans are stored on the segment and copied onto the shot rating.

### Source lives on the segment

A segment comes from exactly one source file. Recording `film_hash` there is what
makes multi-source runs attributable at all — without it, a clip assembled from
two films cannot be traced no matter what is stored above it.

Above the segment, intent and composition need different mechanisms:

- `run_film` is a **table**, written when a run starts. It records which sources
  were pointed at. Intent is not derivable from output — a run that fails to
  produce a clip still needs to record what it was aiming at, and `draft` already
  supports partial runs.
- `clip_film` is a **view** over segments. A clip's composition is exactly the
  distinct sources among its segments. As a view it is queryable like a table and
  cannot drift from the segments it describes.

There is deliberately no scalar `film_hash` on `run` or `clip`. It would be
correct today and meaningless the moment a run spans two sources.

### The database is global, not per-film

`cutlist.sqlite` lives at the project root, not under `cache/<film_hash>/`. Taste
generalises across films, and the cache is regenerable — deleting it must not
destroy a month of judgements.

### `draft` only writes

Nothing in this phase reads ratings back. `draft` writes provenance, `review` and
`rate` write ratings, and no path closes the loop. Honouring `veto` immediately
was considered and rejected: it is a small win now in exchange for a coupling
that has to be unwound when real scoring lands.

### Ratings are append-only

No uniqueness constraint on the rated object. The current verdict is the most
recent row. Changing your mind is itself data.

## Schema

```sql
film            film_hash PK, display_name, duration_s, fps, width, height,
                first_seen_at, last_seen_at

run             id PK, preset_name, preset_sha256, preset_json, caption_text,
                seed NOT NULL, cutlist_version, created_at

run_film        run_id -> run, film_hash -> film,
                PRIMARY KEY (run_id, film_hash)

clip            id PK, run_id -> run, ordinal, path, duration_s,
                UNIQUE (run_id, ordinal)

segment         id PK, clip_id -> clip, position, film_hash -> film NOT NULL,
                seg_start_s, seg_end_s,      -- the trim that was rendered
                shot_start_s, shot_end_s,    -- the containing shot
                shot_index,                  -- convenience; goes stale
                UNIQUE (clip_id, position)

clip_rating     id PK, clip_id -> clip ON DELETE CASCADE,
                verdict CHECK IN ('fire','ok','no'), note, created_at

shot_rating     id PK, film_hash -> film NOT NULL,
                seg_start_s, seg_end_s, shot_start_s, shot_end_s,
                mark CHECK IN ('good','bad','veto'),
                segment_id -> segment ON DELETE SET NULL, note, created_at

clip_film       VIEW: clip_id, film_hash, segment_count
                (SELECT clip_id, film_hash, COUNT(*) FROM segment GROUP BY 1, 2)
```

`seed` is `NOT NULL` because every run must be reproducible; when the user
supplies no seed, `draft` generates one, uses it, and stores it.

`cutlist_version` records which selection algorithm produced the run. When
scoring replaces the naive sampler, ratings from the random era must not be
silently pooled with ratings from the scored era — they measure different things.

`preset_json` stores the resolved preset, not merely a hash of it. Presets are
files that get edited; storing the resolved form makes every run self-describing
after the YAML changes or is deleted. `preset_sha256` exists so runs using an
identical preset can be grouped.

Schema versioning uses `PRAGMA user_version` with an ordered list of migrations
applied on open. The store runs in WAL mode, since `review` and `rate` can both
write.

## Modules

```
cutlist/
  db/
    schema.py       DDL, views, ordered migrations, user_version handling
    store.py        typed read/write; the only module containing SQL
  media/
    thumbs.py       segment thumbnail extraction, lazy and cached
  review/
    server.py       stdlib http.server: JSON API, static assets, ranged video
    page.html       single page, CSS and JS inlined
  feedback/
    rate.py         CLI rating path
```

`store.py` is the only place SQL lives. The server and the CLI are both callers,
which keeps the schema testable without a browser and stops the two rating paths
from drifting apart.

Thumbnails are generated on first view and cached under the run's workdir. They
stay out of the database because they are regenerable, and out of `draft` because
generating them there would slow rendering for clips that may never be reviewed.

## Commands

```
cutlist draft   ...                       # unchanged flags; records provenance
cutlist review  [--film F] [--preset P] [--port N]
cutlist rate    <clip> <fire|ok|no> [--segments "1:good,3:veto"]
cutlist ratings [--json]
```

`review` defaults to clips with no verdict yet, newest first. `ratings` prints a
summary, or dumps the store as JSON.

`rate` identifies a clip by path — the same `output/.../03.mp4` the tool wrote —
resolved back to a `clip` row. An ordinal alone (`03`) is ambiguous across runs,
and a database id is not something anyone has in hand while looking at a file.
A path matching no recorded clip is an error naming the path, not a silent
no-op.

## Review interface

The page surrounds video, which constrains it before any taste enters. Reference
practice puts the surround near 10% of peak white: a pure-black page exaggerates
apparent contrast so shadow detail in the frame cannot be judged, and a tinted
one shifts the viewer's white point through chromatic adaptation. DaVinci Resolve
ships a toggle to remove even the 6-point blue bias in its own panel gray for
this reason.

So: fully achromatic chrome around a neutral mid-gray mat, and no accent colour
within roughly 200px of the frame. Colour is reserved as signal.

Below the player, the segment strip is a **proportional-width timeline** —
segments abutting with 1px seams, width proportional to duration, mono timecode
beneath each. Segments are contiguous in the output, so gaps between them would
lie; proportional widths make a clip's pacing visible without reading a number,
at exactly the moment pacing is being judged.

Marks render on the edge rather than as a fill: a 3px bar under the thumbnail.
`veto` is destructive and so *removes* presence — the thumbnail drops to about
35% opacity with a diagonal hatch — rather than adding another colour.

Interaction is keyboard-first and two-handed, following the Avid layout that
Frame.io and most review tools inherited. Right hand on transport: `space`,
`J`/`K`/`L` to shuttle, arrows to frame-step. Left hand on judgement: `1`–`9`
targets a segment, then `g`/`b`/`v` marks it; `f`/`o`/`n` sets the clip verdict,
which commits and auto-advances; `z` undoes; `?` shows the overlay. Direct
addressing beats arrow traversal at ten or fewer items.

For a user whose hands never leave the keyboard: a real focus ring that survives
against both bright thumbnails and dark chrome, nothing revealed on hover, key
caps printed inline so the legend is the label, a persistent `clip 4 / 9`
counter, and an echo line naming the last action.

Type is dense — 13px chrome, 12px data, 24–28px controls, full viewport with no
page scroll and no centred column. Numerals are tabular so durations compare by
eye. Fonts are served locally as subsetted `.woff2` or fall back to a system
stack; there is no CDN and no build step.

The design is checked against a list of markers that read as generated-by-default
output: Tailwind indigo and violet, indigo-to-violet gradients, glassmorphism,
Inter, 12–16px radii on 24px controls, stacked large shadows on panels that do
not float, uniform 24px gaps, centred max-width columns, stroke-1.5 icon sets,
and pill radii on non-tags.

## Testing

Migrations are applied from an empty database and from each prior version, and
are idempotent.

Store operations round-trip.

Provenance gets a property test: every recorded segment span lies strictly inside
its recorded shot span, every segment names a source present in its run's
`run_film` rows, and a clip's segment durations sum to its rendered duration
within tolerance.

Rating history resolves latest-wins, and `clip_film` agrees with the segments it
derives from.

The HTTP layer is tested at the API level — the page serves, video responses
honour range requests, `POST /ratings` persists, malformed payloads are rejected
and leave the store unchanged. There is no browser automation; the DOM does not
justify a test harness here.

## When things go wrong

A missing or corrupt database is created fresh rather than failing the run;
`draft` must never lose a rendered clip to a storage problem. A `draft` that
fails partway still records its run, its `run_film` rows, and whichever clips
completed, so a partial batch remains reviewable.

`review` refuses to start if the port is taken rather than picking another
silently. A clip whose file has been deleted appears in the list marked missing
and cannot be rated. A rating written while the file is being re-rendered is
accepted — WAL mode handles the concurrency, and the rating refers to segments,
not to bytes on disk.

## Known consequences

Clips produced before this change cannot be imported. They have no recorded
segments, and unless a run was invoked with `--seed`, its selection cannot be
reproduced. The first review session starts from a fresh batch.

The `output/<film>/<preset>/` layout does not survive multi-source runs. It is
left alone here and revisited when multi-source rendering actually lands.

The `film` naming will read oddly once this points at video that is not film.
Renaming the concept to `source` would touch `paths.py`, `probe`, `shots` and the
CLI, so it belongs in its own change.

## Out of scope

Consuming ratings in any form, including honouring `veto`. Embeddings, the
index, contact sheets, the judge, and the shortlist stage. Multi-source
rendering — the schema admits it, `draft` does not yet produce it. Authentication,
multiple users, and mobile layouts. Editing or deleting past ratings through the
interface.

## Later

The ratings collected here become training data for scoring once the CLIP index
exists: liked and disliked shot embeddings form centroids, and every unrated shot
is scored by cosine distance to them, generalising a few dozen judgements across
thousands of shots and across films.

Clip verdicts feed assembly rather than selection. Every clip already has
structural features known at render time — segment count, mean segment duration,
total duration, spread across runtime — and once embeddings exist, mean pairwise
similarity among its segments. Correlating those against verdicts answers whether
a preset wants cohesion or diversity, and narrows its rhythm ranges from evidence
instead of guesswork.

Highly-rated shots also make the agent judge better directly, as few-shot
examples of what this user actually likes.
