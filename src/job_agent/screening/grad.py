"""Graduate-program detection. Architecture 6.3.

The full spec also wants graduation_window, wam_gpa_requirement,
assessment_stages and friends -- those need the description read properly and
belong to LLM extraction. This is the deterministic half: is this a structured
graduate program, and which intake year.

The distinction matters for scoring. seniority.classify already returns
GRADUATE for a title like "Graduate Software Engineer" -- a grad-LEVEL role.
This flags a grad PROGRAM: a cohort with an intake, a deadline and assessment
stages. 6.3 gives them their own dashboard view precisely because "deadlines,
not freshness, are what matter for them".

Title-driven, like seniority and for the same reason location is: descriptions
say "recent graduates are encouraged to apply" and "graduate of an accredited
university" as boilerplate, constantly. The description is consulted only for
the intake year, and only once the title has already established this is a
program -- the same narrow exception location.py makes for days_in_office.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# Australian grad recruiting has its own vocabulary. "Vacation Program" and
# "Vacationer" are the standard AU terms for penultimate-year internships and
# would be missed entirely by a US-shaped word list.
PROGRAM = re.compile(
    r"\b("
    r"grad(uate)?\s*(program|programme|scheme|intake|pool)"
    r"|intern(ship)?\s*(program|programme)"
    r"|vacation(er)?\s*(program|programme)?"
    r"|summer\s*(intern(ship)?|analyst|scholar)"
    r"|cadet(ship)?\s*(program|programme)?"
    r"|campus\s*(hire|recruit(ing|ment)?)"
    r"|early\s*careers?\s*(program|programme)?"
    r"|trainee\s*(program|programme)"
    r")\b", re.I)

YEAR = re.compile(r"\b(20\d{2})\b")

# A year plus an entry-level marker is a cohort intake even without the word
# "program": "2026 Software Engineering Intern" is a structured summer cohort
# with a deadline, whereas a bare "Software Engineer Intern" is a one-off. The
# year must fall inside the intake window for this to fire, which is what keeps
# it from matching an archive label or a founding year.
ENTRY_MARKER = re.compile(
    r"\b(intern(ship)?|graduate|grad|cadet|trainee|vacation(er)?)\b", re.I)


def _intake_year(text: str | None, now: datetime) -> int | None:
    """A year in a grad-program title is an intake year. Bounded to a sane
    window so a company's founding year in the description cannot become one --
    the same failure the postcode rule guards against in location.py."""
    if not text:
        return None
    lo, hi = now.year - 1, now.year + 2
    for m in YEAR.finditer(text):
        year = int(m.group(1))
        if lo <= year <= hi:
            return year
    return None


def detect(title: str | None, description: str | None = None,
           now: datetime | None = None) -> tuple[bool, int | None]:
    """Return (is_grad_program, intake_year)."""
    now = now or datetime.now(timezone.utc)
    t = title or ""
    title_year = _intake_year(t, now)
    cohort = bool(title_year) and bool(ENTRY_MARKER.search(t))
    if not (PROGRAM.search(t) or cohort):
        return False, None
    # Title first; fall back to the description only now that the title has
    # already proven this is a program.
    return True, title_year or _intake_year(description, now)
