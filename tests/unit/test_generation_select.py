"""Resume selection. Deterministic, so every case here is exact rather than
approximate."""
from job_agent.generation.select import (
    Fact, facts_by_id, relevance, select, selected_facts, terms,
)

FACTS = [
    Fact("exp.a.b1", "experience_bullet",
         "Led UX optimisation of a Shopify storefront in HTML, CSS and JavaScript, "
         "increasing retention by 25%", ("javascript", "shopify")),
    Fact("exp.a.b2", "experience_bullet",
         "Consolidated Python/FastAPI and React/TypeScript services",
         ("python", "fastapi", "react")),
    Fact("exp.a.b3", "experience_bullet", "Ran weekly stand-ups with the client", ("agile",)),
    Fact("proj.x.b1", "project_bullet", "Built a Postgres-backed work queue in Python",
         ("python", "postgres")),
    Fact("proj.y.b1", "project_bullet", "Wrote a terminal Pacman clone in C", ("c",)),
    Fact("skill.python", "skill", "Python", ("ADVANCED",)),
    Fact("skill.c", "skill", "C", ("ADVANCED",)),
    Fact("skill.blender", "skill", "Blender", ("BASIC",)),
    Fact("star.s1", "star", "Integrated five capstone codebases into one Python platform",
         ("delivery",)),
    Fact("edu.unsw", "education", "BSc Computer Science, UNSW"),
]


def test_generic_vocabulary_is_not_a_ranking_signal():
    """Nearly every ad says "communication" and "software engineering". Terms
    that match everything rank nothing."""
    t = terms(["Python", "FastAPI"], ["Strong communication skills"],
              ["Graduate Software Engineer"])
    assert "python" in t and "fastapi" in t
    assert "communication" not in t and "software" not in t and "engineer" not in t


def test_relevance_counts_distinct_job_terms():
    jt = terms(["Python", "FastAPI", "PostgreSQL"], [], [])
    assert relevance(FACTS[1], jt) == 2.0        # python + fastapi, no digits


def test_a_measured_outcome_breaks_ties_upward():
    """"increasing retention by 25%" earns the half point: between two equally
    relevant bullets the one with a number is the one worth the page space."""
    jt = terms(["Shopify"], [], [])
    assert relevance(FACTS[0], jt) == 1.5
    assert relevance(FACTS[2], jt) == 0.0


def test_selection_picks_relevant_bullets_but_prints_them_chronologically():
    sel = select(FACTS, job_skills=["Python", "FastAPI"],
                 job_title="Graduate Python Engineer", max_experience=2)
    assert set(sel.experience) == {"exp.a.b1", "exp.a.b2"}
    order = [f.id for f in FACTS]
    assert list(sel.experience) == sorted(sel.experience, key=order.index)


def test_skills_lead_with_the_ones_the_job_asked_for():
    sel = select(FACTS, job_skills=["Python"], max_skills=2)
    assert sel.skills[0] == "skill.python"


def test_education_is_never_ranked_away():
    """A graduate resume leads with the degree whether or not the ad mentions
    it. Dropping it for a better-matching bullet is a worse resume."""
    sel = select(FACTS, job_skills=["Kubernetes"], max_experience=1)
    assert sel.education == ("edu.unsw",)


def test_a_job_with_no_extracted_vocabulary_still_produces_a_resume():
    """Enrichment can be missing or empty. Selection must degrade to "keep
    everything in order", never to "select nothing"."""
    sel = select(FACTS, job_skills=[], job_requirements=[], job_title="")
    assert len(sel.experience) == 3 and sel.education == ("edu.unsw",)


def test_the_validator_index_covers_every_fact_not_just_the_selected_ones():
    """Citing a real fact that selection dropped is a weaker document, not a
    fabrication -- UNKNOWN_SOURCE_ID must mean "no such fact"."""
    sel = select(FACTS, job_skills=["Python"], max_skills=1)
    assert "skill.blender" not in sel.skills
    assert "skill.blender" in facts_by_id(FACTS)


def test_selected_facts_resolve_in_selection_order():
    sel = select(FACTS, job_skills=["Python"])
    assert [f.id for f in selected_facts(FACTS, sel)][0] == "edu.unsw"
