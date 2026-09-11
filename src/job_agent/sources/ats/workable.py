from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import AsyncIterator

import httpx
from selectolax.parser import HTMLParser

from job_agent.sources.base import NormalisedPosting, RawPosting, SourceAdapter, TokenBucket
from job_agent.sources.urls import canonicalise

API = "https://apply.workable.com/api/v1/widget/accounts/{tenant}?details=true"


def _loc_key(job: dict) -> str:
    """One Workable requisition can appear as several flat entries, one per
    location, sharing the same shortcode and URL. Disambiguate by location
    so each becomes a distinct posting instead of silently overwriting the
    last one inserted."""
    parts = [job.get("city"), job.get("state"), job.get("country")]
    key = "-".join(p for p in parts if p).lower().replace(" ", "-")
    if not key:
        key = "remote" if job.get("telecommuting") else "unspecified"
    return key


def _employment_type(v: str | None) -> str | None:
    if not v:
        return None
    v = v.lower()
    if "full" in v:
        return "FULL_TIME"
    if "part" in v:
        return "PART_TIME"
    if "intern" in v:
        return "INTERNSHIP"
    if "contract" in v or "temporary" in v:
        return "CONTRACT"
    if "casual" in v:
        return "CASUAL"
    return None


def _location_raw(job: dict) -> str | None:
    parts = [job.get("city"), job.get("state"), job.get("country")]
    text = ", ".join(p for p in parts if p)
    if not text and job.get("telecommuting"):
        return "Remote"
    return text or None


class WorkableAdapter(SourceAdapter):
    key = "workable"
    platform = "workable"
    rate = TokenBucket(rate=1.0, capacity=3)
    trust_url_for_hash = False  # shortcode + url are reused across location entries

    async def fetch(self, tenant: str) -> AsyncIterator[RawPosting]:
        await self.rate.take()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(API.format(tenant=tenant),
                            headers={"User-Agent": "job-agent/1.0 (personal job search)"})
            if r.status_code == 404:
                return  # some accounts 404 rather than return an empty jobs list
            r.raise_for_status()
            for job in r.json().get("jobs", []):
                shortcode = job.get("shortcode") or job.get("code") or ""
                if not shortcode:
                    continue
                job_id = f"{shortcode}:{_loc_key(job)}"
                yield RawPosting(source_job_id=job_id, url=job.get("url"), payload=job)

    def normalise(self, raw: RawPosting, tenant: str) -> NormalisedPosting:
        j = raw.payload
        shortcode = j.get("shortcode") or j.get("code") or ""

        posted_raw = j.get("published_on") or j.get("created_at")
        posted_at, conf = None, "UNKNOWN"
        if posted_raw:
            posted_at = datetime.combine(date.fromisoformat(posted_raw), datetime.min.time(),
                                         tzinfo=timezone.utc)
            conf = "DAY" if j.get("published_on") else "INFERRED"

        desc_html = j.get("description") or ""
        description = HTMLParser(desc_html).text(separator="\n").strip() or None

        return NormalisedPosting(
            source_job_id=raw.source_job_id,
            title=(j.get("title") or "").strip(),
            description=description,
            location_raw=_location_raw(j),
            posted_at=posted_at,
            posted_at_confidence=conf,
            posted_at_raw=posted_raw,
            canonical_url=canonicalise(j.get("url")),
            ats_platform="workable",
            ats_tenant=tenant,
            ats_job_id=shortcode,          # deliberately NOT the composite — layer-2 dedup
                                            # lookup is a non-unique index, multiple locations
                                            # legitimately sharing one ats_job_id is correct
            company_name=tenant,
            employment_type=_employment_type(j.get("employment_type")),
        )
