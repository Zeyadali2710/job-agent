from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from job_agent.sources.base import NormalisedPosting, RawPosting, SourceAdapter, TokenBucket
from job_agent.sources.urls import canonicalise

API = "https://api.lever.co/v0/postings/{tenant}?mode=json"


class LeverAdapter(SourceAdapter):
    key = "lever"
    platform = "lever"
    rate = TokenBucket(rate=1.0, capacity=3)

    async def fetch(self, tenant: str) -> AsyncIterator[RawPosting]:
        await self.rate.take()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(API.format(tenant=tenant),
                            headers={"User-Agent": "job-agent/1.0 (personal job search)"})
            r.raise_for_status()
            for job in r.json():
                yield RawPosting(source_job_id=job["id"], url=job.get("hostedUrl"), payload=job)

    def normalise(self, raw: RawPosting, tenant: str) -> NormalisedPosting:
        j = raw.payload
        created = j.get("createdAt")
        posted_at = (
            datetime.fromtimestamp(created / 1000, tz=timezone.utc) if created else None
        )
        cats = j.get("categories") or {}
        return NormalisedPosting(
            source_job_id=j["id"],
            title=(j.get("text") or "").strip(),
            description=j.get("descriptionPlain") or j.get("description"),
            location_raw=cats.get("location"),
            posted_at=posted_at,
            posted_at_confidence="EXACT" if created else "UNKNOWN",
            posted_at_raw=str(created) if created else None,
            canonical_url=canonicalise(j.get("hostedUrl")),
            ats_platform="lever",
            ats_tenant=tenant,
            ats_job_id=j["id"],
            company_name=tenant,
            employment_type=_commitment(cats.get("commitment")),
        )


def _commitment(v: str | None) -> str | None:
    if not v:
        return None
    v = v.lower()
    if "intern" in v:
        return "INTERNSHIP"
    if "full" in v:
        return "FULL_TIME"
    if "part" in v:
        return "PART_TIME"
    if "contract" in v or "temp" in v:
        return "CONTRACT"
    return None
