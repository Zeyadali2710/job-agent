"""Outbox, card rendering, and Discord failure handling.

No database and no network: the Discord tests drive a real httpx.AsyncClient
through httpx.MockTransport, so 429/500/connect-error handling is exercised
deterministically rather than left to manual testing.
"""
from __future__ import annotations

import httpx
import pytest

from job_agent.notifications import cards, discord
from job_agent.notifications.outbox import (
    BACKOFF_MINUTES, MAX_ATTEMPTS, backoff_minutes, is_dead, redact,
)

WEBHOOK = "https://discord.com/api/webhooks/123456/tOkEn-SeCrEt"


# ------------------------------------------------------------------ redaction

@pytest.mark.parametrize("text", [
    f"HTTPStatusError for url '{WEBHOOK}'",
    f"ConnectError posting to {WEBHOOK}?wait=true",
    f"https://discordapp.com/api/webhooks/9/xyz and {WEBHOOK}",
])
def test_redact_removes_the_webhook_token(text):
    out = redact(text)
    assert "tOkEn-SeCrEt" not in out
    assert "xyz" not in out
    assert "<redacted>" in out


def test_redact_keeps_the_rest_of_the_message():
    assert redact(f"discord 500: {WEBHOOK}").startswith("discord 500:")


def test_redact_passes_through_empty():
    assert redact(None) is None
    assert redact("") == ""


# -------------------------------------------------------------------- backoff

def test_backoff_follows_the_documented_schedule():
    assert [backoff_minutes(a) for a in (1, 2, 3, 4, 5)] == list(BACKOFF_MINUTES)


def test_backoff_clamps_past_the_last_interval():
    """attempts runs to 8 but the schedule has 5 entries; later attempts must
    hold at 6h rather than IndexError."""
    assert backoff_minutes(6) == backoff_minutes(8) == BACKOFF_MINUTES[-1]


def test_dead_lands_exactly_on_the_check_constraint():
    """notifications.attempts has CHECK (attempts <= 8): the row must go DEAD
    at 8, never be written with 9."""
    assert not is_dead(MAX_ATTEMPTS - 1)
    assert is_dead(MAX_ATTEMPTS)
    assert MAX_ATTEMPTS == 8


# ---------------------------------------------------------------------- cards

def test_score_colour_bands():
    assert cards.score_colour(85) == cards.GREEN
    assert cards.score_colour(84) == cards.YELLOW
    assert cards.score_colour(70) == cards.YELLOW
    assert cards.score_colour(69) == cards.GREY
    assert cards.score_colour(None) == cards.GREY


def test_daily_summary_card_hides_healthy_zeroes():
    payload = {"date": "2025-06-13", "new_groups": 5, "ready": 2, "review": 1,
               "screened_out_24h": 3, "new_postings": 20, "queued": 0,
               "dead": 0, "circuits_open": 0, "top_screen_reasons": []}
    embed = cards.render("DAILY_SUMMARY", payload)["embeds"][0]
    names = [f["name"] for f in embed["fields"]]
    assert "Dead work items" not in names and "Circuits open" not in names
    assert embed["color"] == cards.BLUE


def test_daily_summary_card_surfaces_trouble():
    payload = {"date": "2025-06-13", "new_groups": 0, "ready": 0, "review": 0,
               "screened_out_24h": 0, "new_postings": 0, "queued": 0,
               "dead": 3, "circuits_open": 1,
               "top_screen_reasons": [{"reason": "SENIORITY_TOO_HIGH", "count": 4}]}
    embed = cards.render("DAILY_SUMMARY", payload)["embeds"][0]
    names = [f["name"] for f in embed["fields"]]
    assert "Dead work items" in names and "Circuits open" in names
    assert embed["color"] == cards.YELLOW
    reasons = next(f for f in embed["fields"] if f["name"] == "Top screen-out reasons")
    assert "SENIORITY_TOO_HIGH — 4" in reasons["value"]


def test_high_match_card_omits_sections_it_has_no_data_for():
    """Scoring is Phase 11. Until then the card must render without `why`,
    `concerns` or `score` rather than raising."""
    embed = cards.render("HIGH_MATCH_JOB", {"title": "Grad Engineer",
                                            "company": "Acme"})["embeds"][0]
    names = [f["name"] for f in embed["fields"]]
    assert "Why it matches" not in names and "Score" not in names
    assert embed["color"] == cards.GREY


def test_high_match_card_deep_links_to_the_dashboard():
    embed = cards.render("HIGH_MATCH_JOB",
                         {"title": "X", "job_group_id": 42, "score": 91},
                         hostname="job-agent.tail1.ts.net")["embeds"][0]
    links = next(f for f in embed["fields"] if f["name"] == "Links")
    assert "https://job-agent.tail1.ts.net/jobs/42" in links["value"]
    assert embed["color"] == cards.GREEN


def test_unknown_kind_still_renders():
    """Phases 11-15 kinds have no producer yet; an unrendered notification is
    worse than a plain one."""
    embed = cards.render("APPLICATION_FAILED", {"reason": "timeout"})["embeds"][0]
    assert embed["fields"]


def test_long_field_values_are_clipped_to_discord_limits():
    embed = cards.render("SYSTEM_ERROR",
                         {"component": "x", "source": "y", "detail": "z" * 5000})["embeds"][0]
    assert all(len(f["value"]) <= cards.MAX_FIELD_VALUE for f in embed["fields"])


# -------------------------------------------------------------- discord client

def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_success_returns_status():
    async with client_for(lambda r: httpx.Response(204)) as c:
        assert await discord.send(WEBHOOK, {"embeds": []}, client=c) == 204


@pytest.mark.asyncio
async def test_server_error_is_transient():
    async with client_for(lambda r: httpx.Response(500)) as c:
        with pytest.raises(discord.Transient):
            await discord.send(WEBHOOK, {"embeds": []}, client=c)


@pytest.mark.asyncio
async def test_client_error_is_permanent():
    """A deleted webhook is 404 and will never succeed."""
    async with client_for(lambda r: httpx.Response(404, text="Unknown Webhook")) as c:
        with pytest.raises(discord.Permanent):
            await discord.send(WEBHOOK, {"embeds": []}, client=c)


@pytest.mark.asyncio
async def test_connect_error_is_transient():
    def boom(request):
        raise httpx.ConnectError("discord unreachable", request=request)
    async with client_for(boom) as c:
        with pytest.raises(discord.Transient):
            await discord.send(WEBHOOK, {"embeds": []}, client=c)


@pytest.mark.asyncio
async def test_429_waits_then_retries_once():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(204)

    async with client_for(handler) as c:
        assert await discord.send(WEBHOOK, {"embeds": []}, client=c) == 204
    assert len(calls) == 2, "should have retried exactly once after the 429"


@pytest.mark.asyncio
async def test_429_asking_for_too_long_gives_the_row_back():
    """Holding a worker for a multi-minute Retry-After wastes it; the outbox
    backoff is the right place for that wait."""
    async with client_for(lambda r: httpx.Response(
            429, json={"retry_after": discord.MAX_SLEEP + 10})) as c:
        with pytest.raises(discord.RateLimited) as exc:
            await discord.send(WEBHOOK, {"embeds": []}, client=c)
    assert exc.value.retry_after == discord.MAX_SLEEP + 10


@pytest.mark.asyncio
async def test_429_falls_back_to_the_header_when_the_body_has_no_json():
    async with client_for(lambda r: httpx.Response(
            429, text="rate limited", headers={"Retry-After": "999"})) as c:
        with pytest.raises(discord.RateLimited) as exc:
            await discord.send(WEBHOOK, {"embeds": []}, client=c)
    assert exc.value.retry_after == 999.0


@pytest.mark.asyncio
async def test_repeated_429_does_not_loop_forever():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"retry_after": 0.01})

    async with client_for(handler) as c:
        with pytest.raises(discord.RateLimited):
            await discord.send(WEBHOOK, {"embeds": []}, client=c)
    assert len(calls) == 2, "one retry, then give up -- never an unbounded loop"


def test_batch_cannot_outlast_the_idle_transaction_timeout():
    """The notifier holds one transaction open across a whole batch of sends,
    and 00-bootstrap.sql sets idle_in_transaction_session_timeout='120s' on
    every role -- a session waiting on an HTTP response is idle. If this ever
    exceeds the limit Postgres kills the transaction mid-batch and rolls back
    every send in it, so raising BATCH or the timeouts must fail here first.
    """
    from job_agent.workers import notifier
    worst_case = notifier.BATCH * (discord.TIMEOUT + discord.MAX_SLEEP + discord.TIMEOUT)
    assert worst_case < 120, f"worst-case batch is {worst_case}s against a 120s limit"
