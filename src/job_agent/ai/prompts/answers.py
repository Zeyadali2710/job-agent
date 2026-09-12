"""Application free-text answers. Architecture 9, third anti-fabrication
mechanism: "the answerer must first emit can_answer true|false with the
supporting source_ids. false -> the question is written to open_questions and
surfaced to you; the application cannot be submitted while any required question
is unanswered."

That is the mechanism behind "if information is missing, stop and ask me", so
`can_answer: false` is a first-class success here, not a failure. A model that
guesses politely is worse than one that declines: the guess reaches an employer
with your name on it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from job_agent.ai.prompts.cover_letter import render_facts

SYSTEM = (
    "You answer job-application questions on behalf of an Australian graduate "
    "applicant, using Australian English.\n\n"
    "You are given the job advertisement, a list of facts about the applicant, "
    "and a list of questions. For each question you must decide honestly "
    "whether the supplied facts actually answer it.\n\n"
    "Absolute rules:\n"
    "- Set can_answer to false whenever the facts do not contain the answer. "
    "This is the correct outcome, not a failure. Do not guess, do not infer "
    "from what is typical, and do not answer from general knowledge about the "
    "applicant's field. Questions about salary expectations, notice periods, "
    "referees, visa numbers, addresses or dates of birth are almost never "
    "answerable from the facts supplied.\n"
    "- When can_answer is false, leave answer_text empty and source_ids empty.\n"
    "- When can_answer is true, every claim in answer_text must appear in the "
    "cited facts or the advertisement, and source_ids must list the exact ids "
    "you used. Never state a number, technology, employer or date that is not "
    "in them.\n"
    "- Answer in at most 120 words unless the question asks for less.\n"
    "- Return exactly one entry per question, with the question_key copied "
    "verbatim from the input.\n"
    "- Plain prose. No markdown."
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answers"],
    "properties": {
        "answers": {
            "type": "array",
            "description": "Exactly one entry per question asked, same order.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question_key", "can_answer", "answer_text", "source_ids"],
                "properties": {
                    "question_key": {
                        "type": "string",
                        "description": "Copied verbatim from the question list.",
                    },
                    "can_answer": {
                        "type": "boolean",
                        "description": (
                            "True only if the supplied facts actually answer the "
                            "question. False is a correct and expected outcome."
                        ),
                    },
                    "answer_text": {
                        "type": "string",
                        # Empty string rather than null: the restricted schema
                        # subset has no type unions, and the DB CHECK
                        # (NOT can_answer OR answer_text IS NOT NULL) only
                        # constrains the answerable case.
                        "description": "The answer, or an empty string when can_answer is false.",
                    },
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact fact ids used. Empty when can_answer is false.",
                    },
                },
            },
        },
    },
}

MAX_DESCRIPTION_CHARS = 6000


def render_questions(questions: Sequence[Mapping]) -> str:
    return "\n".join(
        f"{q['question_key']} | {q['question_text']}" for q in questions)


def build_user(title: str, company: str | None, description: str | None,
               facts: Sequence[Mapping], questions: Sequence[Mapping]) -> str:
    body = (description or "").strip()
    if len(body) > MAX_DESCRIPTION_CHARS:
        body = body[:MAX_DESCRIPTION_CHARS] + "\n[truncated]"
    return (
        f"ADVERTISEMENT\nTitle: {title}\nCompany: {company or 'the company'}\n"
        f"---\n{body}\n---\n\n"
        f"FACTS ABOUT THE APPLICANT (id | fact)\n{render_facts(facts)}\n\n"
        f"QUESTIONS (question_key | question)\n{render_questions(questions)}\n\n"
        "Answer each question, or set can_answer false where the facts do not "
        "answer it."
    )
