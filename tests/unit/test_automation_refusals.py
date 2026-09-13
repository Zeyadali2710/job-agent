"""What the browser layer refuses to touch.

From the brief: "the browser layer will refuse to touch CAPTCHA, MFA or
rate-limit controls". The first version of these tests passed while the rule
was wrong, because they were written against invented pages. The fixtures in
tests/fixtures/ats are real application forms, and they are what showed that
refusing on the presence of a captcha script refuses every real form.
"""
from pathlib import Path

import pytest

from job_agent.automation.refusals import (
    CAPTCHA, CHALLENGE_SELECTORS, LOGIN_WALL, MFA, RATE_LIMIT,
    challenge_presented, has_captcha_infrastructure, inspect, may_automate,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats"
REAL_FORMS = sorted(p.name for p in FIXTURES.glob("*.html"))


# ------------------------------------------------- the real forms must pass

@pytest.mark.parametrize("name", REAL_FORMS)
def test_a_real_application_form_is_not_refused(name):
    """Greenhouse, Lever and Ashby all load invisible, score-based captcha and
    none of them presents a challenge. Refusing on the script being present
    refuses 100% of real forms and the browser layer does nothing at all."""
    html = (FIXTURES / name).read_text(encoding="utf-8")
    r = inspect(html)
    assert r is None, f"{name} refused for {r}"


@pytest.mark.parametrize("name", REAL_FORMS)
def test_every_real_form_does_carry_captcha_infrastructure(name):
    """The thing we deliberately do not refuse on. If this ever stops being
    true the distinction this module rests on is worth revisiting."""
    html = (FIXTURES / name).read_text(encoding="utf-8")
    assert has_captcha_infrastructure(html)


# ------------------------------------------------- controls must stop the run

@pytest.mark.parametrize("text,kind", [
    ("Please verify you are human before continuing", CAPTCHA),
    ("I'm not a robot", CAPTCHA),
    ("Select all images with a bus", CAPTCHA),
    ("Enter the one-time code we sent to your phone", MFA),
    ("Two-factor authentication is required", MFA),
    ("Too many requests. Try again in 60 seconds.", RATE_LIMIT),
    ("We have detected unusual traffic from your network", RATE_LIMIT),
    ("Sign in to apply for this role", LOGIN_WALL),
])
def test_controls_that_must_stop_the_browser(text, kind):
    r = inspect(text)
    assert r is not None and r.kind == kind


def test_an_ordinary_form_with_no_controls_is_fine():
    assert inspect("Apply for Graduate Software Engineer. Upload your resume.") is None


def test_http_429_is_the_protocol_saying_what_the_prose_says():
    assert inspect("", status=429).kind == RATE_LIMIT


def test_who_is_applying_outranks_how_often():
    assert inspect("Too many requests. Also, please prove you're human.").kind == CAPTCHA


def test_the_refusal_explains_itself_for_the_record():
    r = inspect("please prove you're human")
    assert "person is present" in r.reason and r.evidence


# ------------------------------------------------- the rendered check

def test_a_visible_challenge_element_is_refused():
    r = challenge_presented(['iframe[src*="hcaptcha.com/captcha"]'])
    assert r is not None and r.kind == CAPTCHA


def test_no_visible_challenge_means_no_refusal():
    assert challenge_presented([]) is None


def test_the_selectors_target_challenge_frames_not_badges():
    """The badge and the empty container exist on every page. Only the
    challenge frame means a challenge is actually being asked."""
    joined = " ".join(CHALLENGE_SELECTORS)
    assert "bframe" in joined and "challenge" in joined
    assert "grecaptcha-badge" not in joined


# ------------------------------------------------- policy

@pytest.mark.parametrize("policy,allowed", [
    ("BROWSER_ALLOWED", True), ("browser_allowed", True),
    ("API_ONLY", False), ("MANUAL_ONLY", False), ("", False), (None, False),
])
def test_automation_policy_is_enforced_not_merely_recorded(policy, allowed):
    assert may_automate(policy) is allowed
