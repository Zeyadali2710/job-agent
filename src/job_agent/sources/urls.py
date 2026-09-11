from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STRIP_PREFIXES = ("utm_", "mc_", "_hs")
STRIP_EXACT = {"ref", "source", "src", "session", "sessionid", "trk", "trackingId", "fbclid", "gclid", "gh_src"}


def canonicalise(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = parts.hostname.lower() if parts.hostname else ""
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(STRIP_PREFIXES) and k not in STRIP_EXACT
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def url_hash(url: str | None, fallback: str = "") -> str:
    return hashlib.sha256((canonicalise(url) or fallback).encode()).hexdigest()


NOISE_BRACKETS = re.compile(
    r"[\(\[][^)\]]*\b(20\d{2}|intake|req|requisition|job\s*id|#\d+)\b[^)\]]*[\)\]]", re.I)


def title_norm(title: str) -> str:
    """Normalise a title for matching. Drops requisition/year noise in brackets
    but KEEPS discriminating bracket text: '(App-Framework)' and '(Money)' are
    different jobs and must not collapse to the same key."""
    t = title.lower()
    t = NOISE_BRACKETS.sub(" ", t)
    t = re.sub(r"[\(\)\[\]]", " ", t)
    t = re.sub(r"[^a-z0-9+#/ ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()
