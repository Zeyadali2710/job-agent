"""Cover-letter drafting. Architecture 9 routing: cloud Sonnet, because this is
the one place where quality converts directly to interviews.

What this prompt may see, and why that is safe: the job advertisement is public,
and the profile facts are a *selected subset* of the same facts that are about to
be sent to the employer on a resume. What it may never see is anything the
employer does not get either -- and it structurally cannot, because the caller
builds the fact list from `PublicProfile`, which has no field for an address, a
phone number, a TFN or a visa number.

Every paragraph must cite the fact ids it rests on. That is not a stylistic
request: `generation.provenance` rejects the document if a paragraph cites
nothing, cites an id that does not exist, or names an entity or number that
appears in neither the cited facts nor the advertisement.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

SYSTEM = (
    "You draft cover letters for an Australian graduate job applicant, using "
    "Australian English.\n\n"
    "You are given a job advertisement and a numbered list of facts about the "
    "applicant. Every factual claim you make must come from one of those facts "
    "or from the advertisement itself.\n\n"
    "Absolute rules:\n"
    "- Never state a number, percentage, duration, technology, employer, "
    "qualification or date that does not appear verbatim in the facts you cite "
    "or in the advertisement. If a fact says 741 tests, you may not write "
    "'over 700' or 'roughly 750'.\n"
    "- Never claim experience the facts do not show. If the advertisement asks "
    "for something the applicant lacks, either omit it or refer to adjacent "
    "experience that the facts do support.\n"
    "- Never merge two facts into one claim. Each employer, project and course "
    "is separate: if one fact describes a five-person university team and "
    "another describes a client contract, do not write a sentence that implies "
    "they were the same engagement. When two facts sit in one sentence, the "
    "sentence must remain true of each of them separately.\n"
    "- Each paragraph must list the exact ids of the facts it rests on. A "
    "paragraph resting on no fact is not allowed.\n"
    "- Write 3 or 4 paragraphs of at most 90 words each: why this role, what "
    "the applicant has done that bears on it, and a brief close.\n"
    "- Do not write a salutation, a sign-off, a date or an address. Those are "
    "added afterwards.\n"
    "- Plain prose. No bullet points, no headings, no markdown."
)

# Same restricted JSON Schema subset as extract.py: no maxItems, no maxLength,
# no type unions. The word and paragraph caps live in the system prompt, with
# max_tokens as the hard ceiling beneath them.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraphs"],
    "properties": {
        "paragraphs": {
            "type": "array",
            "description": "3 or 4 paragraphs, in the order they should be read.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "source_ids"],
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "One paragraph of plain prose, at most 90 words.",
                    },
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Exact ids of the supplied facts this paragraph rests "
                            "on. Must not be empty and must not invent an id."
                        ),
                    },
                },
            },
        },
    },
}

MAX_DESCRIPTION_CHARS = 8000
MAX_PARAGRAPHS = 4


def render_facts(facts: Sequence[Mapping]) -> str:
    """One fact per line as `id | body`. The id is what the model must cite, so
    it is first and separated by a character that does not occur in an id."""
    lines = []
    for f in facts:
        body = str(f["body"] if isinstance(f, Mapping) else f.body).replace("\n", " ")
        fid = f["id"] if isinstance(f, Mapping) else f.id
        lines.append(f"{fid} | {body}")
    return "\n".join(lines)


def build_user(title: str, company: str | None, description: str | None,
               facts: Sequence[Mapping]) -> str:
    body = (description or "").strip()
    if len(body) > MAX_DESCRIPTION_CHARS:
        body = body[:MAX_DESCRIPTION_CHARS] + "\n[truncated]"
    return (
        f"ADVERTISEMENT\nTitle: {title}\nCompany: {company or 'the company'}\n"
        f"---\n{body}\n---\n\n"
        f"FACTS ABOUT THE APPLICANT (id | fact)\n{render_facts(facts)}\n\n"
        "Draft the cover letter. Cite the exact ids you used for each paragraph."
    )
