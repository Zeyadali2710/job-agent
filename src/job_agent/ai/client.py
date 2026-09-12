"""The only place this system talks to a language model.

Architecture 9 routes tasks to model classes; the revised architecture 6
removes local inference entirely on this hardware ("no local LLM on this box"),
so every one of those routes is the Anthropic API. It also says, plainly, why
the budget code below is not optional:

    "a retry loop against a paid API is the only way this system can cost you
     real money."

Every call is recorded in app.llm_calls with a token count and a hash of the
payload, so what left the network is auditable after the fact.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import text

# Architecture 6's routing table names the model classes; these are the current
# members of those classes. Extraction is high-volume and easy, rationale is
# where quality shows.
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"

# USD per million tokens, from the published pricing for these two models.
# Converted to AUD with the fx rate in app.settings so llm_calls.cost_aud is in
# the same currency as the budget.
PRICING_USD = {
    HAIKU: {"in": 1.00, "out": 5.00},
    SONNET: {"in": 2.00, "out": 10.00},
}

# Haiku 4.5 rejects output_config.effort, and neither task wants extended
# thinking: extraction is mechanical and the rationale is three sentences.
MODEL_OPTS: dict[str, dict] = {
    HAIKU: {},
    SONNET: {"output_config": {"effort": "low"}},
}

DEFAULT_FX_AUD_PER_USD = 1.55
DEFAULT_MONTHLY_BUDGET_AUD = 20.0


class BudgetExceeded(RuntimeError):
    """The month's spend cap is reached. Not an error to retry -- the caller
    stops enriching and the pipeline carries on deterministically."""


class AIDisabled(RuntimeError):
    """ai.mode is 'off'. The original architecture offered 'local_only' as the
    zero-cloud escape hatch; with local inference removed there is no local
    mode to fall back to, so 'off' preserves the intent -- everything
    deterministic still runs, nothing is sent anywhere."""


@dataclass(frozen=True)
class Usage:
    model: str
    tokens_in: int
    tokens_out: int
    cost_aud: float


def payload_sha256(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def cost_aud(model: str, tokens_in: int, tokens_out: int, fx: float) -> float:
    p = PRICING_USD.get(model)
    if not p:
        return 0.0
    usd = (tokens_in * p["in"] + tokens_out * p["out"]) / 1_000_000
    return round(usd * fx, 6)


SETTINGS = text("SELECT key, value FROM app.settings WHERE key = ANY(:keys)")

SPENT_THIS_MONTH = text("""
SELECT coalesce(sum(cost_aud), 0) AS spent
  FROM app.llm_calls
 WHERE created_at >= date_trunc('month', now())
""")

LOG_CALL = text("""
INSERT INTO app.llm_calls
  (purpose, model, payload_sha256, tokens_in, tokens_out, cost_aud,
   entity_type, entity_id, ok, error)
VALUES (:purpose, :model, :sha, :tin, :tout, :cost, :etype, :eid, :ok, :err)
""")


def decode_jsonb(v):
    """jsonb reaches us decoded or raw depending on driver codec registration.

    The isinstance(str) -> json.loads guard used elsewhere in this codebase is
    right for jsonb OBJECTS -- a dict never arrives as a str unless it is
    undecoded -- but wrong for jsonb STRING SCALARS, where the decoded form is
    itself a str. app.settings holds the first scalar jsonb this system reads,
    and '"on"'::jsonb decodes to the Python string "on", which json.loads then
    rejects with "Expecting value: line 1 column 1".

    So: attempt a decode, and treat failure as proof it was already decoded.
    Both paths converge on the same value.
    """
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except ValueError:
        return v


async def load_config(conn) -> dict:
    rows = (await conn.execute(SETTINGS, {"keys": [
        "ai.mode", "ai.monthly_budget_aud", "ai.fx_aud_per_usd", "score.weights",
    ]})).mappings().all()
    return {r["key"]: decode_jsonb(r["value"]) for r in rows}


async def spent_this_month(conn) -> float:
    return float((await conn.execute(SPENT_THIS_MONTH)).scalar() or 0)


async def check_budget(conn, cfg: Mapping) -> float:
    """Raise before spending, not after. Returns the remaining budget."""
    if str(cfg.get("ai.mode", "on")).lower() == "off":
        raise AIDisabled("ai.mode is off; skipping enrichment")
    budget = float(cfg.get("ai.monthly_budget_aud", DEFAULT_MONTHLY_BUDGET_AUD))
    spent = await spent_this_month(conn)
    if spent >= budget:
        raise BudgetExceeded(
            f"month-to-date spend {spent:.2f} AUD has reached the {budget:.2f} cap")
    return budget - spent


async def log_call(conn, *, purpose: str, usage: Usage, sha: str,
                   entity_type: str | None = None, entity_id: int | None = None,
                   ok: bool = True, error: str | None = None) -> None:
    await conn.execute(LOG_CALL, {
        "purpose": purpose, "model": usage.model, "sha": sha,
        "tin": usage.tokens_in, "tout": usage.tokens_out, "cost": usage.cost_aud,
        "etype": entity_type, "eid": entity_id, "ok": ok,
        "err": (error or None) and error[:2000],
    })


def build_client(api_key: str):
    """Imported lazily so that every service without an API key -- which is all
    of them except the worker -- can still import this module."""
    import anthropic
    return anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=2)


def call_json(client, *, model: str, system: str, user: str,
              schema: Mapping, fx: float, max_tokens: int = 2000) -> tuple[dict, Usage]:
    """One structured-output call. Returns (parsed, usage).

    output_config.format constrains the response to the schema, so the result
    is parseable without begging the model to emit clean JSON in the prompt.
    """
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": dict(schema)},
                       **MODEL_OPTS.get(model, {}).get("output_config", {})},
    )
    usage = Usage(
        model=model,
        tokens_in=resp.usage.input_tokens,
        tokens_out=resp.usage.output_tokens,
        cost_aud=cost_aud(model, resp.usage.input_tokens, resp.usage.output_tokens, fx),
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    body = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(body), usage
