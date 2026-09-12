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
    "state something, omit it or use null -- never infer, never fill a gap with "
    "what is typical for the role. Skills must be named technologies, tools, "
    "languages or named methodologies, not soft skills or adjectives."
)

# Architecture 6.3 names the grad fields worth having. graduation_window_years
# is a two-sided check: "must have graduated within the last 2 years" is a real
# and common AU criterion that can exclude you for graduating too long ago, not
# only for graduating too recently.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["skills", "requirements"],
    "properties": {
        "skills": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 40},
            "description": "Named technologies, languages, tools or frameworks.",
        },
        "requirements": {
            "type": "array",
            "maxItems": 15,
            "items": {"type": "string", "maxLength": 200},
            "description": "Stated requirements, one per entry, quoted or closely paraphrased.",
        },
        "degree_requirements": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 100},
        },
        "graduation_window_years": {
            "type": ["integer", "null"],
            "description": "Max years since graduation the ad allows, if stated.",
        },
        "years_experience_min": {"type": ["integer", "null"]},
        "is_grad_program": {
            "type": "boolean",
            "description": "A structured graduate/intern cohort with an intake, not merely a junior role.",
        },
        "intake_year": {"type": ["integer", "null"]},
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
