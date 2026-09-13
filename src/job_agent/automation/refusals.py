"""What the browser layer refuses to touch, and why.

From the brief, quoted in architecture 0.3: "I'm not going to write code
specifically designed to defeat SEEK's or LinkedIn's bot detection, and the
browser layer will refuse to touch CAPTCHA, MFA or rate-limit controls."

The first version of this module refused any page that *mentioned* recaptcha or
hcaptcha. Fetching three real application forms showed that refuses everything:

    Greenhouse  GOOGLE_RECAPTCHA_INVISIBLE_KEY
    Ashby       .grecaptcha-badge { visibility: hidden }   (reCAPTCHA v3)
    Lever       js.hcaptcha.com/1/secure-api.js, onSuccess clicks submit itself

Every one of them loads invisible, score-based captcha that never shows a
challenge to an ordinary visitor. A rule keyed on the script being present
refuses 100% of real forms and the browser layer does nothing at all.

So the line is drawn where the brief actually draws it: at a *control*, not at
fraud scoring in the background.

- Infrastructure present, no challenge shown -> proceed, and record that it is
  there. We are not touching it; the site is scoring traffic, which it may do.
- A challenge actually presented -> refuse. That is a control whose purpose is
  to establish a person is present, and answering it automatically is a lie
  about who is applying.

Whether a challenge is *presented* cannot be decided from static HTML -- it is
a question about what rendered and is visible -- so it is split in two:
`inspect()` reads text and status, and `challenge_presented()` is what the
browser calls with the elements it can actually see.

MFA and rate limiting need no such subtlety. Entering a second factor defeats
the factor, and "you are doing that too often" is the site asking us to stop.

Pure functions. No browser, no network.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

CAPTCHA = "CAPTCHA"
MFA = "MFA"
RATE_LIMIT = "RATE_LIMIT"
LOGIN_WALL = "LOGIN_WALL"

# A challenge being *presented*, in words a visitor would read. Note what is
# absent: the bare product names. "recaptcha" in a script tag is infrastructure.
CHALLENGE_TEXT = re.compile(
    r"\b(are you a robot|i'?m not a robot|prove you'?re (a )?human|"
    r"verify you are (a )?human|complete the (captcha|security check)|"
    r"select all (images|squares)|security check required|"
    r"please complete the (captcha|challenge))\b", re.I)

# Elements that only exist once a challenge is actually being shown. The
# harness checks these for visibility; presence alone is not enough, because
# the invisible variants inject empty containers on every page.
CHALLENGE_SELECTORS = (
    'iframe[src*="recaptcha/api2/bframe"]',      # the v2 challenge popup
    'iframe[src*="hcaptcha.com/captcha"]',       # hCaptcha challenge frame
    'iframe[title*="challenge" i]',
    "#challenge-stage",                          # Cloudflare interstitial
    ".cf-challenge-running",
)

# Present on nearly every real application form. Recorded, never refused.
INFRASTRUCTURE = re.compile(
    r"(recaptcha|hcaptcha|turnstile|grecaptcha)", re.I)

PATTERNS: dict[str, re.Pattern] = {
    CAPTCHA: CHALLENGE_TEXT,
    MFA: re.compile(
        r"\b(two[- ]factor|2fa|multi[- ]factor|one[- ]time (code|password)|"
        r"\botp\b|authenticator app|verification code (was )?sent|"
        r"security code we sent|enter the code we sent)\b", re.I),
    RATE_LIMIT: re.compile(
        r"\b(too many requests|rate limit(ed)?|slow down|"
        r"you('| a)re doing that too (often|much)|try again in \d+|"
        r"temporarily blocked|unusual traffic)\b", re.I),
    LOGIN_WALL: re.compile(
        r"\b(sign in to (apply|continue)|log ?in to (apply|continue)|"
        r"create an account to apply|you must be signed in)\b", re.I),
}

STATUS_REFUSALS = {429: RATE_LIMIT, 403: RATE_LIMIT}

EXPLANATIONS = {
    CAPTCHA: "a CAPTCHA challenge is being presented, and it exists to "
             "establish that a person is present; answering it automatically "
             "would be a lie about who is applying",
    MFA: "the page asks for a second authentication factor; entering it "
         "automatically would defeat the factor",
    RATE_LIMIT: "the site is asking us to stop; working around that is abuse "
                "and is also how an account gets banned",
    LOGIN_WALL: "the form is behind a sign-in the system has no credentials for",
}


@dataclass(frozen=True)
class Refusal:
    kind: str
    evidence: str

    @property
    def reason(self) -> str:
        """The canned explanation for a known control, and the evidence itself
        for anything else.

        Falling back to the word "refused" threw away the only useful part:
        "INCOMPLETE_FORM: refused" tells you nothing, while
        "INCOMPLETE_FORM: 3 fields filled, 1 required and unanswered: mystery"
        is the whole message. This string is what reaches the Discord alert.
        """
        return f"{self.kind}: {EXPLANATIONS.get(self.kind) or self.evidence}"

    def __str__(self) -> str:
        return self.reason


def _excerpt(text: str, m: re.Match, width: int = 60) -> str:
    start = max(0, m.start() - width // 2)
    return " ".join(text[start:m.end() + width // 2].split())


def has_captcha_infrastructure(page_text: str) -> bool:
    """True on essentially every real application form. Worth recording on the
    attempt so that a later refusal is not a surprise, and worth never
    refusing on."""
    return bool(INFRASTRUCTURE.search(page_text or ""))


def inspect(page_text: str, *, status: int | None = None,
            attributes: Mapping[str, str] | None = None) -> Refusal | None:
    """The first reason to refuse, from text and status alone, or None.

    Deliberately cannot see an unrendered challenge -- that is
    `challenge_presented`'s job, because visibility is a browser question.
    """
    if status is not None and status in STATUS_REFUSALS:
        return Refusal(STATUS_REFUSALS[status], f"HTTP {status}")

    haystacks = [page_text or ""]
    haystacks += [f"{k}={v}" for k, v in (attributes or {}).items()]

    # Identity outranks frequency: a page with both a challenge and a rate
    # limit is recorded as the challenge.
    for kind in (CAPTCHA, MFA, LOGIN_WALL, RATE_LIMIT):
        pattern = PATTERNS[kind]
        for hay in haystacks:
            m = pattern.search(hay)
            if m:
                return Refusal(kind, _excerpt(hay, m))
    return None


def challenge_presented(visible_selectors: Iterable[str]) -> Refusal | None:
    """Called by the harness with the CHALLENGE_SELECTORS it found *visible*.

    Presence is not enough. The invisible variants inject empty containers on
    every page, so a harness that checked for existence rather than visibility
    would reproduce the bug this module was rewritten to fix.
    """
    for sel in visible_selectors or ():
        return Refusal(CAPTCHA, f"visible challenge element: {sel}")
    return None


def may_automate(policy: str) -> bool:
    """sources.automation_policy, enforced in code rather than documented.

    Architecture 5: BROWSER_ALLOWED is the only value that permits driving a
    form. API_ONLY means the source is read through its API and applications
    go elsewhere; MANUAL_ONLY means you apply yourself.
    """
    return str(policy or "").upper() == "BROWSER_ALLOWED"
