"""Graduate-program detection. Architecture 6.3. Pure function, no DB."""
from datetime import datetime, timezone

import pytest

from job_agent.screening.grad import detect

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def d(title, description=None):
    return detect(title, description, now=NOW)


@pytest.mark.parametrize("title", [
    "2026 Graduate Program - Technology",
    "Graduate Programme (Engineering)",
    "Grad Program 2027",
    "Technology Graduate Scheme",
    "2026 Summer Vacation Program",
    "Vacationer - Cyber Security",
    "Summer Analyst, Markets",
    "Engineering Internship Program",
    "Cadetship - Civil",
    "Early Careers Program, Data",
    "Campus Recruiting - Software",
    "Graduate Intake 2026",
])
def test_australian_grad_program_titles_are_detected(title):
    """AU grad recruiting has its own vocabulary -- Vacation Program and
    Vacationer are standard here and absent from a US-shaped word list."""
    assert d(title)[0] is True


@pytest.mark.parametrize("title", [
    "Graduate Software Engineer",       # grad-LEVEL role, not a cohort program
    "Junior Developer",
    "Software Engineer Intern",         # an internship, not an internship program
    "Senior Engineer",
    "Program Manager",                  # "program" alone must not trigger
    "Graduate of Life - Marketing Lead",
    "",
])
def test_non_programs_are_not_flagged(title):
    """seniority.classify already scores a grad-level role through
    seniority_match. This flag is the narrower cohort-program signal, and
    double-counting one job as both would distort the ranking."""
    assert d(title)[0] is False


def test_description_boilerplate_cannot_create_a_grad_program():
    """The location.py lesson: descriptions say "recent graduates are
    encouraged to apply" constantly. The title decides."""
    boilerplate = ("We are an equal opportunity employer. Recent graduates are "
                   "encouraged to apply to our graduate program. You will be a "
                   "graduate of an accredited university.")
    assert d("Senior Backend Engineer", boilerplate)[0] is False
    assert d("Data Analyst", boilerplate)[0] is False


# ------------------------------------------------------------- intake year

def test_intake_year_comes_from_the_title():
    assert d("2026 Graduate Program")[1] == 2026
    assert d("Graduate Program 2027")[1] == 2027


def test_intake_year_falls_back_to_the_description_only_for_a_real_program():
    assert d("Graduate Program", "Applications for our 2027 intake close soon.")[1] == 2027
    # not a program, so nothing is extracted at all
    assert d("Senior Engineer", "Founded in 2026.") == (False, None)


def test_a_founding_year_cannot_become_an_intake_year():
    """Same shape as the postcode guard in location.py: a bare four-digit year
    in prose is not a signal. Only years in a plausible intake window count."""
    assert d("Graduate Program", "The firm was founded in 2011.")[1] is None
    assert d("Graduate Program", "Since 2019 we have hired 400 people.")[1] is None


def test_intake_window_is_bounded_around_now():
    assert d("Graduate Program 2025")[1] == 2025      # last year, still plausible
    assert d("Graduate Program 2028")[1] == 2028      # two years out
    assert d("Graduate Program 2029")[1] is None      # too far to be an intake
    assert d("Graduate Program 2015")[1] is None


def test_a_program_without_a_year_is_still_a_program():
    assert d("Graduate Program - Technology") == (True, None)


def test_the_real_posting_that_scored_52():
    """From production: this ranked below a Proposal Specialist because
    is_grad_program was never set by anything."""
    assert d("2026 Cloud Networking and Cyber Graduate Program") == (True, 2026)


@pytest.mark.parametrize("title,year", [
    ("2026 Software Engineering Intern", 2026),
    ("2026 Robotics Engineer Intern", 2026),
    ("2026 Mechanical Engineering Intern", 2026),
    ("2027 Graduate Analyst", 2027),
    ("Cadet Engineer 2026", 2026),
])
def test_a_year_plus_an_entry_marker_is_a_cohort_intake(title, year):
    """A year-prefixed entry-level title is a structured cohort with a
    deadline, even without the word "program". Three of these were in the top
    five of the real scored corpus and none were flagged."""
    assert d(title) == (True, year)


@pytest.mark.parametrize("title", [
    "Software Engineer Intern",          # no year: a one-off internship
    "Senior Engineer",
    "2026 Senior Backend Engineer",      # year but no entry-level marker
    "Graduate Software Engineer",        # grad level, no cohort year
])
def test_the_cohort_rule_needs_both_halves(title):
    assert d(title)[0] is False


def test_an_out_of_window_year_does_not_make_a_cohort():
    """The year must be a plausible intake, or "Intern (2015 archive)" style
    noise would qualify."""
    assert d("2015 Software Engineering Intern")[0] is False
