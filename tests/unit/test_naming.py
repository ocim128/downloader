"""Windows sanitization table + render_path determinism (docs/technical/05)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from xdl.models import Author, MediaAsset, ResolvedTweet, TweetRef
from xdl.storage.naming import next_available, render_path, sanitize_segment, slugify


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("normal", "normal"),
        ("", "_"),  # rule 6
        ("   ", "_"),
        ("trailing dots...", "trailing dots"),  # rule 2
        ("  padded  ", "padded"),
        ("a<b>c:d\"e/f\\g|h?i*j", "a_b_c_d_e_f_g_h_i_j"),  # rule 1
        ("ctrl\x00\x1fchars", "ctrl__chars"),  # rule 1 control chars
        ("CON", "_CON"),  # rule 3
        ("con.txt", "_con.txt"),
        ("NUL", "_NUL"),
        ("COM1.mp4", "_COM1.mp4"),
        ("LPT9", "_LPT9"),
        ("COM0", "COM0"),  # COM0 is not reserved
        ("console", "console"),  # not an exact match
        ("x" * 300, "x" * 120),  # rule 4 cap
        ("café", "café"),  # rule 5 unicode kept
        ("a.b.c", "a.b.c"),  # interior dots fine
        (".hidden", ".hidden"),  # leading dot legal on NTFS
    ],
)
def test_sanitize_segment(raw: str, expected: str) -> None:
    assert sanitize_segment(raw) == expected


def test_sanitize_nfc_normalization() -> None:
    # decomposed e-acute vs composed: NFC makes visually identical names dedupe
    assert sanitize_segment("e\u0301x") == sanitize_segment("\u00e9x") == "éx"


def test_sanitize_cap_does_not_end_with_dot_or_space() -> None:
    out = sanitize_segment("x" * 119 + ". ")
    assert len(out) <= 120 and not out.endswith(".") and not out.endswith(" ")


def test_slugify() -> None:
    assert slugify("Check OUT this Thing!!") == "check-out-this-thing"
    assert slugify("  multiple   spaces  ") == "multiple-spaces"
    assert slugify("ümlaut text") == "mlaut-text"
    assert len(slugify("x" * 100)) == 30
    assert slugify("---") == ""


def _tweet(
    tweet_id: str = "1750123456789012352",
    author: str = "someuser",
    created: datetime | None = None,
) -> ResolvedTweet:
    return ResolvedTweet(
        ref=TweetRef(tweet_id=tweet_id, screen_name=author,
                     canonical_url=f"https://x.com/{author}/status/{tweet_id}"),
        text="caption text",
        created_at=created or datetime(2026, 10, 1, 17, 50, tzinfo=UTC),
        author=Author(screen_name=author, display_name="Some User"),
        media=[MediaAsset(kind="video", index=1)],
        source="syndication",
    )


def test_render_path_by_author_default(tmp_path: Path) -> None:
    p = render_path(tmp_path, _tweet(), 1)
    assert p == tmp_path / "x" / "someuser" / f"{_local_date(_tweet())}_1750123456789012352.mp4"


def _local_date(tweet: ResolvedTweet) -> str:
    # {date} is the post date in the machine's local tz per spec — compute the
    # expectation with the same conversion so the test is tz-agnostic.
    return tweet.created_at.astimezone().strftime("%Y-%m-%d")


def test_render_path_flat_and_by_date(tmp_path: Path) -> None:
    d = _local_date(_tweet())
    assert render_path(tmp_path, _tweet(), 1, layout="flat") == (
        tmp_path / "x" / f"{d}_1750123456789012352.mp4"
    )
    assert render_path(tmp_path, _tweet(), 1, layout="by-date") == (
        tmp_path / "x" / f"{_tweet().created_at.astimezone().year}"
        / f"{_tweet().created_at.astimezone().month:02d}" / f"{d}_1750123456789012352.mp4"
    )


def test_render_path_multi_video_suffix(tmp_path: Path) -> None:
    # index 1 omits the suffix; >=2 appends _v{index} (stable single-video names)
    d = _local_date(_tweet())
    assert render_path(tmp_path, _tweet(), 1).name == f"{d}_1750123456789012352.mp4"
    assert render_path(tmp_path, _tweet(), 2).name == f"{d}_1750123456789012352_v2.mp4"
    assert render_path(tmp_path, _tweet(), 3).name == f"{d}_1750123456789012352_v3.mp4"


def test_render_path_deterministic(tmp_path: Path) -> None:
    a = render_path(tmp_path, _tweet(), 1)
    b = render_path(tmp_path, _tweet(), 1)
    assert a == b


def test_render_path_author_sanitized(tmp_path: Path) -> None:
    # hostile author name -> every segment (directories included) sanitized;
    # trailing dot is stripped by rule 2, not replaced
    p = render_path(tmp_path, _tweet(author="bad<>name."), 1)
    assert p.parent == tmp_path / "x" / "bad__name"
    assert p.name == f"{_local_date(_tweet())}_1750123456789012352.mp4"


def test_render_path_template_vars(tmp_path: Path) -> None:
    p = render_path(tmp_path, _tweet(), 1, template="{date}_{author}_{slug}")
    assert p.name == f"{_local_date(_tweet())}_someuser_caption-text.mp4"


def test_render_path_emoji_text_slug(tmp_path: Path) -> None:
    tweet = ResolvedTweet(
        ref=TweetRef(tweet_id="20", screen_name="jack",
                     canonical_url="https://x.com/jack/status/20"),
        text="just setting up my twttr 🎉",
        created_at=datetime(2006, 3, 21, 20, 50, tzinfo=UTC),
        author=Author(screen_name="jack", display_name="jack"),
        media=[MediaAsset(kind="video", index=1)],
        source="syndication",
    )
    p = render_path(tmp_path, tweet, 1, template="{date}_{id}_{slug}")
    assert p.name == f"{_local_date(tweet)}_20_just-setting-up-my-twttr.mp4"


def test_next_available(tmp_path: Path) -> None:
    first = tmp_path / "a.mp4"
    assert next_available(first) == first
    first.write_bytes(b"x")
    assert next_available(first) == tmp_path / "a (2).mp4"
    (tmp_path / "a (2).mp4").write_bytes(b"x")
    assert next_available(first) == tmp_path / "a (3).mp4"
