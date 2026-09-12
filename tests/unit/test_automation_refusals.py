"""What the browser layer refuses to touch.

From the brief: "the browser layer will refuse to touch CAPTCHA, MFA or
rate-limit controls". These tests are that sentence, enforced.
"""
import pytest

from job_agent.automation.refusals import (
    CAPTCHA, LOGIN_WALL, MFA, RATE_LIMIT, inspect, may_automate,
)


@pytest.mark.parametrize("text,kind", [
    ("Please verify you are human before continuing", CAPTCHA),
    ("I'm not a robot", CAPTCHA),
    ("Enter the one-time code we sent to your phone", MFA),
    ("Two-factor authentication is required", MFA),
    ("Too many requests. Try again in 60 seconds.", RATE_LIMIT),
    ("We have detected unusual traffic from your network", RATE_LIMIT),
    ("Sign in to apply for this role", LOGIN_WALL),
])
def test_controls_that_must_stop_the_browser(text, kind):
    r = inspect(text)
    assert r is not None and r.kind == kind


def test_an_ordinary_application_form_is_not_refused():
    assert inspect("Apply for Graduate Software Engineer. Upload your resume "
                   "and tell us why you are interested.") is None


def test_a_captcha_that_has_not_rendered_yet_is_still_a_captcha():
    """The widget's script tag is present before any challenge text is. A page
    that only looks clean because the challenge has not painted yet must not
    be driven."""
    r = inspect("Apply now", attributes={
        "script.src": "https://www.google.com/recaptcha/api.js"})
    assert r is not None and r.kind == CAPTCHA


def test_http_429_is_the_protocol_saying_the_same_thing_as_the_prose():
    r = inspect("", status=429)
    assert r is not None and r.kind == RATE_LIMIT


def test_who_is_applying_outranks_how_often():
    """A page with both a CAPTCHA and a rate-limit notice is refused as a
    CAPTCHA: one is about identity, the other about frequency, and the reason
    recorded should be the more fundamental."""
    r = inspect("Too many requests. Also, please prove you're human.")
    assert r.kind == CAPTCHA


def test_the_refusal_explains_itself_for_the_record():
    r = inspect("recaptcha")
    assert "person is present" in r.reason
    assert r.evidence


@pytest.mark.parametrize("policy,allowed", [
    ("BROWSER_ALLOWED", True),
    ("API_ONLY", False),
    ("MANUAL_ONLY", False),
    ("", False),
    (None, False),
    ("browser_allowed", True),
])
def test_automation_policy_is_enforced_not_merely_recorded(policy, allowed):
    assert may_automate(policy) is allowed
