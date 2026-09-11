from __future__ import annotations

import asyncio
import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator


@dataclass(slots=True)
class RawPosting:
    source_job_id: str
    url: str | None
    payload: dict[str, Any]

    @property
    def payload_sha256(self) -> str:
        import json
        blob = json.dumps(self.payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(slots=True)
class NormalisedPosting:
    source_job_id: str
    title: str
    description: str | None
    location_raw: str | None
    posted_at: datetime | None
    posted_at_confidence: str
    posted_at_raw: str | None
    canonical_url: str | None
    ats_platform: str | None
    ats_tenant: str | None
    ats_job_id: str | None
    company_name: str | None
    closes_at: datetime | None = None
    employment_type: str | None = None
    salary: dict = field(default_factory=dict)


class TokenBucket:
    """Simple async rate limiter: `rate` requests per second, burst `capacity`."""

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate, self.capacity = rate, capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self.rate)
                self._tokens = 0
            else:
                self._tokens -= 1


class CircuitOpen(RuntimeError):
    pass


class SourceAdapter(ABC):
    key: str
    platform: str | None = None
    trust_url_for_hash: bool = True
    rate = TokenBucket(rate=2.0, capacity=4)

    @abstractmethod
    async def fetch(self, tenant: str) -> AsyncIterator[RawPosting]:
        """Yield immutable raw records for one tenant."""

    @abstractmethod
    def normalise(self, raw: RawPosting, tenant: str) -> NormalisedPosting:
        """Pure function: raw payload → normalised fields. No I/O, no DB."""
