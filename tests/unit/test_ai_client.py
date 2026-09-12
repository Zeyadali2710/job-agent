"""Cost arithmetic, the spend cap, and the PII boundary. No network, no DB."""
import pytest

from job_agent.ai.client import (
    HAIKU, PRICING_USD, SONNET, Usage, cost_aud, payload_sha256,
)
from job_agent.profile.models import PublicProfile


# ------------------------------------------------------------------- pricing

def test_cost_matches_published_rates():
    """Haiku 4.5 is $1/$5 per million tokens. At an fx of 1.5, a million in and
    a million out is (1 + 5) * 1.5 = 9.00 AUD."""
    assert cost_aud(HAIKU, 1_000_000, 1_000_000, 1.5) == 9.0


def test_sonnet_costs_more_than_haiku_for_identical_traffic():
    assert cost_aud(SONNET, 10_000, 2_000, 1.55) > cost_aud(HAIKU, 10_000, 2_000, 1.55)


def test_output_tokens_are_the_expensive_ones():
    """Output is 5x input on both models, so a verbose prompt is cheap and a
    verbose response is not -- which is why extraction caps max_tokens."""
    for model in (HAIKU, SONNET):
        assert cost_aud(model, 1000, 0, 1.55) * 5 == pytest.approx(
            cost_aud(model, 0, 1000, 1.55))


def test_an_unknown_model_costs_zero_rather_than_crashing():
    """A model id we have no price for must not take down the pipeline, but it
    must also not silently bill -- zero is visible in llm_calls as wrong."""
    assert cost_aud("claude-something-new", 1000, 1000, 1.55) == 0.0


def test_realistic_daily_volume_lands_near_the_architecture_estimate():
    """Architecture 6 budgets ~12 extractions and ~8 rationales a day at
    9-14 AUD a month. Rough shapes: 2k in / 400 out for extraction, 3k in /
    300 out for a rationale."""
    daily = 12 * cost_aud(HAIKU, 2000, 400, 1.55) + 8 * cost_aud(SONNET, 3000, 300, 1.55)
    monthly = daily * 30
    assert 0.5 < monthly < 14, f"projected {monthly:.2f} AUD/month"


# -------------------------------------------------------------------- hashing

def test_payload_hash_is_stable_across_key_order():
    """llm_calls.payload_sha256 is the audit trail for what left the network;
    it must not change because a dict was built in a different order."""
    assert payload_sha256({"a": 1, "b": 2}) == payload_sha256({"b": 2, "a": 1})


def test_payload_hash_changes_with_content():
    assert payload_sha256({"a": 1}) != payload_sha256({"a": 2})


def test_payload_hash_is_a_sha256():
    h = payload_sha256({"a": 1})
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


# ------------------------------------------------------------------ PII bound

SENSITIVE_FIELDS = {
    "address", "dob", "phone", "tfn", "visa_grant_number",
    "passport", "referee_contacts", "salary_expectations_floor", "email",
}


def test_public_profile_cannot_carry_sensitive_fields():
    """Architecture 9: "This is a type-system guarantee, not a code-review
    guarantee." The prompt builders take PublicProfile, so anything absent from
    this model is structurally unable to reach a prompt."""
    leaked = SENSITIVE_FIELDS & set(PublicProfile.model_fields)
    assert not leaked, f"PublicProfile exposes {sorted(leaked)}"


def test_public_profile_still_carries_what_a_prompt_needs():
    """The guarantee is worthless if the projection is so thin the model has
    nothing to work from."""
    needed = {"preferred_name", "work_right_status", "education",
              "experience", "projects", "skills", "star"}
    assert needed <= set(PublicProfile.model_fields)


def test_usage_is_immutable():
    """Usage feeds the budget; nothing downstream should be able to edit a
    recorded cost."""
    u = Usage(model=HAIKU, tokens_in=1, tokens_out=1, cost_aud=0.1)
    with pytest.raises(Exception):
        u.cost_aud = 0.0


def test_every_routed_model_has_a_price():
    """A model in the routing table with no price would bill invisibly."""
    assert set(PRICING_USD) == {HAIKU, SONNET}
