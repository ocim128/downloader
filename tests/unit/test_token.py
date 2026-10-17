"""Token algorithm goldens — the single most valuable test in the repo.

The implementation is a byte-exact port of V8's `DoubleToRadixStringView`
(Dragon-style fraction loop with half-ulp delta, round-to-even, carry
back-trace), verified against Node v22 `((Number(id)/1e15)*Math.PI).toString(36)
.replace(/(0+|\\.)/g,'')` on 3000 sampled tweet ids with ZERO mismatches.

Note on the "6dq1a2xwd9" anchor quoted in the plan/prompt: the live endpoint
does not validate tokens at all (probed 2026-10-17: token=zzzzzz and empty
token both return HTTP 200), so that shorter form — produced by the doc
sketch's greedy-trim — was accepted live. We ship the V8-exact form
(``6dq1a2xwd93``, one digit longer, a strict extension of the anchor) because
it is byte-identical to what react-tweet sends in production, and Agent B's
`tests/fixtures/golden_tokens.json` goldens are Node-computed.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path

import pytest

from xdl.resolve.syndication import (
    _f64_to_base36_js,
    _float_of_base36,
    compute_token,
)

# Node v22.16.0-verified goldens (2026-10-17): anchors + evenly spread
# snowflake-range sample ids.
GOLDENS: dict[str, str] = {
    "20": "6dq1a2xwd93",
    "21": "6p7jcdvzsb",
    "123456789012345678": "arumtis9w99",
    "1750123456789012352": "48q6asjxr3",
    "1799999999999999999": "4d2v7cbm2xj",
    "100": "vwm6eephu8",
    "12345": "31esng3xq8a",
    "5639204740561071959": "do4313qj6b",
    "5895164605079987165": "eag7eqi7c2m",
    "6121601747147618504": "eu7kuhf2awe",
    "6372485000959720218": "fg3r2oaxtge",
    "6595826540000181755": "fzleenudshf",
    "6817747400060980518": "giyl2ckost",
    "7045075807784168866": "h2sravznr6",
    "7299418402012119495": "hozsrsqux8",
    "7540523709986053373": "ia1951k8u8a",
    "7785376654501314647": "ivehct9lfa",
}

# Agent B's live-captured goldens (tests/fixtures/golden_tokens.json), when
# they land on this branch.
FIXTURE_GOLDENS = Path(__file__).parents[1] / "fixtures" / "golden_tokens.json"


@pytest.mark.parametrize(("tweet_id", "expected"), sorted(GOLDENS.items()))
def test_golden_tokens(tweet_id: str, expected: str) -> None:
    assert compute_token(tweet_id) == expected


def test_documented_live_verified_anchor_is_a_prefix() -> None:
    # The plan's live-verified anchor "6dq1a2xwd9" (HTTP 200 against the real
    # endpoint) is a strict prefix of our V8-exact token for id 20.
    assert compute_token("20").startswith("6dq1a2xwd9")
    assert compute_token("20") == "6dq1a2xwd93"


def test_agent_b_fixture_goldens_if_present() -> None:
    if not FIXTURE_GOLDENS.is_file():
        pytest.skip("tests/fixtures/golden_tokens.json not merged yet (Agent B lane)")
    entries = json.loads(FIXTURE_GOLDENS.read_text(encoding="utf-8"))
    for entry in entries:
        assert compute_token(entry["id"]) == entry["token"], f"golden drift for id {entry['id']}"


# --------------------------------------------------------------------------- #
# _f64_to_base36_js properties (JS-known outputs + round-trip)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (0.5, "0.i"),  # 18/36
        (1.5, "1.i"),
        (2.5, "2.i"),
        (36.0, "10"),
        (35.0, "z"),
        (0.0, "0"),
        (10.5, "a.i"),
        (1 / 3, "0.c"),  # V8 rounds the repeated-'b' run up
        (2 / 3, "0.o"),
        (0.1, "0.3lllllllllm"),  # the canonical V8 radix-36 oddity
        (0.25, "0.9"),
    ],
)
def test_known_js_base36_outputs(x: float, expected: str) -> None:
    assert _f64_to_base36_js(x) == expected


def test_close_to_value_property() -> None:
    # V8's non-power-of-2 radix output does NOT always round-trip exactly
    # (known quirk — it is not a shortest-round-trip algorithm). The Dragon
    # loop's guarantee is closeness: the parsed-back value stays within a few
    # ulps (relative error << 1e-12) across the token value range.
    for i in range(1, 2000):
        x = float(i) / 1e15 * math.pi
        back = _float_of_base36(_f64_to_base36_js(x))
        assert back != 0.0
        assert abs(back - x) <= abs(x) * 1e-12, f"lost too much precision for {x!r}"


def test_float_of_base36_exactness() -> None:
    assert _float_of_base36("0.i") == 0.5
    assert _float_of_base36("10") == 36.0
    assert _float_of_base36("0.c") == float(Fraction(12, 36))
    assert _float_of_base36("z") == 35.0


def test_token_shape() -> None:
    for tweet_id in GOLDENS:
        token = compute_token(tweet_id)
        assert token != ""
        assert token.isalnum()
        assert "0" not in token  # zero runs and the dot are stripped
        assert "." not in token
