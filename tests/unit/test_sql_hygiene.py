"""Repo-level checks for SQL written inside sqlalchemy text().

These catch classes of mistake that unit tests structurally cannot: they are
properties of how text() and asyncpg read a string, not of any function's
behaviour, so they only surface when a container tries to run the query.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "job_agent"
PY_FILES = sorted(SRC.rglob("*.py"))

# A colon-prefixed name, not preceded by another colon (which would be a
# PostgreSQL ::cast) or a word character (a Python dict key, a time literal).
BIND = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)")


def test_no_sql_comment_declares_a_stray_bind_parameter():
    """text() regex-scans the entire string and does not parse SQL comments, so
    a colon-prefixed name written inside a `--` comment becomes a *required*
    bind parameter. Cost one deploy cycle: an explanatory comment mentioning
    ":p IS NULL" made every render fail with "A value is required for bind
    parameter 'p'".
    """
    offenders = []
    for f in PY_FILES:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("--") and BIND.search(stripped):
                offenders.append(f"{f.relative_to(SRC)}:{i}: {stripped}")
    assert not offenders, "colon-prefixed name inside SQL comment:\n" + "\n".join(offenders)


def test_optional_bind_parameters_are_cast_before_being_compared_to_null():
    """asyncpg needs a type for every parameter. `WHERE (:x IS NULL OR col = :x)`
    raises AmbiguousParameterError before the query runs, and casting only the
    second use does not help the first -- this took down the worker and the
    renderer together.
    """
    offenders = []
    for f in PY_FILES:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("--", "#")):
                continue
            if re.search(r"(?<![:\w]):[a-z_][a-z0-9_]*\s+IS\s+NULL", stripped, re.I):
                offenders.append(f"{f.relative_to(SRC)}:{i}: {stripped}")
    assert not offenders, (
        "uncast bind compared to NULL (use CAST(:x AS type) IS NULL):\n"
        + "\n".join(offenders))
