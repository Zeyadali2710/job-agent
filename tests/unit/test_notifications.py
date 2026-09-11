"""Pure-function tests for the daily summary payload. No DB, no network."""
from datetime import datetime, timedelta, timezone

import pytest

from job_agent.notifications.summary import COUNT_FIELDS, dedupe_key, summarise

STATS = {"new_groups": 12, "ready": 3, "review": 2, "screened_out_24h": 7,
         "new_postings": 40, "queued": 1, "dead": 0, "circuits_open": 0}
REASONS = [{"reason": "SENIORITY_TOO_HIGH", "count": 4},
           {"reason": "LOCATION_REQUIRES_RELOCATION", "count": 3}]


def test_dedupe_key_is_one_per_utc_day():
    at_19_sydney_aest = datetime(2025, 6, 13, 9, 0, tzinfo=timezone.utc)
    at_19_sydney_aedt = datetime(2025, 1, 13, 8, 0, tzinfo=timezone.utc)
    assert dedupe_key(at_19_sydney_aest) == "daily_summary:2025-06-13"
    assert dedupe_key(at_19_sydney_aedt) == "daily_summary:2025-01-13"


def test_dedupe_key_is_stable_across_the_misfire_window():
    """misfire_grace_time is 900s, so a late fire must still claim the same
    key -- otherwise a delayed scheduler sends two summaries for one day."""
    on_time = datetime(2025, 6, 13, 9, 0, tzinfo=timezone.utc)
    late = on_time + timedelta(seconds=900)
    assert dedupe_key(on_time) == dedupe_key(late)


def test_dedupe_key_normalises_a_non_utc_input():
    syd = timezone(timedelta(hours=10))
    assert dedupe_key(datetime(2025, 6, 13, 19, 0, tzinfo=syd)) == "daily_summary:2025-06-13"


def test_summarise_carries_every_count():
    p = summarise(STATS, REASONS, now=datetime(2025, 6, 13, 9, 0, tzinfo=timezone.utc))
    assert p["date"] == "2025-06-13"
    for f in COUNT_FIELDS:
        assert p[f] == STATS[f], f
    assert p["top_screen_reasons"] == [
        {"reason": "SENIORITY_TOO_HIGH", "count": 4},
        {"reason": "LOCATION_REQUIRES_RELOCATION", "count": 3},
    ]


def test_summarise_is_json_serialisable():
    """The payload goes into a jsonb column via json.dumps; a stray Decimal or
    Row from the driver would fail there, at 19:00, in the worker."""
    import json
    json.dumps(summarise(STATS, REASONS))


def test_summarise_coerces_driver_numerics():
    """count() comes back as int, but a driver returning Decimal must not put
    a non-serialisable value in the payload."""
    from decimal import Decimal
    stats = {k: Decimal(v) for k, v in STATS.items()}
    p = summarise(stats, [{"reason": "X", "count": Decimal(2)}])
    assert all(isinstance(p[f], int) for f in COUNT_FIELDS)
    assert isinstance(p["top_screen_reasons"][0]["count"], int)


def test_summarise_on_an_empty_day():
    zero = dict.fromkeys(COUNT_FIELDS, 0)
    p = summarise(zero, [])
    assert p["top_screen_reasons"] == []
    assert p["new_groups"] == 0


def test_missing_count_is_not_silently_zero():
    """A renamed SQL alias must fail loudly here, not send a summary that
    quietly reports zero new jobs."""
    incomplete = {k: v for k, v in STATS.items() if k != "ready"}
    with pytest.raises(KeyError):
        summarise(incomplete, [])


def test_handler_is_registered_for_the_kind_the_scheduler_enqueues():
    """The whole point: scheduler/main.py enqueues 'daily_summary' and
    loop.py must have a handler for it, or every run dies after 5 attempts."""
    from job_agent.workers.loop import HANDLERS
    assert "daily_summary" in HANDLERS
