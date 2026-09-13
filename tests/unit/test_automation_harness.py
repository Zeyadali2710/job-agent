"""The form-driving protocol, exercised without a browser.

The browser sits behind PageDriver so every decision -- refuse or proceed,
which selector gets which value, whether a plan may be submitted -- is tested
here. A safety property that can only be exercised by starting Chromium is one
that gets tested once and then trusted.

The fake driver serves the real captured ATS pages from tests/fixtures/ats.
"""
from pathlib import Path

import pytest

from job_agent.automation import refusals
from job_agent.automation.adapters.ats import Applicant
from job_agent.automation.harness import Refused, applicant_from, prepare

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats"

APPLICANT = Applicant(
    first_name="Zeyad", last_name="Ali", full_name="Zeyad Ali",
    email="z@example.test", phone="0400000000", location="Sydney, NSW",
    current_org="ArmsOA", links={"GitHub": "https://github.com/x"},
    resume_path="/app/artifacts/r.pdf", cover_letter_path="/app/artifacts/c.pdf",
    answers={"Website": "https://zeyad.dev"})


class FakeDriver:
    """Serves fixed HTML and records what was done to it."""

    def __init__(self, html, status=200, visible=(), after=None):
        self._html, self._status = html, status
        self._after = after
        self._visible = list(visible)
        self.filled, self.files, self.shots = {}, {}, []
        self._reads = 0

    def goto(self, url):
        self.url = url
        return self._status

    def content(self):
        self._reads += 1
        if self._after is not None and self._reads > 1:
            return self._after
        return self._html

    def fill(self, selector, value):
        self.filled[selector] = value

    def set_files(self, selector, path):
        self.files[selector] = path

    def visible(self, selectors):
        return [s for s in selectors if s in self._visible]

    def screenshot(self, path):
        self.shots.append(path)


def fixture(name):
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


# --------------------------------------------------------------- happy path

@pytest.mark.parametrize("platform", ["greenhouse", "lever"])
def test_a_real_form_is_filled_and_handed_back_unsubmitted(platform):
    d = FakeDriver(fixture(platform))
    form = prepare(d, platform=platform, url="https://x.test/apply",
                   applicant=APPLICANT, screenshot_path="/tmp/s.png")
    assert form.plan.complete and d.filled and d.files
    assert d.shots == ["/tmp/s.png"]
    # every real ATS form carries invisible captcha; recorded, not refused
    assert form.captcha_infrastructure is True


def test_greenhouse_honeypots_are_reported_and_left_empty():
    d = FakeDriver(fixture("greenhouse"))
    form = prepare(d, platform="greenhouse", url="https://x.test/apply",
                   applicant=APPLICANT)
    assert "2 honeypot field(s) left empty" in form.notes[0]
    assert "" not in d.filled.values()


def test_the_harness_never_submits():
    """It returns a PreparedForm. The engine writes SUBMIT_INTENT and holds the
    DRY_RUN guard, so the decision to click lives in exactly one place."""
    src = (Path(__file__).resolve().parents[2] / "src" / "job_agent"
           / "automation" / "harness.py").read_text(encoding="utf-8")
    assert "def submit" not in src
    assert "click" not in src.replace("whether to click", "").replace(
        "decision to click", "")


# ------------------------------------------------------------- the refusals

def test_a_presented_challenge_stops_everything():
    d = FakeDriver("<form></form>", visible=['iframe[src*="hcaptcha.com/captcha"]'])
    with pytest.raises(Refused) as e:
        prepare(d, platform="lever", url="https://x.test", applicant=APPLICANT)
    assert e.value.refusal.kind == refusals.CAPTCHA
    assert not d.filled, "nothing may be typed into a page we are refusing"


def test_a_rate_limited_page_stops_everything():
    d = FakeDriver("<html>slow down</html>", status=429)
    with pytest.raises(Refused) as e:
        prepare(d, platform="lever", url="https://x.test", applicant=APPLICANT)
    assert e.value.refusal.kind == refusals.RATE_LIMIT


def test_a_challenge_that_appears_only_after_filling_is_caught():
    """The realistic case: the page is clean on arrival and asks for a
    challenge once the form has been typed into. Checking only on arrival
    would sail straight past it."""
    d = FakeDriver(fixture("lever"),
                   after="<html>Please verify you are human</html>")
    with pytest.raises(Refused) as e:
        prepare(d, platform="lever", url="https://x.test", applicant=APPLICANT)
    assert e.value.refusal.kind == refusals.CAPTCHA


def test_a_manual_only_source_is_not_even_opened():
    d = FakeDriver(fixture("lever"))
    with pytest.raises(Refused):
        prepare(d, platform="lever", url="https://x.test", applicant=APPLICANT,
                policy="MANUAL_ONLY")
    assert not hasattr(d, "url"), "a MANUAL_ONLY source must not be visited"


def test_a_form_that_did_not_parse_is_refused_rather_than_submitted_blank():
    """Ashby's served HTML has no form. Without a rendered DOM there is
    nothing to fill, and an empty plan must never be handed onward."""
    d = FakeDriver(fixture("ashby"))
    with pytest.raises(Refused) as e:
        prepare(d, platform="ashby", url="https://x.test", applicant=APPLICANT)
    assert e.value.refusal.kind == "INCOMPLETE_FORM"


def test_an_unanswerable_required_field_refuses():
    d = FakeDriver('<form><input name="mystery" required></form>')
    with pytest.raises(Refused) as e:
        prepare(d, platform="lever", url="https://x.test", applicant=APPLICANT)
    assert "mystery" in str(e.value)


# ------------------------------------------------------------- assembly

def test_applicant_is_assembled_from_what_the_system_already_holds():
    a = applicant_from(
        {"full_name": "Zeyad Ali", "email": "z@example.test",
         "phone_display": "0400 000 000", "location_city": "Sydney",
         "location_state": "NSW",
         "links": [{"label": "GitHub", "url": "https://github.com/x"},
                   {"label": "Nothing", "url": ""}]},
        {"RESUME": "/r.pdf", "COVER_LETTER": "/c.pdf"},
        {"why_us": "because"})
    assert a.first_name == "Zeyad" and a.last_name == "Ali"
    assert a.location == "Sydney, NSW"
    assert a.links == {"GitHub": "https://github.com/x"}
    assert a.resume_path == "/r.pdf" and a.answers["why_us"] == "because"


def test_a_single_word_name_does_not_lose_its_surname_field():
    a = applicant_from({"full_name": "Prince"}, {}, {})
    assert a.first_name == "Prince" and a.last_name == "Prince"
