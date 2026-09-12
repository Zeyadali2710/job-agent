"""Structured requirement extraction. Architecture 6 routing table, Haiku-class,
~12 a day post-screen.

The input is the job ad and nothing else. No profile, public or otherwise, is
serialised here -- there is nothing to compare against at this stage, so the
question "could this leak my details" has the strongest possible answer: the
prompt has no field to put them in.
"""
from __future__ import annotations

SYSTEM = (
    "You extract structured facts from Australian job advertisements. "
    "Report only what the advertisement states. If the advertisement does not "
    "state something, omit the field entirely -- never infer, never fill a gap "
    "with what is typical for the role. Skills must be named technologies, "
    "tools, languages or named methodologies, not soft skills or adjectives. "
    "List at most 20 skills and at most 15 requirements, keeping each "
    "requirement under 200 characters."
)

# Architecture 6.3 names the grad fields worth having. graduation_window_years
# is a two-sided check: "must have graduated within the last 2 years" is a real
# and common AU criterion that can exclude you for graduating too long ago, not
# only for graduating too recently.
#
# Structured outputs accept a restricted subset of JSON Schema. The API
# rejected maxItems outright ("For 'array' type, property 'maxItems' is not
# supported"), so every validation keyword whose support is unconfirmed is left
# out rather than discovered one 400 at a time: no maxItems, no maxLength, no
# type unions. SUPPORTED_KEYWORDS below is enforced by a test.
#
# The caps those keywords used to express now live in the system prompt, and
# max_tokens is the hard ceiling underneath it -- a model that ignores the
# instruction costs a bounded number of output tokens, not an unbounded one.
#
# Optional fields are simply absent from `required` rather than nullable: the
# model omits what the ad does not state, and data.get() reads that as None.
SUPPORTED_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items",
    "description", "enum",
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["skills", "requirements"],
    "properties": {
        "skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Named technologies, languages, tools or frameworks. At most 20.",
        },
        "requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Stated requirements, one per entry, quoted or closely paraphrased. At most 15.",
        },
        "degree_requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Degree or field-of-study requirements, if stated.",
        },
        "graduation_window_years": {
            "type": "integer",
            "description": "Max years since graduation the ad allows. Omit if not stated.",
        },
        "years_experience_min": {
            "type": "integer",
            "description": "Minimum years of experience stated. Omit if not stated.",
        },
        "is_grad_program": {
            "type": "boolean",
            "description": "A structured graduate/intern cohort with an intake, not merely a junior role.",
        },
        "intake_year": {
            "type": "integer",
            "description": "Cohort intake year. Omit if not stated.",
        },
    },
}

MAX_DESCRIPTION_CHARS = 12000


def build_user(title: str, company: str | None, description: str | None) -> str:
    """The ad, trimmed. Long descriptions are mostly boilerplate at the end --
    benefits, EEO statements, application instructions -- and the requirements
    are near the top, so a head trim loses little and caps the input cost.
    """
    body = (description or "").strip()
    if len(body) > MAX_DESCRIPTION_CHARS:
        body = body[:MAX_DESCRIPTION_CHARS] + "\n[truncated]"
    return (
        f"Title: {title}\n"
        f"Company: {company or 'unknown'}\n"
        f"---\n{body}\n---\n"
        "Extract the structured facts."
    )
