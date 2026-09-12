"""Extraction prompt, the call path, and the HIGH_MATCH_JOB payload.

The Anthropic client is stubbed, so the request shape and the cost arithmetic
are exercised without a network or a key -- the same approach the Discord
failure paths use.
"""
import json
from types import SimpleNamespace

import pytest

from job_agent.ai import client as ai
from job_agent.ai.prompts import extract as prompt
from job_agent.notifications.matches import DEFAULT_MIN_SCORE, salary_text


# ------------------------------------------------------------------- prompt

def test_the_extraction_prompt_contains_no_profile_data():
    """Extraction compares nothing against you -- the input is the ad alone.
    The strongest possible answer to "could this leak my details" is that the
    prompt has no field to put them in."""
    user = prompt.build_user("Engineer", "Acme", "We need Python.")
    blob = (prompt.SYSTEM + user).lower()
    for token in ("tfn", "passport", "visa", "referee", "date of birth",
                  "address", "phone"):
        assert token not in blob


def test_long_descriptions_are_trimmed_to_cap_input_cost():
    body = "x" * (prompt.MAX_DESCRIPTION_CHARS + 5000)
    user = prompt.build_user("Engineer", "Acme", body)
    assert len(user) < prompt.MAX_DESCRIPTION_CHARS + 500
    assert "[truncated]" in user


def test_build_user_survives_a_missing_description():
    assert prompt.build_user("Engineer", None, None)


def test_schema_forbids_extra_keys_and_requires_the_two_that_matter():
    """skills feeds skill_overlap and requirements feeds the card. Everything
    else is a bonus, so only those two are required -- a partial extraction is
    better than a refused one."""
    assert prompt.SCHEMA["additionalProperties"] is False
    assert set(prompt.SCHEMA["required"]) == {"skills", "requirements"}


def _keywords(node):
    """Every schema keyword used anywhere in the tree."""
    found = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            if k == "properties" and isinstance(v, dict):
                for sub in v.values():
                    found |= _keywords(sub)
            elif k in ("items",):
                found |= _keywords(v)
    return found


def test_schema_uses_only_keywords_structured_outputs_accepts():
    """Structured outputs take a restricted subset of JSON Schema. The API
    rejected maxItems outright -- "For 'array' type, property 'maxItems' is not
    supported" -- and each such rejection costs a deploy cycle to discover, so
    the allowed set is pinned here rather than found one 400 at a time."""
    used = _keywords(prompt.SCHEMA) - {"properties"}
    unsupported = used - prompt.SUPPORTED_KEYWORDS
    assert not unsupported, f"schema uses unsupported keywords: {sorted(unsupported)}"


def test_no_type_unions_anywhere():
    """["integer", "null"] would very likely have been the next rejection.
    Optional fields are absent from `required` instead, and the model omits
    what the ad does not state."""
    for name, spec in prompt.SCHEMA["properties"].items():
        assert isinstance(spec["type"], str), f"{name} uses a type union"


def test_the_output_caps_moved_into_the_prompt():
    """maxItems used to bound the output-token bill. With it unavailable, the
    system prompt states the caps and max_tokens is the hard ceiling under it."""
    assert "at most 20 skills" in prompt.SYSTEM.lower()
    assert "at most 15 requirements" in prompt.SYSTEM.lower()


def test_the_grad_window_is_optional_because_it_is_a_two_sided_check():
    """Architecture 6.3: "must have graduated within the last 2 years" can
    exclude you for graduating too long ago. Absent must be distinguishable
    from zero, so it is omitted rather than sent as 0."""
    assert "graduation_window_years" not in prompt.SCHEMA["required"]
    assert prompt.SCHEMA["properties"]["graduation_window_years"]["type"] == "integer"


# --------------------------------------------------------------- call path

class StubMessages:
    def __init__(self, payload, tokens=(1000, 200), stop_reason="end_turn"):
        self.payload, self.tokens, self.stop_reason = payload, tokens, stop_reason
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
            usage=SimpleNamespace(input_tokens=self.tokens[0],
                                  output_tokens=self.tokens[1]),
        )


def stub(payload, **kw):
    return SimpleNamespace(messages=StubMessages(payload, **kw))


def test_call_json_parses_and_prices_the_response():
    client = stub({"skills": ["python"], "requirements": ["3 years"]})
    data, usage = ai.call_json(client, model=ai.HAIKU, system="s", user="u",
                               schema=prompt.SCHEMA, fx=1.55)
    assert data["skills"] == ["python"]
    assert usage.tokens_in == 1000 and usage.tokens_out == 200
    assert usage.cost_aud == ai.cost_aud(ai.HAIKU, 1000, 200, 1.55)


def test_call_json_sends_the_schema_as_structured_output():
    client = stub({"skills": [], "requirements": []})
    ai.call_json(client, model=ai.HAIKU, system="s", user="u",
                 schema=prompt.SCHEMA, fx=1.55)
    sent = client.messages.last_kwargs["output_config"]["format"]
    assert sent["type"] == "json_schema"
    assert sent["schema"]["required"] == ["skills", "requirements"]


def test_haiku_is_not_sent_an_effort_setting():
    """Haiku 4.5 rejects output_config.effort with a 400."""
    client = stub({"skills": [], "requirements": []})
    ai.call_json(client, model=ai.HAIKU, system="s", user="u",
                 schema=prompt.SCHEMA, fx=1.55)
    assert "effort" not in client.messages.last_kwargs["output_config"]


def test_sonnet_does_get_an_effort_setting():
    client = stub({"skills": [], "requirements": []})
    ai.call_json(client, model=ai.SONNET, system="s", user="u",
                 schema=prompt.SCHEMA, fx=1.55)
    assert client.messages.last_kwargs["output_config"]["effort"] == "low"


def test_a_refusal_raises_rather_than_writing_an_empty_analysis():
    """stop_reason 'refusal' returns HTTP 200 with no usable content. Reading
    content without checking it would store an empty skills list and quietly
    score every refused job as having no requirements."""
    client = stub({"skills": []}, stop_reason="refusal")
    with pytest.raises(RuntimeError):
        ai.call_json(client, model=ai.HAIKU, system="s", user="u",
                     schema=prompt.SCHEMA, fx=1.55)


# ---------------------------------------------------------------- the card

def test_high_match_threshold_matches_the_architecture():
    assert DEFAULT_MIN_SCORE == 80


@pytest.mark.parametrize("salary,expected", [
    ({"confidence": "STATED", "base_salary_min": 90000, "base_salary_max": 110000,
      "total_package_min": 100800}, "$90,000-110,000 (package $100,800)"),
    ({"confidence": "STATED", "base_salary_min": 90000, "total_package_min": 100800},
     "$90,000 (package $100,800)"),
    ({"confidence": "ABSENT"}, None),
    ({}, None),
    (None, None),
    ('{"confidence": "STATED", "base_salary_min": 80000}', "$80,000"),
])
def test_salary_text_renders_or_stays_silent(salary, expected):
    """After the money-marker fix most ads are genuinely ABSENT. The card must
    print nothing rather than a fabricated figure."""
    assert salary_text(salary) == expected


# ------------------------------------------------- permanent vs transient

class FakeStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_client_errors_abort_the_batch(status):
    """A 400 like "this API key is not scoped to a workspace" is a property of
    the credential, not of one advertisement. Continuing would make an
    identical doomed call for every remaining job and fill llm_calls with
    copies of one message -- which is exactly what happened on the first real
    run against the API."""
    assert ai.is_permanent_error(FakeStatusError(status)) is True


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
def test_rate_limits_and_server_errors_are_not_permanent(status):
    """The SDK has already retried these by the time they surface. The next
    job may well succeed, so the batch carries on."""
    assert ai.is_permanent_error(FakeStatusError(status)) is False


@pytest.mark.parametrize("exc", [
    ValueError("bad json"),
    RuntimeError("model declined the request"),
    TimeoutError(),
])
def test_errors_without_a_status_code_do_not_abort(exc):
    """A parse failure or a refusal is about that one advertisement."""
    assert ai.is_permanent_error(exc) is False
