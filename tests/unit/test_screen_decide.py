"""The hard screen. Pure function, no DB.

screen_group is SQL around one decision; decide() is that decision. These pin
the gate order, which matters because the reason you see on the dashboard is
whichever gate fires first.
"""
import pytest
import yaml
from pathlib import Path

from job_agent.pipeline.screen import decide
from job_agent.profile.models import Preferences

PROFILE = Path(__file__).resolve().parents[2] / "profile"

PREFS = {
    "location_classes_accepted": ["SYDNEY_CBD", "SYDNEY_METRO", "GREATER_SYDNEY",
                                  "HYBRID_SYDNEY", "REMOTE_AU"],
    "allow_relocation": False,
    "salary_min": 70000,
    "salary_is_mandatory": False,
}
SAL = {"confidence": "STATED", "total_package_min": 100000}
OK = dict(wr_class="COMPATIBLE", cl_block=False, sen_class="GRADUATE",
          loc_class="SYDNEY_CBD", conflict=False, sal=SAL)


def d(prefs=None, **over):
    return decide({**PREFS, **(prefs or {})}, **{**OK, **over})


def test_clean_graduate_role_is_ready():
    assert d() == ("READY", None)


# ------------------------------------------------- the two changed behaviours

def test_uncertain_work_rights_proceeds_by_default():
    """Architecture 6: "Never auto-reject for ambiguity ... a large share of AU
    ads simply omit the clause." UNCERTAIN means no work-rights language was
    found at all. Holding those in review put 63 of 70 review items there."""
    assert d(wr_class="UNCERTAIN") == ("READY", None)


def test_uncertain_work_rights_can_still_be_held_for_review():
    assert d({"uncertain_policy": "review"}, wr_class="UNCERTAIN") == (
        "REVIEW_REQUIRED", "WORK_RIGHTS_UNCERTAIN")


def test_the_profile_on_disk_selects_the_proceed_policy():
    """The policy only works if the real profile carries the key and it
    survives Preferences(extra='forbid')."""
    prefs = Preferences(**yaml.safe_load((PROFILE / "preferences.yaml").read_text()))
    assert prefs.uncertain_policy == "proceed_with_flag"


def test_an_unknown_policy_value_is_rejected_not_defaulted():
    """A typo must fail in the profile validator. Falling back to a default
    would silently pick a screening policy nobody chose."""
    raw = yaml.safe_load((PROFILE / "preferences.yaml").read_text())
    with pytest.raises(Exception):
        Preferences(**{**raw, "uncertain_policy": "proceed"})


# ------------------------------------------------------------ gate precedence

def test_incompatible_work_rights_outrank_everything():
    assert d(wr_class="INCOMPATIBLE", sen_class="SENIOR", loc_class="UNKNOWN") == (
        "SCREENED_OUT", "WORK_RIGHTS_INCOMPATIBLE")


def test_clearance_block_outranks_seniority():
    assert d(cl_block=True, sen_class="SENIOR") == (
        "SCREENED_OUT", "CLEARANCE_CITIZENSHIP_CONFLICT")


@pytest.mark.parametrize("sen", ["SENIOR", "LEAD"])
def test_senior_titles_are_screened_out(sen):
    assert d(sen_class=sen) == ("SCREENED_OUT", "SENIORITY_TOO_HIGH")


def test_seniority_outranks_unknown_location():
    """Job 8956 in production: a Business Development Manager with no parsable
    location came back SENIORITY_TOO_HIGH, not LOCATION_UNKNOWN."""
    assert d(sen_class="SENIOR", loc_class="UNKNOWN") == (
        "SCREENED_OUT", "SENIORITY_TOO_HIGH")


def test_unknown_location_goes_to_review_never_screened_out():
    """Guessing a location is worse than asking. This must never become a
    SCREENED_OUT."""
    assert d(loc_class="UNKNOWN") == ("REVIEW_REQUIRED", "LOCATION_UNKNOWN")


def test_unaccepted_location_is_screened_out_with_the_class_in_the_reason():
    assert d(loc_class="REQUIRES_RELOCATION") == (
        "SCREENED_OUT", "LOCATION_REQUIRES_RELOCATION")


def test_other_capital_passes_only_when_relocation_is_allowed():
    assert d(loc_class="OTHER_CAPITAL") == ("SCREENED_OUT", "LOCATION_OTHER_CAPITAL")
    assert d({"allow_relocation": True}, loc_class="OTHER_CAPITAL") == ("READY", None)


def test_seniority_conflict_is_review_not_rejection():
    assert d(conflict=True) == ("REVIEW_REQUIRED", "SENIORITY_CONFLICT")


# ----------------------------------------------------------------- salary

def test_absent_salary_only_rejects_when_mandatory():
    absent = {"confidence": "ABSENT", "total_package_min": None}
    assert d(sal=absent) == ("READY", None)
    assert d({"salary_is_mandatory": True}, sal=absent) == (
        "SCREENED_OUT", "SALARY_NOT_STATED")


def test_salary_below_minimum_allows_ten_percent_slack():
    """70000 * 0.9 = 63000: at or above that passes, below it does not."""
    assert d(sal={"confidence": "STATED", "total_package_min": 63000}) == ("READY", None)
    assert d(sal={"confidence": "STATED", "total_package_min": 62999}) == (
        "SCREENED_OUT", "SALARY_BELOW_MINIMUM")


def test_a_fabricated_low_salary_would_reject_a_good_job():
    """Why the money-marker fix mattered: "Join our 50 person startup" used to
    parse as $50k and land here."""
    assert d(sal={"confidence": "STATED", "total_package_min": 56000}) == (
        "SCREENED_OUT", "SALARY_BELOW_MINIMUM")
