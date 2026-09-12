"""Discord webhook client. The only place in the system that makes an outbound
request to Discord.

Architecture 7: a token bucket caps 5 messages per 2 seconds, and the server's
own X-RateLimit-Remaining / Retry-After always wins over our local estimate.
"""
from __future__ import annotations

import asyncio
import re

import httpx

from job_agent.core.ratelimit import TokenBucket

# 5 messages per 2 seconds.
RATE = TokenBucket(rate=2.5, capacity=5)

# These two are not free parameters. The notifier claims a batch with
# FOR UPDATE SKIP LOCKED and holds that transaction across every send in the
# batch, and 00-bootstrap.sql sets idle_in_transaction_session_timeout='120s'
# on every role -- a session awaiting an HTTP response counts as idle. The
# worst case is BATCH * (TIMEOUT + MAX_SLEEP + TIMEOUT) and it must stay under
# 120s, or Postgres kills the transaction mid-batch and the whole batch is
# rolled back. test_batch_cannot_outlast_the_idle_transaction_timeout pins it.
TIMEOUT = 10.0
# A 429 asking for longer than this is not worth holding the worker for; give
# the row back to the outbox and let the 1m/5m/15m/1h/6h backoff handle it.
MAX_SLEEP = 5.0


# Phase 2 creates /opt/job-agent/secrets/discord_webhook holding the literal
# string PLACEHOLDER_SET_IN_PHASE_10. _read_secret only checks the file exists
# and is non-empty, so a placeholder passes: the worker would start, then every
# send would fail as an unparseable URL, be classified Transient, and burn all
# 8 attempts before going DEAD. Check the shape at startup instead.
WEBHOOK_URL_RE = re.compile(
    r"^https://(?:\w+\.)*discord(?:app)?\.com/api/webhooks/\d+/[\w-]+/?$")


def is_webhook_url(value: str) -> bool:
    return bool(WEBHOOK_URL_RE.match((value or "").strip()))

class RateLimited(RuntimeError):
    """429 asking for longer than MAX_SLEEP. Retryable, not a failure."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after:.1f}s")
        self.retry_after = retry_after


class Transient(RuntimeError):
    """5xx or a network error. Retryable."""


class Permanent(RuntimeError):
    """4xx that will never succeed on retry: a deleted webhook, a malformed
    embed. Retrying these just burns the attempt budget, but the outbox still
    caps at 8 so they end as DEAD either way."""


def _retry_after(resp: httpx.Response) -> float:
    """Discord sends retry_after in the JSON body (seconds, fractional) and
    also as a Retry-After header. Prefer the body; it is more precise."""
    try:
        body = resp.json()
        if isinstance(body, dict) and "retry_after" in body:
            return float(body["retry_after"])
    except (ValueError, TypeError):
        pass
    try:
        return float(resp.headers.get("Retry-After", 1.0))
    except (TypeError, ValueError):
        return 1.0


async def _exhausted(resp: httpx.Response) -> None:
    """Discord tells us how many requests remain in the current window. When
    it hits zero, wait out the window instead of earning a 429."""
    if resp.headers.get("X-RateLimit-Remaining") == "0":
        try:
            reset_after = float(resp.headers.get("X-RateLimit-Reset-After", 0))
        except (TypeError, ValueError):
            return
        if 0 < reset_after <= MAX_SLEEP:
            await asyncio.sleep(reset_after)


async def send(webhook_url: str, body: dict, client: httpx.AsyncClient | None = None) -> int:
    """POST one card. Returns the HTTP status on success.

    Raises RateLimited / Transient / Permanent -- the caller records the error
    against the outbox row and lets the backoff schedule decide what happens
    next. Never raises the webhook URL in a message this module builds itself;
    httpx exceptions may still carry it, which is why outbox.mark_failed
    redacts.
    """
    await RATE.take()
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        try:
            resp = await client.post(webhook_url, json=body)
        except httpx.HTTPError as exc:
            raise Transient(f"{type(exc).__name__}: {exc}") from exc

        if resp.status_code == 429:
            wait = _retry_after(resp)
            if wait > MAX_SLEEP:
                raise RateLimited(wait)
            await asyncio.sleep(wait)
            resp = await client.post(webhook_url, json=body)
            if resp.status_code == 429:
                raise RateLimited(_retry_after(resp))

        if resp.status_code >= 500:
            raise Transient(f"discord {resp.status_code}")
        if resp.status_code >= 400:
            raise Permanent(f"discord {resp.status_code}: {resp.text[:300]}")

        await _exhausted(resp)
        return resp.status_code
    finally:
        if owned:
            await client.aclose()
