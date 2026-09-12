"""Answer reconciliation. Architecture 9's third anti-fabrication mechanism:
an unanswerable question must become an open question, never a guess.
"""
from pathlib import Path

from job_agent.generation.answers import load_questions, reconcile

QUESTIONS = [
    {"question_key": "why_us", "question_text": "Why us?", "required": True},
    {"question_key": "salary", "question_text": "Salary expectations?", "required": False},
]


def test_an_answered_question_is_carried_through_with_its_citations():
    [a, _] = reconcile(QUESTIONS, [
        {"question_key": "why_us", "can_answer": True,
         "answer_text": "Because of the work.", "source_ids": ["exp.a.b1"]}])
    assert a["can_answer"] and a["answer_text"] == "Because of the work."
    assert a["source_ids"] == ["exp.a.b1"]


def test_a_question_the_model_ignored_is_unanswerable_not_dropped():
    """Silence is not consent. Dropping it would quietly remove a required
    question from the set that blocks submission."""
    [_, salary] = reconcile(QUESTIONS, [
        {"question_key": "why_us", "can_answer": True, "answer_text": "x",
         "source_ids": ["a"]}])
    assert salary["can_answer"] is False and salary["missing_reply"] is True


def test_can_answer_true_with_an_empty_answer_is_not_an_answer():
    """The schema cannot express null, so "unanswerable" is an empty string.
    A model that sets the flag and then writes nothing must not produce a row
    the DB CHECK would reject and a human would read as ready."""
    [a, _] = reconcile(QUESTIONS, [
        {"question_key": "why_us", "can_answer": True, "answer_text": "   ",
         "source_ids": []}])
    assert a["can_answer"] is False and a["answer_text"] is None


def test_declining_clears_the_answer_and_the_citations():
    [a, _] = reconcile(QUESTIONS, [
        {"question_key": "why_us", "can_answer": False,
         "answer_text": "I would guess...", "source_ids": ["a"]}])
    assert a["answer_text"] is None and a["source_ids"] == []


def test_required_defaults_to_true_when_the_config_omits_it():
    """An unmarked question blocking submission is the safe default: the
    alternative is a question silently not counting."""
    [a] = reconcile([{"question_key": "k", "question_text": "?"}], [])
    assert a["required"] is True


def test_the_shipped_question_set_loads_and_is_well_formed():
    qs = load_questions(Path(__file__).resolve().parents[2] / "config" / "questions.yaml")
    assert len(qs) >= 5
    assert all(q["question_key"] and q["question_text"] for q in qs)
    assert len({q["question_key"] for q in qs}) == len(qs), "duplicate question_key"


def test_a_missing_question_file_is_empty_not_an_error():
    """The renderer image has no config mount. Absent config must degrade to
    "nothing to answer", not to a crash loop."""
    assert load_questions(Path("/nonexistent/questions.yaml")) == []
