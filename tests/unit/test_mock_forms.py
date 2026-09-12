"""The mock form suite itself: four pages, four outcomes.

These run without a browser -- urllib is enough to prove the server serves
what the refusal rules are supposed to catch. The browser harness is tested
against the same server in the image that has Playwright.
"""
import urllib.error
import urllib.request

import pytest

from job_agent.automation.refusals import CAPTCHA, MFA, RATE_LIMIT, inspect
from tests.mock_forms.server import serve


@pytest.fixture(scope="module")
def base():
    httpd, url = serve()
    yield url
    httpd.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_the_plain_form_is_drivable(base):
    status, body = get(f"{base}/form/plain")
    assert status == 200
    assert 'id="application"' in body and 'name="full_name"' in body
    assert inspect(body, status=status) is None


def test_the_captcha_form_is_refused(base):
    status, body = get(f"{base}/form/captcha")
    r = inspect(body, status=status)
    assert r is not None and r.kind == CAPTCHA


def test_the_mfa_form_is_refused(base):
    status, body = get(f"{base}/form/mfa")
    r = inspect(body, status=status)
    assert r is not None and r.kind == MFA


def test_the_rate_limited_form_is_refused_on_status_alone(base):
    status, body = get(f"{base}/form/limited")
    assert status == 429
    assert inspect(body, status=status).kind == RATE_LIMIT


def test_posting_records_a_submission_and_returns_a_reference(base):
    data = b"full_name=Zeyad&email=z%40example.test"
    with urllib.request.urlopen(f"{base}/submit", data=data) as r:
        body = r.read().decode()
    assert r.status == 200 and "Application received" in body and "MOCK-" in body
