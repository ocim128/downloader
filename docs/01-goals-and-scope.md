# 01 — Goals and Scope

## Background

[x-downloader.com](https://x-downloader.com/en/) is a free web tool: paste an X post link,
click, get the MP4. It's convenient but:

- it's a **public web service** — your download list and IP go through their servers,
- the **bulk mode** is limited,
- it shows ads, and its longevity depends on someone else keeping the upstream working,
- private/restricted posts need their Chrome extension (their trick: read the video
  straight from your own browser storage).

We want the same convenience as a **private local app**: I paste URLs, my machine resolves
and downloads, files land in my folder structure with metadata, and duplicates are skipped
automatically.

## Goals

Numbered and testable — each maps to acceptance criteria in
[05-implementation-plan.md](05-implementation-plan.md).

1. **Batch input**: accept 1..N post URLs via CLI args, a text file (`-f urls.txt`,
   one URL per line, `#` comments allowed), or stdin (pipe from another command).
2. **Highest quality by default**: pick the highest-bitrate MP4 variant the post offers;
   let the user cap it (`--quality best|worst|720|480|...`).
3. **Original media fidelity**: no re-encoding, no watermarking (X videos carry none) —
   byte-for-byte CDN output unless the user explicitly asks for a conversion
   (audio extraction, GIF).
4. **Metadata sidecar**: for every downloaded post, write a JSON file with author, text,
   post date, source URL, chosen variant info, and checksums.
5. **Dedup / skip**: a local SQLite state store records what was downloaded; re-running
   the same URL skips instantly (`--force` to redo).
6. **Resilient batch semantics**: one bad URL (deleted post, photo-only post, network
   error) never aborts the batch; the run ends with a per-item summary and meaningful
   exit codes.
7. **Polite and rate-limit aware**: bounded concurrency, jittered pacing, `Retry-After`
   honored, global cooldown on HTTP 429.
8. **Resumable downloads**: interrupted transfers continue via HTTP Range on `.part`
   files instead of restarting.
9. **HLS support**: posts whose best variant is an m3u8 playlist (long videos,
   broadcasts) are assembled losslessly via ffmpeg when available.
10. **Local-only and private**: no telemetry, no external services beyond X's own CDN and
    (optional, flag-gated) the fxtwitter public API; state and config stay on disk.
11. **Windows-friendly**: correct filename sanitization, `%APPDATA%` config, ffmpeg
    detection, readable console output on Windows Terminal.
12. **Deterministic naming**: output path is a pure function of
    (tweet id, author, date, config) — same input always lands in the same place,
    which is what makes dedup and re-runs trivial.

## Non-goals (explicitly out of scope)

- **Running it as a public web service** for other people. Single user, local machine.
- **Mass scraping / crawling**: no searching X, no timelines, no follower harvesting.
  Input is an explicit list of URLs the user already has.
- **DRM circumvention**: encrypted/DRM streams (Widevine/FairPlay) are reported as
  unsupported, never attacked. Standard unencrypted HLS is fine.
- **Reposting/uploading** to X or anywhere else (the parent repo handles reposting
  separately if at all).
- **Multi-account or credential farming**: at most *your own* cookies, opt-in, stored
  with tight file permissions.
- **Mobile apps / browser extensions**.
- **Screenshot rendering** of posts (x-downloader has this; stretch goal at best — see
  roadmap).
- **GUI installer** packaging (pipx/uv install is enough for the owner).

## User stories

1. *As the owner*, I paste 30 links from my "watch later" list into `urls.txt`, run
   `xdl -f urls.txt`, and get 30 files + 30 sidecars, with 3 skipped because I already
   downloaded them last week.
2. *As the owner*, I pipe a single link: `echo <url> | xdl -` and the MP4 appears in my
   downloads folder a few seconds later.
3. *As the owner*, I re-run last night's failed batch with `xdl retry-failed` after my
   connection dropped, and only the incomplete items are retried.
4. *As the owner*, I hit a 429 storm because I pasted 500 URLs; the tool backs off,
   slows down, and completes the batch unattended instead of burning my IP.
5. *As the owner*, I want the audio of a post as MP3: `xdl <url> --audio-only`.
6. *As the owner*, on my laptop I run `xdl serve` and use a localhost page with a paste
   box and progress list, exactly like x-downloader.com but private (core milestone M3).

## Feature mapping vs. x-downloader.com

| x-downloader.com feature | Ours | Notes |
|---|---|---|
| Paste single URL → MP4 | ✅ core | `xdl <url>` |
| Bulk video downloader | ✅ core, stronger | file/stdin/args input, dedup, retry-failed |
| Original quality (HD/4K) | ✅ core | highest-bitrate variant, no re-encode |
| Highest-quality setting | ✅ | `--quality` cap/select |
| MP3 audio extraction | ✅ phase 4 | `--audio-only` via ffmpeg |
| Broadcast (HLS) downloader | ✅ phase 4 | ffmpeg remux |
| Screenshots of posts | ⛔ stretch | out of MVP; needs HTML rendering |
| Private-account videos (via extension) | ✅ opt-in phase 7 | via own cookies + GraphQL, like their extension reads your session |
| Chrome add-on / Android app / WP plugin | ⛔ | not a service, won't exist |
| "No limits" | ✅ | local tool; politeness limits are self-imposed |
| Privacy: "no download info saved" | ✅ trivially | nothing leaves the machine; we *do* keep a local history DB (that's a feature) |

## MVP definition (must all be true)

- `xdl <url>` downloads the best-quality MP4 of a normal public video post, with sidecar
  and state entry, on Windows.
- `xdl -f urls.txt` processes a mixed batch (some photo-only, one deleted, one 429'd
  simulation) without aborting, printing an accurate summary; exit code is 1 (partial).
- Re-running the same batch skips all completed items in under a second.
- Interrupting a download with Ctrl+C and re-running resumes from where it stopped.
- No account, no cookies, no external service needed for the above.

## Guiding principles

1. **Core library is frontend-free** — the pipeline must be drivable from CLI, tests, and
   the future web UI identically.
2. **Upstreams are adapters, not assumptions** — syndication endpoint changes are the
   #1 risk; every remote call sits behind a narrow interface with recorded fixtures.
3. **Fail per-item, never per-batch.**
4. **Bytes on disk are the source of truth** — the state DB is an accelerator
   (skip/lookup), and its claims are always verified against the filesystem.
5. **Boring dependencies, few of them.**
