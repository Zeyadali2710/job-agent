"""The anti-fabrication validator. Pure function, no DB, no model.

These are the tests that decide whether a generated document reaches an
employer, so they are written as "what must never get through" rather than as
coverage of the implementation.
"""
import pytest

from job_agent.generation.provenance import (
    PASSED, SUSPECTED, _entities, check_text, cited_ids, validate,
)

FACTS = {
    "exp.armsoa.b2": ("Consolidated Python/FastAPI and React/TypeScript services, "
                      "achieving a 100% pass rate across 741 automated tests"),
    "proj.ancr.b1": ("Product Owner and point of contact for a 5-person engineering "
                     "team building an offline-first Network Analytic Tool"),
    "skill.python": "Python",
}
AD = ("We are hiring a Graduate Software Engineer in Sydney. "
      "You will work with Python, Django and AWS.")


def kinds(findings):
    return sorted(f.kind for f in findings)


# ------------------------------------------------------------ must pass

def test_a_paragraph_drawn_from_its_cited_fact_passes():
    f = check_text(
        "I consolidated Python and FastAPI services, reaching a 100% pass rate "
        "across 741 automated tests.",
        source_ids=["exp.armsoa.b2"], facts=FACTS, job_text=AD)
    assert f == [], [str(x) for x in f]


def test_a_technology_named_only_in_the_job_ad_is_allowed():
    """The ad is part of the evidence. Saying you are keen to work with Django
    is not a claim to have used it, and blocking it would make every cover
    letter unable to mention the employer's own stack."""
    assert check_text("I am keen to work with Django on your team.",
                      source_ids=["skill.python"], facts=FACTS, job_text=AD) == []


def test_sentence_initial_verbs_are_not_proper_nouns():
    """Without this the check drowns: every sentence starts with a capital."""
    f = check_text("Building reliable systems matters. Delivering them on time matters more.",
                   source_ids=["skill.python"], facts=FACTS, job_text=AD)
    assert f == [], [str(x) for x in f]


def test_a_number_at_the_end_of_a_sentence_keeps_its_meaning():
    assert check_text("The suite has 741.", source_ids=["exp.armsoa.b2"],
                      facts=FACTS, job_text=AD) == []


# ------------------------------------------------------------ must fail

def test_a_fabricated_metric_is_caught():
    """The single most dangerous thing a model invents. 99% and 9000 are both
    plausible and both absent from the cited fact, which says 100% and 741."""
    f = check_text("I delivered a 99% pass rate across 9000 automated tests.",
                   source_ids=["exp.armsoa.b2"], facts=FACTS, job_text=AD)
    assert kinds(f) == ["UNSUPPORTED_NUMBER", "UNSUPPORTED_NUMBER"]


def test_a_fabricated_technology_is_caught():
    f = check_text("I built the platform on Kubernetes and Terraform.",
                   source_ids=["exp.armsoa.b2"], facts=FACTS, job_text=AD)
    assert kinds(f) == ["UNSUPPORTED_ENTITY", "UNSUPPORTED_ENTITY"]


def test_an_uncited_paragraph_fails():
    """Unverifiable is not the same as false, but it cannot be allowed through:
    nothing downstream can check it."""
    assert any(x.kind == "NO_CITATION"
               for x in check_text("Some text.", source_ids=[], facts=FACTS))


def test_a_citation_to_a_fact_that_does_not_exist_fails():
    assert any(x.kind == "UNKNOWN_SOURCE_ID" for x in
               check_text("Some text.", source_ids=["exp.nope.b9"], facts=FACTS))


def test_an_entity_shaped_first_word_is_still_checked():
    """The sentence-initial exemption must not become a way through: FastAPI
    starting a sentence is still a claim."""
    assert any("FastAPI" in x.detail for x in check_text(
        "FastAPI was never used here.", source_ids=["skill.python"],
        facts=FACTS, job_text=AD))


# ------------------------------------------------------- numbers as words

def test_a_spelled_out_number_is_supported_by_its_digit_form():
    """A fact saying "5-person" supports a letter saying "five-person"."""
    assert check_text("I led a five-person team.", source_ids=["proj.ancr.b1"],
                      facts=FACTS) == []


def test_a_spelled_out_number_that_matches_nothing_is_caught():
    """The hole this closed: 741 and 100% were checked from the start, but
    "five" and "two-week" were not checked at all, so a model that cannot
    invent a digit could still invent a quantity in words."""
    f = check_text("I led a seven-person team.", source_ids=["proj.ancr.b1"],
                   facts=FACTS)
    assert kinds(f) == ["UNSUPPORTED_NUMBER"] and "seven" in f[0].detail


def test_a_number_word_inside_another_word_is_not_a_number():
    """Without a word boundary, "Someone" contains "one" and every letter
    mentioning anyone fails."""
    assert check_text("Someone honest.", source_ids=["skill.python"],
                      facts=FACTS) == []


# ------------------------------------------------------------ tokenising

@pytest.mark.parametrize("text,expected", [
    ("We use Node.js daily.", "Node.js"),
    ("Our CI/CD pipeline runs.", "CI/CD"),
])
def test_dotted_and_slashed_names_survive_tokenising(text, expected):
    assert expected in _entities(text)


def test_a_plain_sentence_yields_no_entities():
    assert _entities("I ship code.") == []


# ------------------------------------------------------------ document level

def test_one_bad_block_quarantines_the_whole_document():
    state, f = validate(
        [{"text": "Python work.", "source_ids": ["skill.python"]},
         {"text": "I used Kubernetes.", "source_ids": ["skill.python"]}],
        facts=FACTS, job_text=AD)
    assert state == SUSPECTED and len(f) == 1


def test_a_clean_document_passes():
    state, f = validate([{"text": "Python work.", "source_ids": ["skill.python"]}],
                        facts=FACTS, job_text=AD)
    assert state == PASSED and f == []


def test_cited_ids_are_deduplicated_in_order():
    blocks = [{"text": "a", "source_ids": ["b", "a"]},
              {"text": "c", "source_ids": ["a", "c"]}]
    assert cited_ids(blocks) == ["b", "a", "c"]


# ----------------------------------------------------- advisories, not failures

PARENTS = {"exp.armsoa.b1": "ArmsOA", "exp.armsoa.b2": "ArmsOA",
           "proj.ancr.b1": "ANCR Capstone"}


def test_a_paragraph_spanning_two_engagements_is_flagged_for_review():
    """The real failure entity checking cannot see: "as Product Owner for a
    five-person capstone team, I directed the integration of five codebases"
    -- every entity real, both numbers cited, two different engagements."""
    from job_agent.generation.provenance import merged_source_advisories
    f = merged_source_advisories(
        [{"source_ids": ["exp.armsoa.b1", "proj.ancr.b1"]}], PARENTS)
    assert [x.kind for x in f] == ["MERGED_SOURCES"]
    assert "ArmsOA" in f[0].detail and "ANCR Capstone" in f[0].detail


def test_two_bullets_from_the_same_engagement_are_not_a_merge():
    from job_agent.generation.provenance import merged_source_advisories
    assert merged_source_advisories(
        [{"source_ids": ["exp.armsoa.b1", "exp.armsoa.b2"]}], PARENTS) == []


def test_background_facts_do_not_count_as_a_second_engagement():
    """Citing a skill or a degree alongside a role is normal writing, not a
    conflation; counting it would make the advisory fire on every paragraph
    and stop being read."""
    from job_agent.generation.provenance import merged_source_advisories
    assert merged_source_advisories(
        [{"source_ids": ["exp.armsoa.b1", "skill.python", "edu.unsw"]}], PARENTS) == []


def test_an_advisory_is_not_a_quarantine():
    """MERGED_SOURCES must never change validator_state: a good cover letter
    legitimately draws on two roles in one paragraph, and quarantining those
    would make the mechanism useless."""
    state, findings = validate(
        [{"text": "Python work.", "source_ids": ["skill.python"]}],
        facts=FACTS, job_text=AD)
    assert state == PASSED and findings == []
