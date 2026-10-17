"""Parametrized parse table from docs/technical/01-url-parsing.md."""

from __future__ import annotations

import httpx
import pytest

from xdl import urls
from xdl.models import ErrorCode, XdlError


def parse_ok(raw: str, *, id_mode: bool = False):
    return urls.parse(raw, id_mode=id_mode)


def parse_err(raw: str, *, id_mode: bool = False) -> ErrorCode:
    with pytest.raises(XdlError) as exc:
        urls.parse(raw, id_mode=id_mode)
    return exc.value.code


ID = "1750123456789012352"

# --------------------------------------------------------------------------- #
# Accepted forms (1-15)
# --------------------------------------------------------------------------- #

ACCEPTED = [
    # (input, expected id, expected screen_name)
    (f"https://x.com/user/status/{ID}", ID, "user"),  # 1 canonical x.com
    (f"https://twitter.com/user/status/{ID}", ID, "user"),  # 2 twitter.com
    (f"https://www.x.com/user/status/{ID}", ID, "user"),  # 3 www
    (f"https://mobile.twitter.com/user/status/{ID}", ID, "user"),  # 3 mobile
    (f"https://twitter.com/user/statuses/{ID}", ID, "user"),  # 4 legacy plural
    (f"https://x.com/user/status/{ID}/video/1", ID, "user"),  # 5 media suffix
    (f"https://x.com/user/status/{ID}/photo/2", ID, "user"),  # 5
    (f"https://x.com/user/status/{ID}?s=20&t=abc", ID, "user"),  # 6 tracking
    (f"https://x.com/i/web/status/{ID}", ID, None),  # 7 web client
    (f"https://x.com/intent/post?tweet_id={ID}", ID, None),  # 8 intent
    (f"https://twitter.com/intent/tweet?tweet_id={ID}", ID, None),  # 8 legacy
    (
        f"https://platform.twitter.com/embed/Tweet.html?id={ID}",
        ID,
        None,
    ),  # 9 embed
    (f"https://fxtwitter.com/user/status/{ID}", ID, "user"),  # 11 mirror
    (f"https://vxtwitter.com/user/status/{ID}", ID, "user"),  # 11
    (f"https://fixupx.com/user/status/{ID}", ID, "user"),  # 11
    (f"https://twittpr.com/user/status/{ID}", ID, "user"),  # 11
    (f"x.com/user/status/{ID}", ID, "user"),  # 12 scheme-less
    (f"//x.com/user/status/{ID}", ID, "user"),  # 15 protocol-relative
    (f"  https://x.com/user/status/{ID}  ", ID, "user"),  # 14 whitespace
    (
        f"Check this https://x.com/user/status/{ID} out",
        ID,
        "user",
    ),  # 14 embedded in text
    (f"https://x.com:443/user/status/{ID}", ID, "user"),  # stray port
    (f"https://x.com/user/statuses/{ID}/video/1?a=b#c", ID, "user"),
]


@pytest.mark.parametrize(("raw", "expected_id", "expected_user"), ACCEPTED)
def test_accepted_forms(raw: str, expected_id: str, expected_user: str | None) -> None:
    ref = parse_ok(raw)
    assert ref.tweet_id == expected_id
    assert ref.screen_name == expected_user
    assert ref.canonical_url == f"https://x.com/{expected_user or 'i'}/status/{expected_id}"


def test_bare_id_mode() -> None:  # 13
    ref = parse_ok(ID, id_mode=True)
    assert ref.tweet_id == ID
    assert ref.screen_name is None


def test_bare_id_without_flag_rejected() -> None:
    assert parse_err(ID) is ErrorCode.URL_UNRECOGNIZED


# --------------------------------------------------------------------------- #
# Error cases (spec table)
# --------------------------------------------------------------------------- #


def test_instagram_rejected() -> None:
    assert parse_err("https://instagram.com/p/xyz") is ErrorCode.URL_UNRECOGNIZED


def test_suffix_host_rejected() -> None:
    assert parse_err("https://evilx.com/user/status/1234567890") is ErrorCode.URL_UNRECOGNIZED
    assert parse_err("https://evil-x.com/user/status/1234567890") is ErrorCode.URL_UNRECOGNIZED
    assert parse_err("https://x.evil.com/user/status/1234567890") is ErrorCode.URL_UNRECOGNIZED


def test_subdomain_rejected() -> None:
    # only www./mobile. are stripped (plus platform.twitter.com for embeds)
    assert parse_err("https://sub.x.com/user/status/1234567890") is ErrorCode.URL_UNRECOGNIZED


def test_id_too_long() -> None:
    overlong = "https://x.com/user/status/17500000000000000000000"  # 20 digits
    assert parse_err(overlong) is ErrorCode.TWEET_ID_INVALID


def test_short_id_ok() -> None:
    # 2-digit ids parse (id "20" is the first tweet); only 1 digit is invalid
    ref = parse_ok("https://x.com/user/status/12")
    assert ref.tweet_id == "12"


def test_not_a_url() -> None:
    assert parse_err("not a url at all") is ErrorCode.URL_UNRECOGNIZED
    assert parse_err("") is ErrorCode.URL_UNRECOGNIZED


def test_single_digit_id_invalid() -> None:
    assert parse_err("https://x.com/user/status/5", id_mode=False) is ErrorCode.TWEET_ID_INVALID


def test_nonnumeric_id_invalid() -> None:
    assert parse_err("https://x.com/user/status/abcdef123") is ErrorCode.URL_UNRECOGNIZED


# --------------------------------------------------------------------------- #
# t.co expansion
# --------------------------------------------------------------------------- #


def test_parse_rejects_tco_without_expansion() -> None:
    assert parse_err("https://t.co/AbCdEf123") is ErrorCode.URL_UNRECOGNIZED


def test_match_tco() -> None:
    assert urls.match_tco("https://t.co/AbCdEf123") == "https://t.co/AbCdEf123"
    assert urls.match_tco("see https://t.co/AbCdEf123 pls") == "https://t.co/AbCdEf123"
    assert urls.match_tco(f"https://x.com/user/status/{ID}") is None


@pytest.mark.asyncio
async def test_expand_tco_one_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "t.co"
        return httpx.Response(301, headers={"location": f"https://x.com/user/status/{ID}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        target = await urls.expand_tco(client, "https://t.co/AbCdEf123")
    assert target == f"https://x.com/user/status/{ID}"
    ref = urls.parse(target)
    assert ref.tweet_id == ID and ref.screen_name == "user"


@pytest.mark.asyncio
async def test_normalize_full_flow() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"https://twitter.com/user/status/{ID}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ref = await urls.normalize(client, "https://t.co/AbCdEf123")
    assert ref.tweet_id == ID
    assert ref.canonical_url == f"https://x.com/user/status/{ID}"


@pytest.mark.asyncio
async def test_expand_tco_no_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="no redirect here")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(XdlError) as exc:
            await urls.expand_tco(client, "https://t.co/xyz")
    assert exc.value.code is ErrorCode.URL_UNRECOGNIZED


@pytest.mark.asyncio
async def test_tco_to_disallowed_host() -> None:
    # defense in depth: one hop landing on a non-allowed host is rejected
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://example.com/whatever"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(XdlError):
            await urls.normalize(client, "https://t.co/xyz")


# --------------------------------------------------------------------------- #
# Media URL allowlist
# --------------------------------------------------------------------------- #


def test_media_url_allowlist() -> None:
    assert urls.is_allowed_media_url("https://video.twimg.com/ext_tw_video/1/vid/avc1/720x1280/xx.mp4")
    assert urls.is_allowed_media_url("https://pbs.twimg.com/amplify_video_thumb/123/img.jpg")
    assert urls.is_allowed_media_url("https://abs.twimg.com/hot.gif")
    assert not urls.is_allowed_media_url("https://evil.twimg.com.evil.net/x.mp4")
    assert not urls.is_allowed_media_url("https://evil.net/video.mp4")
    assert not urls.is_allowed_media_url("https://twimg.com.evil.net/x.mp4")
