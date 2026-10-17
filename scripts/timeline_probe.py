#!/usr/bin/env python3
"""Spike: try the syndication timeline endpoint for a screen name."""

import re
import sys

import httpx

name = sys.argv[1] if len(sys.argv) > 1 else "LegendOfZelda"
r = httpx.get(
    "https://syndication.twitter.com/srv/timeline-profile/screen-name/" + name,
    params={"showReplies": "false"},
    headers={"User-Agent": "curl/8.0.1"},
    timeout=25,
)
print(r.status_code, len(r.text))
print(r.text[:200].replace("\n", " "))
if r.status_code == 200:
    ids = sorted(set(re.findall(r'data-tweet-id=["\x27](\d{16,})', r.text)))
    if not ids:
        ids = sorted(set(re.findall(r'/status(?:es)?/(\d{16,})', r.text)))
    print("id count:", len(ids))
    print(ids[:40])
