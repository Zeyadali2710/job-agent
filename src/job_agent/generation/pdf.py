"""PDF rendering. Runs only in the renderer container, which has no network.

fpdf2 rather than WeasyPrint: every service is built from one Dockerfile, and
WeasyPrint would pull pango, cairo and gdk-pixbuf into the image that the API,
the worker, the scheduler and the notifier all run from. fpdf2 is a pure-Python
wheel, and a resume is a stack of headings and bullets, not a web page.

Core PDF fonts carry no embedded font file, which keeps the image small and the
output deterministic; the cost is latin-1, so text is transliterated rather than
allowed to raise halfway through a document.

These functions are pure: structures in, bytes out. No DB, no clock, no I/O.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from fpdf import FPDF

FONT = "Helvetica"
MARGIN = 15.0
RULE_GREY = 160


@dataclass(frozen=True)
class Contact:
    """Template-filled locally, never sent to a model. PublicProfile carries no
    full_name, email or phone, so these can only have come from the renderer's
    own read of the profile."""
    full_name: str
    email: str = ""
    phone: str = ""
    location: str = ""
    links: Sequence[tuple[str, str]] = field(default_factory=tuple)


def _s(text: str) -> str:
    """Core fonts are latin-1. Replace rather than raise: a smart quote in a
    generated paragraph must not lose the whole document."""
    return (str(text)
            .replace("’", "'").replace("‘", "'")
            .replace("“", '"').replace("”", '"')
            .replace("–", "-").replace("—", "-")
            .replace("…", "...").replace(" ", " ")
            .encode("latin-1", "replace").decode("latin-1"))


class _Doc(FPDF):
    def __init__(self) -> None:
        super().__init__(format="A4", unit="mm")
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(True, margin=MARGIN)
        self.set_creator("job-agent")

    @property
    def usable_width(self) -> float:
        return self.w - 2 * MARGIN

    def header_block(self, c: Contact) -> None:
        self.set_font(FONT, "B", 18)
        self.cell(0, 9, _s(c.full_name), new_x="LMARGIN", new_y="NEXT")
        bits = [b for b in (c.location, c.email, c.phone) if b]
        bits += [url for _, url in c.links]
        if bits:
            self.set_font(FONT, "", 9)
            self.set_text_color(90)
            self.multi_cell(self.usable_width, 4.5, _s("  |  ".join(bits)))
            self.set_text_color(0)
        self.ln(1)

    def rule(self) -> None:
        self.set_draw_color(RULE_GREY)
        self.line(MARGIN, self.get_y(), self.w - MARGIN, self.get_y())
        self.ln(2)

    def section(self, title: str) -> None:
        self.ln(2)
        self.set_font(FONT, "B", 11)
        self.cell(0, 6, _s(title.upper()), new_x="LMARGIN", new_y="NEXT")
        self.rule()

    def entry(self, left: str, right: str = "") -> None:
        self.set_font(FONT, "B", 10)
        self.cell(self.usable_width * 0.72, 5, _s(left))
        self.set_font(FONT, "", 9)
        self.set_text_color(90)
        self.cell(0, 5, _s(right), align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0)

    def subtitle(self, text: str) -> None:
        if not text:
            return
        self.set_font(FONT, "I", 9.5)
        self.cell(0, 4.5, _s(text), new_x="LMARGIN", new_y="NEXT")

    def bullet(self, text: str) -> None:
        self.set_font(FONT, "", 9.5)
        x = self.get_x()
        self.cell(4, 4.8, "-")
        self.set_x(x + 4)
        self.multi_cell(self.usable_width - 4, 4.8, _s(text))
        self.set_x(x)

    def paragraph(self, text: str, size: float = 10.0, leading: float = 5.0) -> None:
        self.set_font(FONT, "", size)
        self.multi_cell(self.usable_width, leading, _s(text))
        self.ln(2)


def _dates(entry: Mapping) -> str:
    start, end = entry.get("start"), entry.get("end")
    if not start and not end:
        return ""
    return f"{start or ''} - {end or 'Present'}".strip(" -")


def resume_pdf(contact: Contact, *, education: Sequence[Mapping] = (),
               experience: Sequence[Mapping] = (), projects: Sequence[Mapping] = (),
               skills: Sequence[str] = ()) -> bytes:
    """Every string here came from a profile fact. Nothing is generated, so a
    resume cannot contain a claim the profile does not already make."""
    d = _Doc()
    d.add_page()
    d.header_block(contact)

    if education:
        d.section("Education")
        for e in education:
            d.entry(_s(e.get("qualification", "")), _dates(e))
            d.subtitle(e.get("institution", ""))
            for b in e.get("bullets") or ():
                d.bullet(b)
            d.ln(1)

    if experience:
        d.section("Experience")
        for x in experience:
            d.entry(_s(x.get("title", "")), _dates(x))
            d.subtitle(x.get("organisation", ""))
            for b in x.get("bullets") or ():
                d.bullet(b)
            d.ln(1)

    if projects:
        d.section("Projects")
        for p in projects:
            d.entry(_s(p.get("name", "")), _dates(p))
            for b in p.get("bullets") or ():
                d.bullet(b)
            d.ln(1)

    if skills:
        d.section("Skills")
        d.paragraph(", ".join(str(s) for s in skills), size=9.5, leading=4.8)

    return bytes(d.output())


def cover_letter_pdf(contact: Contact, *, company: str = "", job_title: str = "",
                     paragraphs: Sequence[str] = (), date: str = "",
                     closing: str = "Yours sincerely") -> bytes:
    d = _Doc()
    d.add_page()
    d.header_block(contact)
    d.ln(3)

    if date:
        d.set_font(FONT, "", 10)
        d.cell(0, 5, _s(date), new_x="LMARGIN", new_y="NEXT")
        d.ln(2)

    if company or job_title:
        d.set_font(FONT, "B", 10)
        line = " - ".join(b for b in (job_title, company) if b)
        d.cell(0, 5, _s(f"Re: {line}"), new_x="LMARGIN", new_y="NEXT")
        d.ln(3)

    d.set_font(FONT, "", 10)
    d.cell(0, 5, _s("Dear Hiring Manager,"), new_x="LMARGIN", new_y="NEXT")
    d.ln(3)

    for p in paragraphs:
        d.paragraph(p)

    d.ln(2)
    d.set_font(FONT, "", 10)
    d.cell(0, 5, _s(closing + ","), new_x="LMARGIN", new_y="NEXT")
    d.ln(6)
    d.cell(0, 5, _s(contact.full_name), new_x="LMARGIN", new_y="NEXT")
    return bytes(d.output())
