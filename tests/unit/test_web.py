"""Pure-function and template-render tests for the dashboard. No DB, no network.

The routes themselves are SQL against a live database and are covered by the
Phase 9 verification steps, not here. What is testable offline is the one piece
of real logic (syd) and whether every template can actually be rendered from
the context its route passes -- which is the "500 because a template references
a key the route didn't pass" failure the guide warns about.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from job_agent.web.routes import as_dict, detail_str, syd

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "job_agent" / "web" / "templates"


# --------------------------------------------------------------- syd()

def test_syd_none_passes_through():
    assert syd(None) is None


def test_syd_treats_naive_as_utc():
    assert syd(datetime(2025, 6, 1, 0, 0)) == syd(datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc))


def test_syd_uses_named_zone_not_a_fixed_offset():
    """Sydney is +10 in June (AEST) and +11 in January (AEDT). A hardcoded
    +10:00 is wrong for roughly half the year -- the guide's "off by an hour
    twice a year" bug."""
    winter = syd(datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc))
    summer = syd(datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc))
    assert winter == "2025-06-01 10:00"
    assert summer == "2025-01-01 11:00"


# --------------------------------------------------------------- jsonb guards

@pytest.mark.parametrize("value,expected", [
    ({"confidence": "STATED"}, {"confidence": "STATED"}),
    ('{"confidence": "STATED"}', {"confidence": "STATED"}),
    (None, {}),
    ("", {}),
    ("not json", {}),
    ("[1,2]", {}),
])
def test_as_dict_accepts_dict_or_str(value, expected):
    """jsonb arrives as dict or str depending on driver codec registration."""
    assert as_dict(value) == expected


def test_detail_str_does_not_double_encode():
    assert detail_str({"layer": 3}, 200) == '{"layer": 3}'
    assert detail_str('{"layer": 3}', 200) == '{"layer": 3}'
    assert len(detail_str({"x": "y" * 500}, 40)) == 40


# --------------------------------------------------------------- templates

def env() -> Environment:
    # StrictUndefined so a key the route forgot to pass fails here, loudly,
    # instead of rendering as an empty string in production.
    return Environment(loader=FileSystemLoader(str(TEMPLATES)), undefined=StrictUndefined)


PAGE = {"page": "overview", "dry_run": True}
STATS = [{"label": "job groups", "value": 12}]
HEARTBEATS = [{"worker": "worker-1", "last_beat_sydney": "2025-06-01 10:00", "age_s": 12}]
SOURCES = [{"key": "greenhouse", "circuit_state": "CLOSED",
            "consecutive_failures": 0, "postings": 40}]
JOBS = [{"id": 1, "title": "Graduate Engineer", "company": "Acme",
         "location_class": "SYDNEY_CBD", "posted_sydney": "2025-06-01 10:00",
         "status": "READY", "screen_reason": None}]
JOB = {"title": "Graduate Engineer", "company": "Acme", "location_class": "SYDNEY_CBD",
       "posted_sydney": "2025-06-01 10:00", "posted_at_confidence": "EXACT",
       "status": "READY", "screen_reason": None}
POSTINGS = [{"source": "greenhouse", "match_layer": 1, "probable_duplicate": False,
             "canonical_url": "https://example.test/j/1"}]
EVENTS = [{"at_sydney": "2025-06-01 10:00", "entity_type": "job_group", "entity_id": 1,
           "actor": "dedupe", "action": "group_created", "detail": '{"layer": 1}'}]
COLUMNS = [{"label": "READY", "count": 3}]
WORK = [{"kind": "poll_source", "state": "PENDING", "count": 1,
         "oldest_sydney": "2025-06-01 10:00"}]

CONTEXTS = {
    "base.html": PAGE,
    "_stats.html": {"stats": STATS},
    "overview.html": {**PAGE, "stats": STATS, "heartbeats": HEARTBEATS, "sources": SOURCES},
    "jobs.html": {**PAGE, "jobs": JOBS, "status": "READY", "query": "grad"},
    "_jobs.html": {"jobs": JOBS},
    "job_detail.html": {**PAGE, "job": JOB, "postings": POSTINGS,
                        "work_rights": {"classification": "COMPATIBLE",
                                        "clause_verbatim": "full working rights"},
                        "clearance": {"strength": "PREFERRED", "clause_verbatim": None},
                        "salary": {"confidence": "STATED", "base_salary_min": 90000,
                                   "base_salary_max": 110000, "total_package_min": 100800,
                                   "period": "ANNUAL"},
                        "events": EVENTS, "description": "Some description"},
    "pipeline.html": {**PAGE, "columns": COLUMNS, "work": WORK},
    "_pipeline.html": {"columns": COLUMNS, "work": WORK},
    "events.html": {**PAGE, "events": EVENTS},
    "_events.html": {"events": EVENTS},
}


def test_every_template_has_a_context():
    on_disk = {p.name for p in TEMPLATES.glob("*.html")}
    assert on_disk == set(CONTEXTS), "a template was added without a render test"


@pytest.mark.parametrize("name", sorted(CONTEXTS))
def test_template_renders(name):
    html = env().get_template(name).render(**CONTEXTS[name])
    assert html.strip()


@pytest.mark.parametrize("name", sorted(CONTEXTS))
def test_template_renders_when_every_collection_is_empty(name):
    """Every list gets an {% else %} branch; a fresh database hits all of them."""
    empty = {k: ([] if isinstance(v, list) else v) for k, v in CONTEXTS[name].items()}
    for optional in ("work_rights", "clearance"):
        if optional in empty:
            empty[optional] = None
    if "salary" in empty:
        empty["salary"] = {}
    html = env().get_template(name).render(**empty)
    assert html.strip()


# ------------------------------------------------- HTMX wiring regressions

@pytest.mark.parametrize("page,partial,url", [
    ("overview.html", "_stats.html", "/partials/stats"),
    ("events.html", "_events.html", "/partials/events"),
])
def test_outer_html_partial_is_not_double_wrapped(page, partial, url):
    """These two poll with hx-swap="outerHTML", so the partial carries its own
    wrapper. The page must include it rather than wrap it again: two nested
    elements both holding hx-get means two pollers, and the first swap nests
    the replacement inside the stale wrapper."""
    page_html = env().get_template(page).render(**CONTEXTS[page])
    partial_html = env().get_template(partial).render(**CONTEXTS[partial])
    assert partial_html.count(f'hx-get="{url}"') == 1
    assert page_html.count(f'hx-get="{url}"') == 1


def test_jobs_search_input_is_well_formed():
    """Guarding a stray '=' that made this <input name="q" ="title contains…">."""
    html = env().get_template("jobs.html").render(**CONTEXTS["jobs.html"])
    assert 'placeholder="title contains' in html
    assert ' ="' not in html


def test_htmx_is_served_locally_not_from_a_cdn():
    html = env().get_template("base.html").render(**PAGE)
    assert '/static/htmx.min.js' in html
    assert "unpkg.com" not in html
    assert (TEMPLATES.parent / "static" / "htmx.min.js").is_file()
