from __future__ import annotations

import re

MONEY = r"\$?\s*(\d{2,3}(?:[,\.]\d{3})*|\d{2,3})\s*(k\b)?"
RANGE = re.compile(rf"{MONEY}\s*(?:-|–|to)\s*{MONEY}", re.I)
SINGLE = re.compile(MONEY, re.I)
# The rate marker (/ per / a) is MANDATORY. With it optional, "38 hours per week"
# parsed as $38/hour and "365 days" as a $365 day rate — both appear in almost
# every ad.
DAILY = re.compile(r"\$?\s*(\d{3,4})\s*(?:/|per\s+|a\s+)\s*(?:day|daily)\b", re.I)
HOURLY = re.compile(r"\$?\s*(\d{2,3}(?:\.\d{2})?)\s*(?:/|per\s+|an?\s+)\s*(?:hour|hr)\b", re.I)
PLUS_SUPER = re.compile(r"\+\s*super", re.I)
PACKAGE = re.compile(r"\b(package|inclusive of super|incl\.? super|total remuneration|TRP)\b", re.I)
OTE = re.compile(r"\bOTE\b", re.I)

WORKING_DAYS = 220
WEEK_HOURS = 38
DEFAULT_SUPER = 0.12


def _num(raw: str, k: str | None) -> int:
    v = float(raw.replace(",", "").replace(".", "") if "," in raw or (
        "." in raw and len(raw.split(".")[-1]) == 3) else raw.replace(",", ""))
    if k:
        v *= 1000
    if v < 1000:
        v *= 1000
    return int(v)


def _has_money_marker(text: str, m: re.Match, num: int, k: int) -> bool:
    """A bare 2-3 digit number is not a salary. "a team of 200 engineers" and
    "founded in 2019" (which matches as 201) both parsed as six-figure STATED
    salaries. Require a real money marker: a k suffix, a thousands separator,
    or a leading $."""
    if m.group(k):
        return True
    if "," in m.group(num) or "." in m.group(num):
        return True
    return text[:m.start(num)].rstrip().endswith("$")


def parse(text: str | None, super_rate: float = DEFAULT_SUPER) -> dict:
    out = {
        "base_salary_min": None, "base_salary_max": None, "super_included": None,
        "super_rate": super_rate, "total_package_min": None, "ote": None,
        "currency": "AUD", "period": "ANNUAL", "confidence": "ABSENT",
    }
    if not text:
        return out

    if m := DAILY.search(text):
        rate = int(m.group(1))
        out.update(period="DAILY", base_salary_min=rate * WORKING_DAYS,
                   confidence="INFERRED", super_included=False)
        out["total_package_min"] = int(out["base_salary_min"] * (1 + super_rate))
        return out

    if m := HOURLY.search(text):
        rate = float(m.group(1))
        out.update(period="HOURLY", base_salary_min=int(rate * WEEK_HOURS * 46),
                   confidence="INFERRED", super_included=False)
        out["total_package_min"] = int(out["base_salary_min"] * (1 + super_rate))
        return out

    lo = hi = None
    for m in RANGE.finditer(text):
        if _has_money_marker(text, m, 1, 2) or _has_money_marker(text, m, 3, 4):
            lo, hi = _num(m.group(1), m.group(2)), _num(m.group(3), m.group(4))
            break
    if lo is None:
        for m in SINGLE.finditer(text):
            if not _has_money_marker(text, m, 1, 2):
                continue
            v = _num(m.group(1), m.group(2))
            if 30000 <= v <= 500000:
                lo = v
            break

    if lo is None:
        return out

    included = bool(PACKAGE.search(text)) and not PLUS_SUPER.search(text)
    out.update(base_salary_min=lo, base_salary_max=hi, super_included=included,
               confidence="STATED")
    out["total_package_min"] = lo if included else int(lo * (1 + super_rate))
    if OTE.search(text):
        out["ote"] = hi or lo
        out["base_salary_min"] = None if hi is None else lo
    return out
