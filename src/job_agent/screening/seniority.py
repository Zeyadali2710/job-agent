from __future__ import annotations

import re

TARGET = re.compile(
    r"\b(graduate|grad program|new grad|early career(s)?|emerging talent|junior|"
    r"entry[- ]level|trainee|cadet|intern(ship)?|campus hire|l1|se1|"
    r"software engineer i(?![iv])|associate)\b", re.I)

SENIOR_NOUNS = r"(director|partner|principal|manager|lead|architect|consultant|vice president|vp)"
NEGATIVE = re.compile(
    rf"\b(senior|staff|principal|head of|director|manager|architect|"
    rf"l[4-9]|sme|lead\b)\b|\bassociate\s+{SENIOR_NOUNS}\b", re.I)

YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)\b", re.I)


def classify(title: str, description: str | None = None) -> tuple[str, int | None, bool]:
    """Return (seniority_class, min_years, conflict)."""
    t = title or ""
    negative = bool(NEGATIVE.search(t))
    target = bool(TARGET.search(t))

    if re.search(r"\bintern(ship)?\b", t, re.I) and not negative:
        cls = "INTERN"
    elif re.search(r"\b(graduate|grad program|new grad|campus hire|cadet)\b", t, re.I) and not negative:
        cls = "GRADUATE"
    elif target and not negative:
        cls = "JUNIOR"
    elif negative:
        is_associate_exec = bool(re.search(rf"\bassociate\s+{SENIOR_NOUNS}\b", t, re.I))
        if is_associate_exec:
            cls = "SENIOR"
        elif re.search(r"\b(head of|director|vice president|vp)\b", t, re.I):
            cls = "LEAD"
        else:
            cls = "SENIOR"
    else:
        cls = "UNKNOWN"

    min_years = None
    if description:
        hits = [int(m.group(1)) for m in YEARS.finditer(description)]
        if hits:
            min_years = min(hits)

    conflict = cls in ("INTERN", "GRADUATE", "JUNIOR") and (min_years or 0) >= 4
    return cls, min_years, conflict
