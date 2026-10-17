"""ThrottleState: pacing jitter, global 429 cooldown (docs/technical/03).

Shared by resolve and download stages — one worker's 429 pauses all workers.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


class ThrottleState:
    def __init__(self, min_gap: float | None = None) -> None:
        # Resolve pacing is jittered at startup to 0.2-0.5 s unless a fixed
        # floor is configured (--delay).
        self._min_gap = min_gap if min_gap is not None else random.uniform(0.2, 0.5)
        self._pause_until = 0.0
        self._last_request = 0.0
        self._recent_429s: list[float] = []  # monotonic timestamps (escalation)

    @property
    def min_gap(self) -> float:
        return self._min_gap

    @property
    def pause_remaining(self) -> float:
        return max(0.0, self._pause_until - time.monotonic())

    async def gate(self) -> None:
        """Await before every remote request: global cooldown + min-gap.

        The slot is RESERVED before awaiting. `_last_request` is advanced to the
        instant this caller will occupy, so concurrent callers queue behind each
        other instead of all reading the same stale timestamp, sleeping the same
        duration and firing in a burst. The read/write pair is atomic because the
        event loop is single-threaded and no `await` sits between them.
        """
        now = time.monotonic()
        slot = max(now, self._pause_until, self._last_request + self._min_gap)
        self._last_request = slot
        if (delay := slot - now) > 0:
            await asyncio.sleep(delay)

    def report_429(self, retry_after: str | None) -> None:
        """One 429 pauses everyone: pause_until = now + max(ra, 30s)
        (docs/technical/03)."""
        ra = parse_retry_after(retry_after) or 30.0
        self.report_pause(ra)

    def report_pause(self, seconds: float) -> None:
        """Record a 429-class pause (seconds already parsed by the caller).

        Escalation per docs/technical/08: 3 x 429 within 60 s ->
        pause_until = now + max(ra x 2, 120 s) — the run is being throttled,
        not the request.
        """
        now = time.monotonic()
        self._recent_429s = [t for t in self._recent_429s if now - t < 60.0]
        self._recent_429s.append(now)
        pause = max(seconds, 30.0)
        if len(self._recent_429s) >= 3:
            pause = max(pause * 2, 120.0)
        self._pause_until = max(self._pause_until, now + pause)


def parse_retry_after(raw: str | None) -> float | None:
    """`Retry-After` as seconds (int) or HTTP-date; None when absent/invalid."""
    if raw is None:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(tz=UTC)).total_seconds())
