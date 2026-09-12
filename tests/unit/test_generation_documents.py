"""Resume assembly, rendering dispatch, and the worker/renderer split.

Pure functions and module structure only -- no DB, no model.
"""
import pytest

from job_agent.generation.documents import build_resume, fmt_date, job_text
from job_agent.generation.pdf import Contact
from job_agent.generation.render import _filename, render_document
from job_agent.generation.select import Fact, select

EXPERIENCE_DATA = {
    "id": "exp.armsoa", "organisation": "ArmsOA", "title": "Product Owner",
    "start": "2026-08", "end": None,
    "bullets": [{"id": "exp.armsoa.b1", "text": "Authored an integration plan"},
                {"id": "exp.armsoa.b2", "text": "Consolidated Python/FastAPI services"}],
}
PROJECT_DATA = {
    "id": "proj.ancr", "name": "ANCR Capstone", "start": "2026-02", "end": "2026-08",
    "bullets": [{"id": "proj.ancr.b1", "text": "Product Owner for a 5-person team"}],
}
EDUCATION_DATA = {
    "id": "edu.unsw", "qualification": "BSc Computer Science", "institution": "UNSW",
    "start": "2022-02", "end": "2026-08",
    "highlights": [{"id": "edu.unsw.h1", "text": "Security Engineering"},
                   {"id": "edu.unsw.h2", "text": "Social club treasurer"}],
}

FACTS = [
    Fact("edu.unsw", "education", "BSc Computer Science, UNSW", (), EDUCATION_DATA),
    Fact("edu.unsw.h1", "education_bullet", "Security Engineering", ("security",)),
    Fact("edu.unsw.h2", "education_bullet", "Social club treasurer", ()),
    Fact("exp.armsoa", "experience", "Product Owner, ArmsOA", (), EXPERIENCE_DATA),
    Fact("exp.armsoa.b1", "experience_bullet", "Authored an integration plan", ()),
    Fact("exp.armsoa.b2", "experience_bullet", "Consolidated Python/FastAPI services",
         ("python", "fastapi")),
    Fact("proj.ancr", "project", "ANCR Capstone", (), PROJECT_DATA),
    Fact("proj.ancr.b1", "project_bullet", "Product Owner for a 5-person team", ()),
    Fact("skill.python", "skill", "Python", ("ADVANCED",)),
    Fact("exp.empty", "experience", "Nothing, Nowhere", (),
         {"id": "exp.empty", "organisation": "Nowhere", "title": "Nothing",
          "bullets": [{"id": "exp.empty.b1", "text": "Unselected"}]}),
]


# ------------------------------------------------------------------ dates

@pytest.mark.parametrize("raw,expected", [
    ("2026-08", "Aug 2026"),
    ("2024-10-01", "Oct 2024"),
    ("2026", "2026"),            # a year alone is legitimate
    ("", ""),
    (None, ""),
    ("2026-13", "2026-13"),      # not a month: passed through, not guessed at
])
def test_dates_format_for_reading_and_pass_through_what_they_cannot_parse(raw, expected):
    assert fmt_date(raw) == expected


# ------------------------------------------------------------------ resume

def test_the_resume_contains_only_selected_bullets():
    sel = select(FACTS, job_skills=["Python", "FastAPI"], max_experience=1,
                 max_education_highlights=1)
    r = build_resume(FACTS, sel)
    texts = [b for e in r["experience"] for b in e["bullets"]]
    assert texts == ["Consolidated Python/FastAPI services"]


def test_a_parent_with_no_selected_bullets_is_dropped_entirely():
    """Otherwise the resume prints an employer heading with nothing under it,
    which reads as an omission rather than as a choice."""
    sel = select(FACTS, job_skills=["Python", "FastAPI"], max_experience=1)
    r = build_resume(FACTS, sel)
    assert [e["organisation"] for e in r["experience"]] == ["ArmsOA"]


def test_education_survives_even_when_no_highlight_does():
    sel = select(FACTS, job_skills=["Kubernetes"], max_education_highlights=0)
    r = build_resume(FACTS, sel)
    assert len(r["education"]) == 1
    assert r["education"][0]["qualification"] == "BSc Computer Science"
    assert r["education"][0]["bullets"] == []


def test_dates_are_formatted_and_an_open_end_stays_open():
    sel = select(FACTS, job_skills=["Python"])
    r = build_resume(FACTS, sel)
    exp = next(e for e in r["experience"] if e["organisation"] == "ArmsOA")
    assert exp["start"] == "Aug 2026" and exp["end"] == ""


def test_every_resume_string_is_a_fact_body_copied_verbatim():
    """The property that makes the resume exempt from the entity scan: nothing
    on it is generated, so it cannot claim something the profile does not."""
    sel = select(FACTS, job_skills=["Python", "FastAPI", "Security"])
    r = build_resume(FACTS, sel)
    bodies = {f.body for f in FACTS}
    for section in ("experience", "projects", "education"):
        for entry in r[section]:
            for bullet in entry["bullets"]:
                assert bullet in bodies, bullet
    for s in r["skills"]:
        assert s in bodies


def test_the_job_text_the_validator_sees_includes_title_and_company():
    """An entity borrowed from the ad must not read as invented, so everything
    the model was shown goes into the haystack."""
    t = job_text("Graduate Engineer", "Atlassian", "We use Django.")
    assert "Graduate Engineer" in t and "Atlassian" in t and "Django" in t


# ------------------------------------------------------------------ rendering

CONTACT = Contact("Zeyad Ali", "z@example.com", "0400", "Sydney, NSW")


def test_render_dispatches_on_kind():
    resume = render_document("RESUME", {"skills": ["Python"]}, CONTACT)
    letter = render_document("COVER_LETTER", {"paragraphs": [{"text": "Hello."}]},
                             CONTACT, title="Engineer", company="Atlassian")
    assert resume[:5] == b"%PDF-" and letter[:5] == b"%PDF-"


def test_an_unknown_document_kind_raises_rather_than_writing_an_empty_pdf():
    with pytest.raises(ValueError):
        render_document("ANSWER_SET", {}, CONTACT)


def test_filenames_carry_the_application_and_a_content_hash():
    name = _filename("COVER_LETTER", 42, "abc123def4567890")
    assert name == "app-000042-cover-letter-abc123def456.pdf"


# ------------------------------------------------------------------ the split

def test_the_worker_cannot_claim_render_and_the_renderer_claims_nothing_else():
    """ja_worker has no USAGE on schema sensitive, so a render item it claimed
    would fail all eight attempts. The queue filter is what enforces this at
    runtime; these two handler maps are what it filters on."""
    from job_agent.workers import loop, renderer
    assert "render" not in loop.HANDLERS
    assert set(renderer.HANDLERS) == {"render"}
    assert not set(loop.HANDLERS) & set(renderer.HANDLERS)


def test_handlers_exist_for_every_kind_the_scheduler_enqueues():
    from job_agent.workers.loop import HANDLERS
    for kind in ("draft", "generate", "screen", "score", "enrich", "notify_matches"):
        assert kind in HANDLERS, kind


def test_parent_map_is_built_from_the_data_not_from_id_prefixes():
    """A profile is free to name its facts however it likes; the parent's own
    bullet list is the authoritative link."""
    from job_agent.generation.documents import parent_of
    pm = parent_of(FACTS)
    assert pm["exp.armsoa.b1"] == "ArmsOA"
    assert pm["proj.ancr.b1"] == "ANCR Capstone"
    assert "skill.python" not in pm and "edu.unsw.h1" not in pm
