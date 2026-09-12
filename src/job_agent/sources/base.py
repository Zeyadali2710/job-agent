from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

# Re-exported: the limiter moved to core in Phase 10 so the notifier could
# use it too. Adapters still import TokenBucket from here.
from job_agent.core.ratelimit import TokenBucket


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
