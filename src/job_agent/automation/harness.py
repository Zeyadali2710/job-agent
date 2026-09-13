"""Drive an application form, through a page driver rather than a browser.

The browser is behind `PageDriver` so everything that decides *what to do* --
refuse or proceed, which selector gets which value, whether the plan is
complete enough to submit -- is testable without Chromium. Only the handful of
methods that actually touch a page need Playwright, and those are the part with
no decisions in them.

That split matters more than usual here. This module's job is to stop before
doing something irreversible, and a safety property that can only be exercised
by starting a real browser is one that gets tested once and then trusted.

Order of operations, which is the whole protocol:

  1. open the page, read it
  2. refuse outright if a control is being presented (CAPTCHA, MFA, rate limit)
  3. parse the form, build a fill plan, refuse if it is incomplete
  4. fill
  5. re-read and refuse again -- filling is what triggers a challenge
  6. hand back a Prepared; the ENGINE writes SUBMIT_INTENT and decides
     whether to click

Step 6 is deliberate. This module never submits. The engine commits the
SUBMIT_INTENT row first and holds the DRY_RUN guard, so the decision to click
lives in one place rather than in every adapter.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dc_field
from typing import Protocol

from job_agent.automation import refusals
from job_agent.automation.adapters.ats import Applicant, plan_for
from job_agent.automation.fields import FillPlan, parse


class PageDriver(Protocol):
    """The only things the harness needs a browser for."""

    def goto(self, url: str) -> int:
        """Navigate. Returns the HTTP status."""

    def content(self) -> str:
        """The rendered DOM, which for Ashby is the only place the form exists."""

    def fill(self, selector: str, value: str) -> None: ...

    def set_files(self, selector: str, path: str) -> None: ...

    def visible(self, selectors: Sequence[str]) -> list[str]:
        """Which of these are present AND visible. Presence alone is not
        enough: the invisible captcha variants inject empty containers on
        every page."""

    def screenshot(self, path: str) -> None: ...


class Refused(RuntimeError):
    """Raised when the page is asking for something we will not do."""

    def __init__(self, refusal: refusals.Refusal) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal


@dataclass(frozen=True)
class PreparedForm:
    platform: str
    url: str
    plan: FillPlan
    captcha_infrastructure: bool
    screenshot_path: str = ""
    notes: Sequence[str] = dc_field(default_factory=tuple)


def _check(page_text: str, status: int | None, driver: PageDriver) -> None:
    r = refusals.inspect(page_text, status=status)
    if r is not None:
        raise Refused(r)
    r = refusals.challenge_presented(driver.visible(refusals.CHALLENGE_SELECTORS))
    if r is not None:
        raise Refused(r)


def prepare(driver: PageDriver, *, platform: str, url: str,
            applicant: Applicant, screenshot_path: str = "",
            policy: str = "BROWSER_ALLOWED") -> PreparedForm:
    """Open a form and fill it, or refuse. Never submits.

    `policy` is sources.automation_policy. Checked first, before anything is
    opened: a MANUAL_ONLY source should not even be visited by automation.
    """
    if not refusals.may_automate(policy):
        raise Refused(refusals.Refusal(
            "POLICY", f"automation_policy is {policy!r}, not BROWSER_ALLOWED"))

    status = driver.goto(url)
    html = driver.content()
    _check(html, status, driver)

    fields = parse(html)
    plan = plan_for(platform, fields, applicant)
    if not plan.complete:
        raise Refused(refusals.Refusal(
            "INCOMPLETE_FORM",
            f"{len(plan.actions)} fields filled, "
            f"{len(plan.unmapped)} required and unanswered: "
            f"{', '.join(plan.unmapped) or 'the form parsed as empty'}"))

    for action in plan.actions:
        if action.kind == "file":
            driver.set_files(action.selector, action.value)
        else:
            driver.fill(action.selector, action.value)

    # Filling is what trips a challenge. A page that was clean on arrival can
    # be asking for one by the time the last field is typed, so the check runs
    # again on what the form looks like now.
    after = driver.content()
    _check(after, None, driver)

    if screenshot_path:
        driver.screenshot(screenshot_path)

    return PreparedForm(
        platform=platform, url=url, plan=plan,
        captcha_infrastructure=refusals.has_captcha_infrastructure(after),
        screenshot_path=screenshot_path,
        notes=tuple(
            ([f"{plan.skipped_honeypots} honeypot field(s) left empty"]
             if plan.skipped_honeypots else [])
            + ([f"unanswered optional question: {c}" for c in plan.choices_left])))


def applicant_from(identity: Mapping, documents: Mapping[str, str],
                   answers: Mapping[str, str]) -> Applicant:
    """Assemble what a form asks for from what the system already holds.

    Nothing here is a field an employer would not receive anyway -- that is
    the same reasoning PublicProfile rests on, one layer further down.
    """
    full = str(identity.get("full_name") or "").strip()
    first, _, last = full.partition(" ")
    return Applicant(
        first_name=first, last_name=last or first, full_name=full,
        email=str(identity.get("email") or ""),
        phone=str(identity.get("phone") or identity.get("phone_display") or ""),
        location=", ".join(b for b in (identity.get("location_city"),
                                       identity.get("location_state")) if b),
        links={l.get("label", ""): l.get("url", "")
               for l in (identity.get("links") or []) if l.get("url")},
        resume_path=documents.get("RESUME", ""),
        cover_letter_path=documents.get("COVER_LETTER", ""),
        answers=dict(answers or {}),
    )
