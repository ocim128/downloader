# Technical 06 — Metadata Sidecar

Every downloaded media file gets a sibling `<basename>.meta.json`. Purposes:
attribution travels with the file, and future-you can answer "where did I get this /
what was the caption?" without X.

## Schema

```json
{
  "schema_version": 1,
  "tweet_id": "1750123456789012352",
  "url": "https://x.com/someuser/status/1750123456789012352",
  "text": "caption text as resolved (may contain t.co urls)",
  "created_at": "2026-09-30T21:14:03Z",
  "author": {
    "screen_name": "someuser",
    "name": "Some User",
    "verified": false
  },
  "media": [
    {
      "index": 1,
      "kind": "video",
      "file": "1750123456789012352.mp4",
      "width": 1280,
      "height": 720,
      "bitrate": 2176000,
      "content_type": "video/mp4",
      "source_variant_url": "https://video.twimg.com/ext_tw_video/.../....mp4",
      "size_bytes": 24117248,
      "poster_url": "https://pbs.twimg.com/ext_tw_video_thumb/...jpg"
    }
  ],
  "stats": {                                           // when source provides (fxtwitter)
    "likes": null, "reposts": null, "replies": null, "views": null
  },
  "source": "syndication",
  "downloaded_at": "2026-10-17T09:12:44Z",
  "xdl_version": "0.4.1"
}
```

Rules:

- One sidecar **per post** (not per media file) when the post has one media item — it
  sits next to the media file with the shared basename. Multi-video posts get **one
  sidecar per media file** (`..._v2.mp4.meta.json`) so a standalone `_v2.mp4` stays
  self-describing; each contains only its own `media[]` entry.
- `schema_version` gates future migrations; readers tolerate unknown fields.
- Atomic write: temp + rename, same as media files.
- `--no-meta` skips sidecars entirely (state DB then carries only paths).
- Timestamps: ISO 8601, UTC (`Z`), second precision.
- `text`: keep as-resolved (X puts `t.co` links in text; resolving those to real URLs is
  backlog nicety, not v1).

## Write order (crash-safety)

1. media file: `.part` → rename (media now final)
2. sidecar: `.part` → rename
3. state row: `status=ok` upsert

A crash between 1 and 3 leaves media-without-state → next run's `--force`-less skip
check sees no row, re-resolves, render_path returns the same path, engine sees existing
complete file with matching size → finalize without re-downloading (idempotent replay).

## Rebuild procedure — not implemented

There is no `rebuild_from_disk` and no `xdl rebuild` command. Glob-and-upsert from
sidecars is a plausible future recovery path, but it had no CLI or UI surface and was
deleted (DECISIONS.md #17). Recover by re-running the URL list: skip logic reuses the
stored `file_path`, and missing files are re-downloaded.
