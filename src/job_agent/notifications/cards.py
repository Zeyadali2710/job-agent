"""Discord card rendering. Pure: payload in, webhook body out. No I/O.

Architecture 7 keys the embed colour to the score band and builds "why it
matches" from score features. Nothing populates app.job_scores yet -- that is
Phase 11 -- so HIGH_MATCH_JOB renders from whatever the payload carries and
simply omits the sections it has no data for. The renderer is finished; its
producer is not.
"""
from __future__ import annotations

from collections.abc import Mapping

GREEN = 0x3FB950
YELLOW = 0xD29922
GREY = 0x8B93A1
RED = 0xF85149
BLUE = 0x58A6FF

MAX_FIELD_VALUE = 1024      # Discord's per-field limit
MAX_EMBED_FIELDS = 25
MAX_CONTENT = 2000


def score_colour(score: float | int | None) -> int:
    """Architecture 7: green >= 85, yellow 70-84, grey below."""
    if score is None:
        return GREY
    if score >= 85:
        return GREEN
    if score >= 70:
        return YELLOW
    return GREY


def _clip(s: str, limit: int = MAX_FIELD_VALUE) -> str:
    s = str(s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _field(name: str, value, inline: bool = True) -> dict:
    return {"name": name, "value": _clip(value if value not in (None, "") else "—"),
            "inline": inline}


def _bullets(items, limit: int = 5) -> str | None:
    items = [str(i) for i in (items or [])][:limit]
    return "\n".join(f"• {i}" for i in items) if items else None


def dashboard_url(hostname: str, gid: int | None) -> str | None:
    if not hostname or gid is None:
        return None
    return f"https://{hostname}/jobs/{gid}"


def _links(payload: Mapping, hostname: str) -> str | None:
    parts = []
    dash = dashboard_url(hostname, payload.get("job_group_id"))
    if dash:
        parts.append(f"[Open in dashboard]({dash})")
    if payload.get("url"):
        parts.append(f"[Job posting]({payload['url']})")
    return "  ·  ".join(parts) if parts else None


def high_match_job(payload: Mapping, hostname: str = "") -> dict:
    score = payload.get("score")
    fields = [
        _field("Company", payload.get("company")),
        _field("Location", payload.get("location_class")),
        _field("Type", payload.get("employment_type")),
        _field("Salary", payload.get("salary_text")),
        _field("Posted", payload.get("posted_sydney")),
    ]
    if score is not None:
        fields.append(_field("Score", score))
    why = _bullets(payload.get("why"))
    if why:
        fields.append(_field("Why it matches", why, inline=False))
    concerns = _bullets(payload.get("concerns"))
    if concerns:
        fields.append(_field("Concerns", concerns, inline=False))
    links = _links(payload, hostname)
    if links:
        fields.append(_field("Links", links, inline=False))
    return {
        "title": _clip(payload.get("title") or "New match", 256),
        "color": score_colour(score),
        "fields": fields[:MAX_EMBED_FIELDS],
    }


def daily_summary(payload: Mapping, hostname: str = "") -> dict:
    fields = [
        _field("New jobs (24h)", payload.get("new_groups")),
        _field("Ready", payload.get("ready")),
        _field("Needs review", payload.get("review")),
        _field("Screened out (24h)", payload.get("screened_out_24h")),
        _field("New postings (24h)", payload.get("new_postings")),
        _field("Queued", payload.get("queued")),
    ]
    # Only surface the alarming counts when they are actually non-zero, so a
    # healthy day's card stays short enough to read on a phone.
    if payload.get("dead"):
        fields.append(_field("Dead work items", payload["dead"]))
    if payload.get("circuits_open"):
        fields.append(_field("Circuits open", payload["circuits_open"]))
    reasons = _bullets(
        [f"{r['reason']} — {r['count']}" for r in payload.get("top_screen_reasons", [])])
    if reasons:
        fields.append(_field("Top screen-out reasons", reasons, inline=False))
    if hostname:
        fields.append(_field("Links", f"[Open dashboard](https://{hostname}/)", inline=False))
    unhealthy = bool(payload.get("dead") or payload.get("circuits_open"))
    return {
        "title": f"Daily summary — {payload.get('date', '')}".strip(" —"),
        "color": YELLOW if unhealthy else BLUE,
        "fields": fields[:MAX_EMBED_FIELDS],
    }


def system_error(payload: Mapping, hostname: str = "") -> dict:
    fields = [_field("Component", payload.get("component")),
              _field("Source", payload.get("source"))]
    if payload.get("detail"):
        fields.append(_field("Detail", payload["detail"], inline=False))
    if hostname:
        fields.append(_field("Links", f"[Open dashboard](https://{hostname}/)", inline=False))
    return {
        "title": _clip(payload.get("title") or "System error", 256),
        "color": RED,
        "fields": fields[:MAX_EMBED_FIELDS],
    }


def generic(kind: str, payload: Mapping, hostname: str = "") -> dict:
    """Fallback for the kinds whose producers land in Phases 11-15. Renders
    whatever scalar keys the payload has rather than dropping the card: an
    unrendered notification is worse than an ugly one."""
    fields = [_field(k.replace("_", " ").capitalize(), v)
              for k, v in payload.items()
              if isinstance(v, (str, int, float, bool)) and k != "title"]
    links = _links(payload, hostname)
    if links:
        fields.append(_field("Links", links, inline=False))
    return {
        "title": _clip(payload.get("title") or kind.replace("_", " ").capitalize(), 256),
        "color": GREY,
        "fields": fields[:MAX_EMBED_FIELDS],
    }


RENDERERS = {
    "HIGH_MATCH_JOB": high_match_job,
    "DAILY_SUMMARY": daily_summary,
    "SYSTEM_ERROR": system_error,
}


def render(kind: str, payload: Mapping, hostname: str = "") -> dict:
    """Notification row -> Discord webhook body."""
    renderer = RENDERERS.get(kind)
    embed = renderer(payload, hostname) if renderer else generic(kind, payload, hostname)
    return {"embeds": [embed]}
