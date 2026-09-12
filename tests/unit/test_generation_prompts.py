"""The cover-letter and answer schemas obey the same restricted JSON Schema
subset that cost a deploy cycle to discover on the extraction prompt, and the
prompts state the rules the provenance validator actually enforces.
"""
import pytest

from job_agent.ai.prompts import answers, cover_letter
from job_agent.ai.prompts.extract import SUPPORTED_KEYWORDS
from job_agent.generation.select import Fact

SCHEMAS = {"cover_letter": cover_letter.SCHEMA, "answers": answers.SCHEMA}


def _keywords(node):
    found = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            if k == "properties" and isinstance(v, dict):
                for sub in v.values():
                    found |= _keywords(sub)
            elif k == "items":
                found |= _keywords(v)
    return found


def _types(node):
    """Every `type` value in the tree, so a union anywhere is visible."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "type":
                out.append(v)
            elif k == "properties":
                for sub in v.values():
                    out += _types(sub)
            elif k == "items":
                out += _types(v)
    return out


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_schema_uses_only_keywords_structured_outputs_accepts(name):
    unsupported = _keywords(SCHEMAS[name]) - {"properties"} - SUPPORTED_KEYWORDS
    assert not unsupported, f"{name} uses unsupported keywords: {sorted(unsupported)}"


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_no_type_unions_anywhere(name):
    assert all(isinstance(t, str) for t in _types(SCHEMAS[name]))


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_generated_block_must_carry_source_ids(name):
    """The provenance validator fails a block that cites nothing, so the schema
    must require the field rather than leave it optional."""
    items = SCHEMAS[name]["properties"][
        "paragraphs" if name == "cover_letter" else "answers"]["items"]
    assert "source_ids" in items["required"]


def test_answers_require_the_can_answer_decision():
    """Architecture 9's third mechanism. Without can_answer in `required` the
    model can silently omit it and a guess reads as an answer."""
    items = answers.SCHEMA["properties"]["answers"]["items"]
    assert "can_answer" in items["required"] and "answer_text" in items["required"]


def test_answer_text_is_a_plain_string_not_a_nullable():
    """No type unions available, so "unanswerable" is an empty string. The DB
    CHECK (NOT can_answer OR answer_text IS NOT NULL) only constrains the
    answerable case, so this is consistent rather than a workaround."""
    spec = answers.SCHEMA["properties"]["answers"]["items"]["properties"]["answer_text"]
    assert spec["type"] == "string"


def test_declining_to_answer_is_stated_as_correct_not_as_failure():
    assert "not a failure" in answers.SYSTEM.lower()


def test_the_cover_letter_prompt_forbids_restating_a_number():
    """The failure mode that matters: 741 becoming "over 700" passes a human
    read and fails provenance, so the prompt names it explicitly."""
    assert "741" in cover_letter.SYSTEM and "over 700" in cover_letter.SYSTEM


def test_facts_render_id_first_so_the_model_can_cite_them():
    rendered = cover_letter.render_facts([
        Fact("exp.a.b1", "experience_bullet", "Did a thing\nacross two lines"),
        {"id": "skill.python", "body": "Python"},
    ])
    assert rendered.splitlines()[0].startswith("exp.a.b1 | ")
    assert "\n" not in rendered.splitlines()[0]      # bodies are flattened
    assert rendered.splitlines()[1] == "skill.python | Python"


def test_a_long_advertisement_is_trimmed_not_sent_whole():
    user = cover_letter.build_user("T", "C", "x" * 20000, [])
    assert "[truncated]" in user
    assert len(user) < 20000


def test_the_question_list_pairs_key_with_text():
    q = answers.render_questions([
        {"question_key": "why_us", "question_text": "Why do you want to work here?"}])
    assert q == "why_us | Why do you want to work here?"


def test_the_cover_letter_prompt_forbids_merging_two_facts_into_one_claim():
    """The failure the validator structurally cannot catch: both "five"s were
    real, but one was a university team and the other a client contract, and
    the sentence implied they were the same engagement. Entity checking sees
    relations between facts as invisible, so the prompt has to carry this."""
    assert "never merge two facts" in cover_letter.SYSTEM.lower()


def test_the_prompt_requires_citing_the_fact_that_names_a_thing():
    """A real quarantine: the letter said "UNSW" and "YSpot" while citing only
    project bullets, so neither name appeared in any cited fact. Both were the
    applicant's own -- the failure was the citation, not the claim."""
    assert "cite the fact whose text contains that name" in cover_letter.SYSTEM
