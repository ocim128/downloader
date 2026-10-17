# Technical 04 — HLS (m3u8) & ffmpeg

Some posts' best rendition is only available as HLS: long videos, broadcasts, and
occasionally fresh uploads before progressive renditions exist. This doc covers
detection, the ffmpeg remux path, the no-ffmpeg fallback, plus the ffmpeg-based
conversions (MP3, GIF).

## Detection

> **⚠ Field reality (2026-10-17, round-1 spike):** tweet payloads never include
> width/height on video variants (only `content_type`/`bitrate`/`url`), and
> m3u8-only variant arrays were never observed live — every video post carries the
> m3u8 **plus** ≥2 progressive MP4s. So resolution-based rules must come from the
> m3u8 `RESOLUTION` lines or a bitrate heuristic, and HLS is best treated as a
> fallback used only when the playlist genuinely beats the best MP4, not a
> pre-emptive path.

A variant is HLS when `content_type == "application/x-mpegURL"` or URL ends
`.m3u8`. Selection policy ([selector](#interaction-with-variant-selection)):

- **Progressive-first (audit round 2)**: X payloads publish no bitrate for HLS variants
  (always null/0) — "HLS advertises higher" is unknowable without fetching the playlist.
  So: always take the best progressive MP4 that meets the `--quality` target.
- Consider HLS **only** when (a) no progressive MP4 exists at all (broadcasts, Spaces,
  some long videos), or (b) the best MP4 is below 720p — and only then fetch and parse
  the master playlist to decide. HLS **requires ffmpeg**. If ffmpeg is absent: use best
  MP4 when one exists (log a notice), else fail `HLS_REQUIRES_FFMPEG` with the install
  hint (`winget install ffmpeg` / `choco install ffmpeg` / apt equivalent).

## Master playlist anatomy

```m3u
#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2176237,RESOLUTION=1280x720,CODECS="avc1.640028,mp4a.40.2"
/ext_tw_video/1750.../pl/720p/.../index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=503215,RESOLUTION=640x360,...
/ext_tw_video/1750.../pl/360p/.../index.m3u8
```

Parse: pick highest `BANDWIDTH` (respecting the `--quality` cap via `RESOLUTION`), resolve
the child playlist URL against the master URL, hand the **child playlist URL to ffmpeg**.

## Preferred path — ffmpeg remux (lossless)

```
ffmpeg -hide_banner -nostdin -loglevel error -stats
       -user_agent "<UA>"
       -allowed_extensions ALL
       -protocol_whitelist file,https,tls,tcp
       -i "<child_playlist_url>"
       -map 0:v:0 -map 0:a?       # first video + audio if present
       -c copy
       -movflags +faststart
       -y "<dest>.part.mp4"
```

- `-c copy` — stream copy, **no re-encode**: bytes-out ≈ bytes-in, fast, lossless.
- `+faststart` — moov atom up front (streams nicely later).
- Write to `.part`-style temp then rename, same invariant as the engine.
- Progress: parse `-progress pipe:1` key=value frames (`out_time_ms`, `total_size`) and
  feed the same `job.progress()` channel as HTTP downloads.
- Exit codes: nonzero → `HLS_FAILED` with stderr tail in the item detail.

### AES-128 segments

X HLS is standard unencrypted HLS or AES-128 with an openly-served key; ffmpeg handles
both transparently. DRM (Widevine/FairPlay `EXT-X-KEY` method != AES-128SAMPLE-AES/plain)
→ detect in playlist, fail `DRM_UNSUPPORTED` without attempting anything.

## Fallback path — manual segment download (rare, optional flag `--hls-manual`)

For environments without ffmpeg where the user accepts a `.ts` concat result... still
needs remux for a valid MP4, so this only produces a playable MPEG-TS file:

1. Fetch child playlist; collect segment URLs (handle `EXT-X-MEDIA-SEQUENCE`,
   live windows: iterate refreshed playlists until `EXT-X-ENDLIST` — broadcasts may be
   still-live; cap wait with `--live-wait` seconds, else take current window).
2. Download segments through the normal engine (Range/resume/throttle all apply; segments
   are tiny so the download semaphore effectively serializes nicely).
3. Concatenate in order into `dest.ts`; validate each segment's `#EXTINF` duration sum ≈
   `EXT-X-TARGETDURATION` sanity.

Documented mostly for completeness — default answer is "install ffmpeg".

## ffmpeg integration details

- Locate once at startup: `shutil.which("ffmpeg")`; if absent, record capability=false.
- Always invoke with `-nostdin` (must not eat our terminal), explicit env, timeout
  (default 30 min per file, configurable), and `asyncio.create_subprocess_exec`
  (not shell).
- `--audio-only` (MP3): same remux shape with
  `-vn -c:a libmp3lame -q:a 0` → `dest.mp3` (V0 ~highest VBR quality; "lossless" claims
  like x-downloader's are marketing — the source AAC is already lossy; we do best-possible
  transcode, documented honestly).
- `--gif` (real GIF from X's mp4-as-gif): two-pass palette
  `ffmpeg -i in.mp4 -vf "fps=15,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" out.gif`
  — output is large; opt-in only.
- ffmpeg subprocesses are the only non-asyncio concurrency; bounded by `ffmpeg_sem` (2).

## Interaction with variant selection

```python
def choose(variants, policy, ffmpeg_available) -> Decision:
    mp4s  = [v for v in variants if v.is_progressive_mp4]
    hls   = [v for v in variants if v.is_hls]
    best_mp4 = max(mp4s, key=bitrate, default=None)
    if best_mp4 and satisfies(best_mp4, policy):
        return Progressive(best_mp4)
    if hls and (best_mp4 is None or best_mp4.height < 720):  # payloads carry no HLS bitrate
        if not ffmpeg_available:
            if best_mp4: return Progressive(best_mp4, notice="HLS better but no ffmpeg")
            return Error(HLS_REQUIRES_FFMPEG)
        return Hls(master=max(hls, key=?))   # bitrate unknown pre-parse; pick first/master
    ...
```

HLS bandwidth is only known *inside* the master playlist, so the HLS decision defers
quality resolution to playlist-parse time (subject to the same `--quality` cap).

## Testing

- Fixtures: master + child playlists (multi-bitrate, audio-only, AES-128, with-DRM) as
  text files; parser unit-tested offline.
- ffmpeg invocations: assert on argv arrays (build-then-compare), not live runs; one
  opt-in live smoke test per release.
