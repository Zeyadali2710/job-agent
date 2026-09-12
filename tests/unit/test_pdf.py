"""PDF rendering. Pure: structures in, bytes out.

These assert that a document is produced and survives the inputs that actually
break PDF writers -- unicode a core font cannot encode, empty sections, and
content longer than a page. Layout is not asserted; it is not a property worth
freezing in a test.
"""
from job_agent.generation.pdf import Contact, _s, cover_letter_pdf, resume_pdf

CONTACT = Contact("Zeyad Ali", "z@example.com", "+61 400 000 000", "Sydney, NSW",
                  [("GitHub", "github.com/x")])


def test_resume_renders_a_valid_pdf():
    pdf = resume_pdf(
        CONTACT,
        education=[{"qualification": "BSc Computer Science", "institution": "UNSW",
                    "start": "2022-02", "end": "2026-08"}],
        experience=[{"title": "Product Owner", "organisation": "ArmsOA",
                     "start": "2026-08", "end": None,
                     "bullets": ["Consolidated Python/FastAPI services"]}],
        projects=[{"name": "Job Agent", "bullets": ["Postgres-backed work queue"]}],
        skills=["Python", "PostgreSQL"])
    assert pdf[:5] == b"%PDF-" and pdf.rstrip()[-5:] == b"%%EOF"


def test_cover_letter_renders_a_valid_pdf():
    pdf = cover_letter_pdf(CONTACT, company="Atlassian",
                           job_title="Graduate Software Engineer",
                           paragraphs=["A paragraph. " * 40, "Another."],
                           date="13 September 2026")
    assert pdf[:5] == b"%PDF-"


def test_a_resume_with_nothing_in_it_still_renders():
    """Selection can legitimately return empty sections. An empty section must
    not emit a heading with nothing under it, and must never raise."""
    assert resume_pdf(Contact("A B"))[:5] == b"%PDF-"


def test_characters_a_core_font_cannot_encode_do_not_lose_the_document():
    """Core PDF fonts are latin-1. A smart quote from a generated paragraph
    must transliterate, not raise halfway through writing the file."""
    pdf = cover_letter_pdf(
        CONTACT, company="Café — Co",
        paragraphs=["I’ve built “things” — many…",
                    "Beyond latin-1: 你好"])
    assert pdf[:5] == b"%PDF-"


def test_typographic_characters_transliterate_rather_than_mangle():
    assert _s("I’ve") == "I've"
    assert _s("a — b") == "a - b"
    assert _s("wait…") == "wait..."
    assert "?" in _s("你好")


def test_content_longer_than_a_page_paginates():
    pdf = resume_pdf(CONTACT, experience=[
        {"title": f"Role {i}", "organisation": "Org", "start": "2020", "end": "2021",
         "bullets": [f"Bullet {j} " * 30 for j in range(6)]} for i in range(12)])
    assert pdf.count(b"/Type /Page") > 2
