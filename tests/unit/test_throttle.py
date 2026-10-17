"""ThrottleState: pacing, Retry-After parsing, global pause (docs/technical/03).

Uses a fake clock — no real sleeps in the suite.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta

import pytest

from xdl.engine import throttle as throttle_module
from xdl.engine.throttle import ThrottleState, parse_retry_after


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.waited = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waited += seconds
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(throttle_module, "time", types.SimpleNamespace(monotonic=c.monotonic))
    monkeypatch.setattr(throttle_module, "asyncio", types.SimpleNamespace(sleep=c.sleep))
    return c


async def test_gate_enforces_min_gap(clock: FakeClock) -> None:
    t = ThrottleState(min_gap=0.4)
    await t.gate()
    assert clock.waited == 0.0  # first request: nothing to wait for
    await t.gate()
    assert clock.waited == pytest.approx(0.4)  # second request paces


async def test_gate_no_wait_when_relaxed(clock: FakeClock) -> None:
    t = ThrottleState(min_gap=0.0)
    await t.gate()
    clock.now += 10.0  # long idle since last request
    await t.gate()
    assert clock.waited == 0.0


async def test_report_429_pauses_everyone(clock: FakeClock) -> None:
    t = ThrottleState(min_gap=0.0)
    await t.gate()
    t.report_429("60")
    assert 55.0 <= t.pause_remaining <= 60.0
    await t.gate()  # a second worker's gate absorbs the global cooldown
    assert clock.waited >= 55.0


async def test_report_429_floor_30s(clock: FakeClock) -> None:
    t = ThrottleState(min_gap=0.0)
    t.report_429(None)  # no Retry-After header -> default 30s
    assert t.pause_remaining >= 29.0


async def test_report_429_keeps_longest_pause(clock: FakeClock) -> None:
    t = ThrottleState(min_gap=0.0)
    t.report_429("120")
    t.report_429("1")
    assert t.pause_remaining >= 100.0


def test_parse_retry_after_seconds() -> None:
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 7 ") == 7.0


def test_parse_retry_after_http_date() -> None:
    when = datetime.now(tz=UTC) + timedelta(seconds=90)
    raw = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
    parsed = parse_retry_after(raw)
    assert parsed is not None and 85.0 <= parsed <= 90.0


def test_parse_retry_after_invalid() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None


def test_default_gap_jitter_range() -> None:
    for _ in range(50):
        t = ThrottleState()
        assert 0.2 <= t.min_gap <= 0.5


# --------------------------------------------------------------------------- #
# 429 escalation (docs/technical/08): 3 x 429 within 60 s -> pause >= 120 s
# --------------------------------------------------------------------------- #


async def test_report_429_escalation_on_third(clock: FakeClock) -> None:
    t = ThrottleState(min_gap=0.0)
    t.report_429("1")
    t.report_429("1")
    assert t.pause_remaining < 35.0  # two alone: just the 30 s floor
    t.report_429("1")  # third within 60 s
    assert t.pause_remaining >= 119.0  # max(30 x 2, 120)


async def test_report_429_escalation_doubles_long_retry_after(clock: FakeClock) -> None:
    t = ThrottleState(min_gap=0.0)
    for _ in range(3):
        t.report_429("100")  # ra=100 -> escalated pause = max(200, 120)
    assert t.pause_remaining >= 195.0


async def test_escalation_window_expires_after_60s(clock: FakeClock) -> None:
    t = ThrottleState(min_gap=0.0)
    t.report_429("1")
    t.report_429("1")
    clock.now += 61.0  # the first two age out of the 60 s window
    t.report_429("1")  # only ONE recent -> no escalation
    assert t.pause_remaining < 35.0


def test_report_pause_direct_api() -> None:
    t = ThrottleState(min_gap=0.0)
    t.report_pause(45.0)  # orchestrator-reported resolve-stage 429
    assert 40.0 <= t.pause_remaining <= 45.0
