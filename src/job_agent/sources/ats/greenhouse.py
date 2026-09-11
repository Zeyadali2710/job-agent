from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import httpx
from selectolax.parser import HTMLParser

from job_agent.sources.base import NormalisedPosting, RawPosting, SourceAdapter, TokenBucket
from job_agent.sources.urls import canonicalise

API = "https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true"


class GreenhouseAdapter(SourceAdapter):
    key = "greenhouse"
    platform = "greenhouse"
    rate = TokenBucket(rate=2.0, capacity=4)

    async def fetch(self, tenant: str) -> AsyncIterator[RawPosting]:
        await self.rate.take()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(API.format(tenant=tenant),
                            headers={"User-Agent": "job-agent/1.0 (personal job search)"})
            r.raise_for_status()
            for job in r.json().get("jobs", []):
                yield RawPosting(
                    source_job_id=str(job["id"]),
                    url=job.get("absolute_url"),
                    payload=job,
                )

    def normalise(self, raw: RawPosting, tenant: str) -> NormalisedPosting:
        j = raw.payload
        posted_raw = j.get("first_published") or j.get("updated_at")
        posted_at, conf = None, "UNKNOWN"
        if posted_raw:
            posted_at = datetime.fromisoformat(posted_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
            conf = "EXACT" if j.get("first_published") else "INFERRED"
        content = j.get("content") or ""
        text = HTMLParser(content.replace("&lt;", "<").replace("&gt;", ">")).text(separator="\n")
        abs_url = j.get("absolute_url")
        if abs_url and str(j["id"]) not in abs_url:
            abs_url = None
        return NormalisedPosting(
            source_job_id=str(j["id"]),
            title=j.get("title", "").strip(),
            description=text.strip() or None,
            location_raw=(j.get("location") or {}).get("name"),
            posted_at=posted_at,
            posted_at_confidence=conf,
            posted_at_raw=posted_raw,
            canonical_url=canonicalise(abs_url),
            ats_platform="greenhouse",
            ats_tenant=tenant,
            ats_job_id=str(j["id"]),
            company_name=(j.get("company_name") or tenant),
        )
