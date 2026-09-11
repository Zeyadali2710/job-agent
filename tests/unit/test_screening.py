import pytest
from job_agent.screening import clearance, location, salary, seniority


@pytest.mark.parametrize("raw,expected", [
    ("Sydney, NSW", "SYDNEY_METRO"),
    ("Barangaroo, Sydney", "SYDNEY_CBD"),
    ("Melbourne, VIC", "OTHER_CAPITAL"),
    ("Newcastle NSW 2300", "NSW_REGIONAL"),
    ("Remote - Australia", "REMOTE_AU"),
    ("London, UK", "REQUIRES_RELOCATION"),
    ("", "UNKNOWN"),
])
def test_location(raw, expected):
    assert location.classify(raw)[0] == expected


def test_hybrid_days():
    cls, days = location.classify("Sydney - hybrid", "3 days in the office each week")
    assert cls == "HYBRID_SYDNEY" and days == 3


@pytest.mark.parametrize("title,expected", [
    ("Graduate Software Engineer", "GRADUATE"),
    ("Software Engineer Intern", "INTERN"),
    ("Junior Developer", "JUNIOR"),
    ("Senior Software Engineer", "SENIOR"),
    ("Associate Director, Risk", "SENIOR"),
    ("Associate Software Engineer", "JUNIOR"),
    ("Head of Engineering", "LEAD"),
])
def test_seniority(title, expected):
    assert seniority.classify(title)[0] == expected


def test_seniority_conflict():
    cls, yrs, conflict = seniority.classify(
        "Graduate Engineer", "We require 5+ years of production experience")
    assert cls == "GRADUATE" and yrs == 5 and conflict is True


@pytest.mark.parametrize("text,base,pkg_gt_base", [
    ("$70,000 + super", 70000, True),
    ("$70k plus superannuation", 70000, True),
    ("$80k package", 80000, False),
    ("$90,000 - $110,000", 90000, True),
])
def test_salary(text, base, pkg_gt_base):
    s = salary.parse(text)
    assert s["base_salary_min"] == base
    assert (s["total_package_min"] > base) == pkg_gt_base


def test_salary_daily_flagged():
    s = salary.parse("$650/day contract rate")
    assert s["period"] == "DAILY" and s["confidence"] == "INFERRED"


def test_package_vs_plus_super_ordering():
    a = salary.parse("$80k package")["total_package_min"]
    b = salary.parse("$78,000 + super")["total_package_min"]
    assert b > a, "a package figure must not outrank a higher base + super"


@pytest.mark.parametrize("text,status,expected", [
    ("Must be an Australian citizen", "TEMP_GRADUATE_485", "INCOMPATIBLE"),
    ("Must be an Australian citizen", "CITIZEN", "COMPATIBLE"),
    ("No sponsorship available", "REQUIRES_SPONSORSHIP", "INCOMPATIBLE"),
    ("Sponsorship available for the right candidate", "REQUIRES_SPONSORSHIP", "SPONSORSHIP_POSSIBLE"),
    ("Citizens or PR preferred", "TEMP_GRADUATE_485", "LIKELY_COMPATIBLE"),
    ("Great team, great culture", "CITIZEN", "UNCERTAIN"),
])
def test_work_rights(text, status, expected):
    from job_agent.screening.work_rights import classify
    assert classify(text, status)[0] == expected


def test_clearance_citizenship_block():
    _, strength, _, block = clearance.classify(
        "Must currently hold an NV1 clearance", "IN")
    assert strength == "MANDATORY_HELD" and block is True
    _, _, _, block_au = clearance.classify(
        "Must currently hold an NV1 clearance", "AU")
    assert block_au is False


def test_clearance_preferred_never_blocks():
    _, strength, _, block = clearance.classify(
        "A security clearance is desirable", "IN")
    assert strength == "PREFERRED" and block is False


def test_description_never_drives_location():
    """An overseas job whose description mentions remote work in Australia
    must not classify as locally acceptable."""
    from job_agent.screening import location
    desc = ("Megaport is an Australian company. This role offers remote "
            "and hybrid working arrangements across our global offices.")
    cls, _ = location.classify("Sao Paulo", desc)
    assert cls == "REQUIRES_RELOCATION", f"got {cls}"
    cls, _ = location.classify("London, United Kingdom", desc)
    assert cls == "REQUIRES_RELOCATION", f"got {cls}"
    cls, _ = location.classify("Remote - Australia", desc)
    assert cls == "REMOTE_AU", f"got {cls}"


def test_year_in_description_is_not_a_postcode():
    """A founding year like 2019 falls in the Sydney postcode range and must
    never be read as a location signal from the description."""
    from job_agent.screening import location
    desc = "Latitude.sh was launched in 2019 and operates bare metal globally."
    assert location.classify("Sao Paulo", desc)[0] == "REQUIRES_RELOCATION"
    assert location.classify("Chicago, Illinois, United States", desc)[0] == "REQUIRES_RELOCATION"
