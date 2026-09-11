from __future__ import annotations

import re

SYDNEY_METRO = (2000, 2249)
CENTRAL_COAST = (2250, 2263)
NSW_OTHER = (2264, 2999)

CBD_SUBURBS = {"sydney cbd", "haymarket", "barangaroo", "the rocks",
               "millers point", "darling harbour", "circular quay", "pyrmont"}
METRO_SUBURBS = {"north sydney", "chatswood", "parramatta", "macquarie park", "st leonards",
                 "surry hills", "redfern", "alexandria", "mascot", "ryde", "bondi junction",
                 "rhodes", "olympic park", "sydney olympic park", "north ryde", "epping",
                 "burwood", "strathfield", "hurstville", "manly", "eastwood", "ultimo"}
CAPITALS = {"melbourne": "VIC", "brisbane": "QLD", "perth": "WA", "adelaide": "SA",
            "hobart": "TAS", "darwin": "NT", "canberra": "ACT"}

REMOTE = re.compile(r"\b(fully remote|100% remote|work from home|wfh|remote[- ]first|remote)\b", re.I)
HYBRID = re.compile(r"\bhybrid\b", re.I)
DAYS_IN_OFFICE = re.compile(r"(\d)\s*(?:days?)\s*(?:per week\s*)?(?:in|at)\s*(?:the\s*)?office", re.I)
POSTCODE = re.compile(r"\b(2\d{3})\b")


def classify(location_raw: str | None, description: str | None = None) -> tuple[str, int | None]:
    """Return (location_class, days_in_office)."""
    blob = f"{location_raw or ''} {description or ''}"   # days_in_office only
    low = (location_raw or "").lower().strip()
    # Location is decided from location_raw ONLY. The description mentions
    # "remote"/"hybrid"/"Australia" constantly in boilerplate and will
    # misclassify an overseas job as locally acceptable.
    loc_only = low

    days = None
    m = DAYS_IN_OFFICE.search(blob)
    if m:
        days = int(m.group(1))

    if not low and not description:
        return "UNKNOWN", days

    remote = bool(REMOTE.search(loc_only))
    hybrid = bool(HYBRID.search(loc_only))

    in_sydney = (
        any(s in low for s in CBD_SUBURBS | METRO_SUBURBS)
        or "sydney" in low or "nsw" in low
    )
    pc = POSTCODE.search(loc_only)
    if pc:
        code = int(pc.group(1))
        if SYDNEY_METRO[0] <= code <= SYDNEY_METRO[1]:
            in_sydney = True

    if hybrid and in_sydney:
        return "HYBRID_SYDNEY", days
    if remote:
        if "australia" in loc_only or "au only" in loc_only or in_sydney:
            return "REMOTE_AU", days
        return "REMOTE_GLOBAL", days

    for suburb in CBD_SUBURBS:
        if suburb in low:
            return "SYDNEY_CBD", days
    for suburb in METRO_SUBURBS:
        if suburb in low:
            return "SYDNEY_METRO", days

    if pc:
        code = int(pc.group(1))
        if SYDNEY_METRO[0] <= code <= SYDNEY_METRO[1]:
            return "SYDNEY_METRO", days
        if CENTRAL_COAST[0] <= code <= NSW_OTHER[1]:
            return "NSW_REGIONAL", days

    if "sydney" in low:
        return "SYDNEY_METRO", days
    if "nsw" in low:
        return "GREATER_SYDNEY", days

    for city in CAPITALS:
        if city in low:
            return "OTHER_CAPITAL", days

    if low and "australia" not in low:
        return "REQUIRES_RELOCATION", days
    return "UNKNOWN", days
