# 06 — Risks and Legal Notes

## Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Syndication endpoint changes shape, adds auth, or dies | **High** (unofficial API, changes have happened before) | Critical — primary resolver lost | Adapter isolation + recorded fixtures + golden tests; **yt-dlp opt-in adapter as the maintenance safety net** (community fixes X breakage continuously — see tech 02 Adapter D); fxtwitter fallback (privacy-caveated); M1 spike gate before building on it. Live-probed healthy 2026-10-17: guest access, computed token, default curl UA, HTTP 200 — **but** audit round 2 notes it is embed-oriented and refuses/omits media on sensitive, age-gated and fresh posts (est. 20–40% of real-world mixes); mitigated by `NOT_EMBEDDABLE` auto-fallback and M1 spike fixtures for exactly those cases |
| R2 | Rate limiting / IP throttle (429) on CDN or syndication | Medium | Batch stalls or fails | Polite defaults (concurrency 3, jittered pacing), `Retry-After` honored, global cooldown, circuit breaker; docs advise batch sizes ≤ a few hundred per run |
| R3 | Token algorithm drift (endpoint starts rejecting old tokens) | Medium | Resolve 404s en masse | Token fn is unit-golden-tested; on resolve-404-storm the circuit breaker halts; fix is a one-function patch documented in the tech spec |
| R4 | HLS-only posts without ffmpeg installed | Medium | Feature unusable | Startup detection + clear install hint; segment-download fallback documented (still needs ffmpeg for clean mp4 — honest error instead) |
| R5 | Windows path issues (reserved names, illegal chars, 255 limit, unicode authors) | Medium | File write errors | Sanitizer with reserved-name table + parametrized tests (see storage spec) |
| R6 | SQLite state/db corruption (hard power-off) | Low | False skips | Verify file-exists before skipping; state DB is rebuildable by scanning sidecars; WAL mode |
| R7 | Remote JSON malformed/hostile fields | Low | Crash or bad paths | Strict pydantic parsing; URL host allowlist before GET (`*.twimg.com`, `*.x.com`); no HTML parsing of remote content |
| R8 | Cookie theft/mishandling (phase 7 only) | Low (opt-in) | Account compromise | Cookies only in a user-named local file; startup permission check (owner-only warn); never logged, never sent anywhere except `api.x.com` |
| R9 | Scope creep into a crawler | Medium | ToS/legal exposure, bans | Non-goals doc is explicit; no timeline/author-scraping features without a deliberate re-decision |
| R10 | fxtwitter (if enabled) changes or rate-limits | Medium | Fallback lost | It's a fallback only; chain continues to error taxonomy; flag-gated |
| R11 | Owner's IP flagged for automated access | Low–Medium | Temporary blocks | Defaults assume unattended overnight batches are fine but bounded; document how to slow down further (`--concurrency 1`, `--delay`) |

## Legal & policy notes (private-use positioning)

- **X Terms of Service** restrict scraping content without permission. This tool is a
  private, single-user convenience that fetches the same public data a browser renders
  when a post is viewed/embedded (the syndication endpoint is what X's own embed widget
  uses). It is not a crawler: it processes only URLs the user explicitly supplies.
  Still, automated access technically sits in a gray zone — this is the owner's
  informed personal choice, at personal scale.
- **Copyright**: downloaded videos belong to their creators. Private retention/viewing
  of publicly posted media is the intended use; **redistribution is not**. The metadata
  sidecar preserves attribution (author, post URL) on purpose — keep it next to files.
- **No DRM circumvention**: standard unencrypted HLS is assembled with ffmpeg (keys, when
  present, are served inline by the CDN to any viewer). DRM-protected streams
  (Widevine/FairPlay) are detected and refused with a clear error — never attacked.
- **No credentials by default**: the account-free design is also a legal-risk reducer.
  The phase-7 GraphQL resolver uses only the owner's own session, for posts their own
  browser can already see — same spirit as x-downloader's extension reading your browser
  storage. Re-decide at that milestone with current ToS in mind.
- **Privacy of others**: if archiving posts from private/protected accounts you follow,
  keep the files private too.

## Cookie handling policy (phase 7 prerequisite)

1. Opt-in only via `--cookies-file PATH` (no config default that silently persists).
2. File format: Netscape cookie jar. Loaded once at startup into the client, never
   written back, never logged; log lines redact cookie values by pattern.
3. Startup warns if the file is readable by other users (best-effort check on Windows:
   `icacls`-based note; full ACL enforcement is manual).
4. Session cookies go only to `api.x.com` (transport-level allowlist), so a malicious
   redirect cannot exfiltrate them.
5. Document an expiry expectation: `auth_token` is long-lived but can rotate; failures
   surface as `AUTH_REQUIRED` with "re-export cookies" guidance, not silent 404s.
6. Never extract cookies from browser stores programmatically. Chrome ≥ 127 on Windows
   uses app-bound encryption (the cookie key is bound to a SYSTEM service), so external
   decryption simply fails — and attempts are fragile even when they work. Export a
   `cookies.txt` via a browser extension (e.g. "Get cookies.txt LOCALLY") or from
   Firefox instead; the tool only ever reads the file you explicitly point it at.

## Break-glass procedures

- **Syndication dies mid-usage**: set `fallback = "ytdlp"` in `config.toml` (preferred
  safety net, M3+), or `resolver = "fxtwitter"` for occasional single recoveries;
  fixtures make the re-spike cheap.
- **Persistent 429s even slow**: `--concurrency 1 --delay 5`; if still blocked, pause for
  the day (IP-level cooldowns are typically 15 min–24 h).
