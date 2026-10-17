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

    @property
    def min_gap(self) -> float:
        return self._min_gap

    @property
    def pause_remaining(self) -> float:
        return max(0.0, self._pause_until - time.monotonic())

    async def gate(self) -> None:
        """Await before every remote request: global cooldown + min-gap."""
        now = time.monotonic()
        wait = max(self._pause_until - now, self._min_gap - (now - self._last_request))
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    def report_429(self, retry_after: str | None) -> None:
        """One 429 pauses everyone: pause_until = now + max(ra, 30s)."""
        ra = parse_retry_after(retry_after) or 30.0
        self._pause_until = max(self._pause_until, time.monotonic() + max(ra, 30.0))


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
