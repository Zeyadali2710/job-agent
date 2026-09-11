from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from job_agent.sources.base import NormalisedPosting, RawPosting, SourceAdapter, TokenBucket
from job_agent.sources.urls import canonicalise

API = "https://api.ashbyhq.com/posting-api/job-board/{tenant}?includeCompensation=true"


class AshbyAdapter(SourceAdapter):
    key = "ashby"
    platform = "ashby"
    rate = TokenBucket(rate=1.0, capacity=3)

    async def fetch(self, tenant: str) -> AsyncIterator[RawPosting]:
        await self.rate.take()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(API.format(tenant=tenant),
                            headers={"User-Agent": "job-agent/1.0 (personal job search)"})
            r.raise_for_status()
            for job in r.json().get("jobs", []):
                if not job.get("isListed", True):
                    continue                      # unlisted: not publicly advertised
                yield RawPosting(source_job_id=job["id"], url=job.get("jobUrl"), payload=job)

    def normalise(self, raw: RawPosting, tenant: str) -> NormalisedPosting:
        j = raw.payload
        pub = j.get("publishedAt")
        posted_at = (
            datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(timezone.utc)
            if pub else None
        )
        comp = j.get("compensation") or {}
        return NormalisedPosting(
            source_job_id=j["id"],
            title=(j.get("title") or "").strip(),
            description=j.get("descriptionPlain"),
            location_raw=j.get("location"),
            posted_at=posted_at,
            posted_at_confidence="EXACT" if pub else "UNKNOWN",
            posted_at_raw=pub,
            canonical_url=canonicalise(j.get("jobUrl")),
            ats_platform="ashby",
            ats_tenant=tenant,
            ats_job_id=j["id"],
            company_name=j.get("organizationName") or tenant,
            salary={"raw": comp} if comp else {},
        )
