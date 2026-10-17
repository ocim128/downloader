"""Deterministic, Windows-safe paths (docs/technical/05).

`render_path` is pure: the same post always maps to the same path — the
foundation of skip checks, idempotent re-runs and `.part` resume.
"""

from __future__ import annotations

import re
import unicodedata
from itertools import count
from pathlib import Path

from xdl.models import ResolvedTweet

_FORBIDDEN = '<>:"/\\|?*'
_TRANSLATION = {ord(c): ord("_") for c in _FORBIDDEN}
_TRANSLATION.update({c: ord("_") for c in range(0x20)})  # control chars

_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_MAX_SEGMENT = 120  # leaves room inside 255 and 260-char MAX_PATH with prefix
_SLUG_MAX = 30

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9-]+")
_SLUG_COLLAPSE_RE = re.compile(r"-{2,}")


def sanitize_segment(name: str, *, max_len: int = _MAX_SEGMENT) -> str:
    """Windows-safe path segment (rules 1-7, docs/technical/05). Applies to
    every segment — directories included (audit round 2)."""
    name = unicodedata.normalize("NFC", name)
    name = name.translate(_TRANSLATION)
    name = name.strip(" ").rstrip(".")  # trailing dots/spaces invalid on NTFS
    base = name.split(".")[0].upper()
    if base in _RESERVED_NAMES:  # exact match, with or without extension
        name = "_" + name
    if len(name) > max_len:  # char boundary; never leave a lone surrogate
        name = name[:max_len]
        while name and 0xD800 <= ord(name[-1]) <= 0xDFFF:
            name = name[:-1]
        name = name.rstrip(" .")
    if not name:
        name = "_"
    return name


def slugify(text: str) -> str:
    """`{slug}` variable: lowercase, non-[a-z0-9-] -> '-', collapse, strip."""
    s = text.lower()
    s = _SLUG_STRIP_RE.sub("-", s)
    s = _SLUG_COLLAPSE_RE.sub("-", s).strip("-")
    return s[:_SLUG_MAX]


def render_path(
    root: Path,
    tweet: ResolvedTweet,
    index: int,
    *,
    layout: str = "by-author",
    template: str = "{id}",
    ext: str = ".mp4",
) -> Path:
    """Deterministic destination for one media file of a post."""
    local = tweet.created_at.astimezone()  # post date, local tz
    variables = {
        "id": tweet.ref.tweet_id,
        "date": local.strftime("%Y-%m-%d"),
        "author": tweet.author.screen_name,
        "index": str(index),
        "slug": slugify(tweet.text),
    }
    stem = sanitize_segment(template.format(**variables))
    if index >= 2:  # stable names for the 95% single-video case
        stem = sanitize_segment(f"{stem}_v{index}")

    root = Path(root)
    if layout == "by-author":
        segments = ["x", sanitize_segment(tweet.author.screen_name or "_")]
    elif layout == "by-date":
        segments = ["x", local.strftime("%Y"), local.strftime("%m")]
    elif layout == "flat":
        segments = ["x"]
    else:
        raise ValueError(f"unknown layout {layout!r}")

    return root.joinpath(*segments) / f"{stem}{ext}"


def next_available(path: Path) -> Path:
    """Collision suffix ' (2)', ' (3)' before the extension — never overwrite."""
    if not path.exists():
        return path
    for n in count(2):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")  # count() is infinite
