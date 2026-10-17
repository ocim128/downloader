# Technical 05 — Storage, Naming & Dedup

Responsibility: `src/xdl/storage/`. Deterministic paths, Windows-safe filenames, the
SQLite state store, and the skip/record lifecycle.

## Output root resolution

Precedence: `--output/-o` flag > `XDL_OUTPUT` env > `config.toml [storage] root` >
`./downloads` (cwd-relative; simple and predictable for a personal tool).

Layouts (config `layout`):

| Layout | Path template | Default |
|---|---|---|
| `by-author` | `{root}/x/{author}/{date}_{id}.mp4` | ✅ |
| `flat` | `{root}/x/{date}_{id}.mp4` | |
| `by-date` | `{root}/x/{yyyy}/{mm}/{date}_{id}.mp4` | |

Template variables available (config `filename_template`):

| Var | Meaning | Example |
|---|---|---|
| `{id}` | tweet id | `1750123456789012352` |
| `{date}` | post date, local tz | `2026-10-01` |
| `{author}` | author screen_name (sanitized) | `someuser` |
| `{index}` | media position in multi-video post | `2` |
| `{slug}` | first 30 chars of text, slugified | `check-out-this-thing` |

Default template: `{date}_{id}` (author comes from the directory in `by-author` layout).
Multi-video posts always append `_v{index}` for index ≥ 2 (`2026-10-01_1750..._v2.mp4`);
index 1 omits the suffix (stable names for the 95% single-video case).

## Determinism contract

`render_path(root, settings, tweet) -> Path` is a pure function. The **same post always
maps to the same path**. This is the foundation of everything:

- skip check = "state row says done AND file exists",
- re-runs are idempotent,
- resume finds its `.part` trivially.

Consequence: template variables that can change over time (author renames!) must resolve
against the **state DB's first-seen values**, not fresh resolutions, when the post is
already recorded — otherwise a rename would orphan history. Rule: if a tweet_id exists in
state, reuse its stored path.

## Windows-safe sanitization (`naming.sanitize_segment`)

1. Replace forbidden chars `<>:"/\|?*` and control chars (`0x00–0x1F`) with `_`.
2. Strip leading/trailing spaces and **trailing dots** (`name.` is invalid on NTFS).
3. Reserved device names — exact match, case-insensitive, with or without extension —
   `CON, PRN, AUX, NUL, COM1–COM9, LPT1–LPT9` → prefix `_` (`_CON`).
4. Cap segment at **120 chars** (leaves room inside 255 and inside 260-char MAX_PATH
   with the folder prefix), truncating on a char boundary, never mid-surrogate-pair.
5. Keep unicode as-is (NTFS is UTF-16 natively); normalize to NFC first so visually
   identical names dedupe.
6. Empty result → `_`.
7. **Every path segment is sanitized — directories included (audit round 2)**: the
   `{author}` folder in the by-author layout goes through this exact function, so a
   trailing dot/space or reserved name can never become a directory and throw
   `WinError 123`. X screen names (`[A-Za-z0-9_]{1,15}`) can't produce these cases
   today — the invariant exists because layouts/templates evolve and the state DB can
   anchor arbitrary stored paths.

`{slug}` additionally: lowercase, non-`[a-z0-9-]` → `-`, collapse runs, strip `-`.

## Collisions

Same tweet id + index → same content by construction (deterministic). A genuine
collision (path exists, state row missing, sha differs — e.g. user re-downloaded an
edited post with `--force`) → suffix ` (2)`, ` (3)` before the extension, recorded in
state. Never silently overwrite; `--force` replaces only after moving the old file to
`<name>.bak` (last bak wins — deliberately simple).

## SQLite state store (`storage/state.py`)

`%APPDATA%/xdl/state.db` (platformdirs), WAL mode, `check_same_thread=False` with a
single-writer discipline (all writes funnel through the orchestrator; SQLite ops are
fast enough to run inline in the event loop — measured µs, and batching wraps each
item's upsert in one transaction).

```sql
CREATE TABLE IF NOT EXISTS batches (              -- audit round 2: the web UI's
    batch_id    INTEGER PRIMARY KEY AUTOINCREMENT, -- POST /jobs endpoints need a
    created_at  TEXT    NOT NULL,                 -- batch history; CLI runs record
    origin      TEXT    NOT NULL,                 -- rows too (origin: cli|web|retry)
    total_items INTEGER NOT NULL,
    status      TEXT    NOT NULL                  -- running|done|partial|failed|cancelled
);

CREATE TABLE IF NOT EXISTS tweets (               -- audit round 2: expected media count
    tweet_id    TEXT PRIMARY KEY,                 -- anchors the complete-skip check
    author      TEXT,                             -- screen_name at first download
    media_count INTEGER NOT NULL,                 -- media assets, from first resolve
    first_seen  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS downloads (
    tweet_id     TEXT    NOT NULL,
    media_index  INTEGER NOT NULL,
    batch_id     INTEGER REFERENCES batches(batch_id),
    variant_url  TEXT,               -- exact URL fetched (provenance/debug)
    file_path    TEXT    NOT NULL,   -- absolute, at record time
    size_bytes   INTEGER,
    sha256       TEXT,               -- optional (--checksum)
    status       TEXT    NOT NULL,   -- ok | incomplete | failed
    error        TEXT,               -- error code for failed rows
    source       TEXT,               -- syndication | fxtwitter | graphql | ytdlp
    downloaded_at TEXT   NOT NULL,   -- ISO 8601 UTC
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (tweet_id, media_index)
);
CREATE INDEX IF NOT EXISTS idx_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_batch  ON downloads(batch_id);
```

API:

| Call | Purpose |
|---|---|
| `seen_complete(tweet_id) -> bool` | `COUNT(media_index WHERE status=ok) == tweets.media_count` — an "any-ok-row" check would wrongly skip multi-video posts whose later videos failed (audit round 2 bug fix) |
| `lookup(tweet_id) -> rows` | skip check + path reuse |
| `record(row)` / `record_many(rows)` | upsert after finalize |
| `mark_incomplete(tweet_id, index, path)` | before transfer starts |
| `failed_items(since=None) -> refs` | powers `xdl retry-failed` |
| `new_batch(origin, total) -> id` / `batches(...)` | batch lifecycle for CLI runs and the web UI's `/jobs` endpoints |
| `upsert_tweet(id, author, media_count)` | recorded on first resolve; anchors `seen_complete` |
| `rebuild_from_disk(root)` | rescan sidecars → reconstruct rows (corruption recovery) |

**Filesystem is truth**: `seen_complete` is only consulted together with
`os.path.exists(stored_path)`; missing file → not skipped, re-download (row reused).

Sidecar files (`*.meta.json`) mirror enough state to make `rebuild_from_disk` lossless —
see [06-metadata-sidecar.md](06-metadata-sidecar.md).

## Skip lifecycle

```
resolve stage:
    tweet = state.tweet(tweet_id)               -- media_count from first resolve
    ok_rows = state.ok_rows(tweet_id)
    if tweet and len(ok_rows) == tweet.media_count
            and all(row files exist) and not --force:
        -> ItemResult(SKIPPED, detail="all N media already downloaded")
    else:
        proceed with MISSING indices only; delete stale ok-rows whose files vanished
finalize stage:
    state.upsert_tweet(tweet_id, author, media_count)   # on first resolve
    state.record(ok rows + sidecar write)   # one transaction per post
partial transfer:
    state.mark_incomplete(...)              # retry-failed and resume target
```

`xdl retry-failed`: re-loads refs for rows `status IN (failed, incomplete)`, runs the
normal pipeline (skip logic naturally re-checks).

## Housekeeping

- `.part` files older than 7 days with no matching `incomplete` row are swept at startup
  (`.part.meta` ETag mismatch handling lives in the engine).
- `state.db` grows ~100 bytes/post — a lifetime of hoarding is a few MB; no vacuum
  ceremony needed. `xdl db vacuum|reset|export` are trivial admin subcommands (backlog).
