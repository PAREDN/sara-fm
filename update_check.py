# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""New-version check (v0.9.96), like VARA FM's "new version available".

Asks GitHub for the latest release of PAREDN/sara-fm. Nothing is sent but a
plain HTTPS request (with SARA FM's version in the User-Agent); no callsign,
no settings. With no internet -- an AREDN-only site, a go-kit in the field --
it simply finds nothing.

Each source answers with JSON: GitHub's release format ("tag_name",
"html_url"), or a plain {"version": "0.9.96", "url": "..."} for a file kept
on another web site. The first source that answers wins.

Note: the GitHub API only answers for a PUBLIC repository.
"""
from __future__ import annotations

import json
import re
import urllib.request

REPO = "PAREDN/sara-fm"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
SOURCES = [f"https://api.github.com/repos/{REPO}/releases/latest"]


def parse(v: str) -> tuple:
    """'sara-fm-0.9.95' / 'v0.9.96' / '0.9.96' / '1.0' -> (0, 9, 96) / (1, 0)."""
    m = re.search(r"(\d+(?:\.\d+)+)", v or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def short(v: str) -> str:
    p = parse(v)
    return ".".join(str(x) for x in p) if p else (v or "?")


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse(candidate), parse(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def read_answer(data: dict):
    """-> (version, page url) or None."""
    if not isinstance(data, dict):
        return None
    if data.get("draft") or data.get("prerelease"):
        return None
    ver = data.get("tag_name") or data.get("version")
    if not ver or not parse(str(ver)):
        return None
    return str(ver), str(data.get("html_url") or data.get("url") or RELEASES_PAGE)


def latest(timeout: float = 8.0, sources=None, opener=None):
    """(version, page url) of the newest release, or None."""
    from ofdm_core import VERSION
    for url in (sources or SOURCES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"SARA-FM/{short(VERSION)}",
                                                       "Accept": "application/vnd.github+json"})
            with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
                ans = read_answer(json.loads(r.read(200_000).decode("utf-8", "replace")))
            if ans:
                return ans
        except Exception:
            continue
    return None
