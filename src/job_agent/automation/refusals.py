"""What the browser layer refuses to touch, and why.

From the brief, quoted in architecture 0.3: "I'm not going to write code
specifically designed to defeat SEEK's or LinkedIn's bot detection, and the
browser layer will refuse to touch CAPTCHA, MFA or rate-limit controls."

This is the enforcement of that sentence rather than a restatement of it. Three
things are refused:

- **CAPTCHA.** A control whose entire purpose is to establish that a person is
  present. Solving it automatically is a lie about who is applying.
- **MFA.** Entering a second factor on someone's behalf defeats the factor.
- **Rate limiting.** "You are doing that too often" is the site asking you to
  stop. Working around it is the definition of abuse, and it is also how an
  account gets banned.

A refusal is not a failure. The attempt stops, the application is flagged for
you to finish by hand, and nothing is retried -- retrying is the behaviour
these controls exist to prevent.

Pure functions over page text. No browser, no network.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

CAPTCHA = "CAPTCHA"
MFA = "MFA"
RATE_LIMIT = "RATE_LIMIT"
LOGIN_WALL = "LOGIN_WALL"

# Matched against page text and against attribute values, so both a visible
# challenge and an invisible widget are caught.
PATTERNS: dict[str, re.Pattern] = {
    CAPTCHA: re.compile(
        r"\b(recaptcha|hcaptcha|captcha|turnstile|cf-challenge|"
        r"are you a robot|i'?m not a robot|prove you'?re human|"
        r"verify you are (a )?human)\b", re.I),
    MFA: re.compile(
        r"\b(two[- ]factor|2fa|multi[- ]factor|mfa|one[- ]time (code|password)|"
        r"\botp\b|authenticator app|verification code (was )?sent|"
        r"security code we sent)\b", re.I),
    RATE_LIMIT: re.compile(
        r"\b(too many requests|rate limit(ed)?|slow down|"
        r"you('| a)re doing that too (often|much)|try again in \d+|"
        r"temporarily blocked|unusual traffic)\b", re.I),
    LOGIN_WALL: re.compile(
        r"\b(sign in to (apply|continue)|log ?in to (apply|continue)|"
        r"create an account to apply|you must be signed in)\b", re.I),
}

# 429 is the protocol saying the same thing as the prose.
STATUS_REFUSALS = {429: RATE_LIMIT, 403: RATE_LIMIT}

EXPLANATIONS = {
    CAPTCHA: "the page presents a CAPTCHA, which exists to establish that a "
             "person is present; solving it automatically would be a lie about "
             "who is applying",
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
        return f"{self.kind}: {EXPLANATIONS.get(self.kind, 'refused')}"

    def __str__(self) -> str:
        return self.reason


def _excerpt(text: str, m: re.Match, width: int = 60) -> str:
    start = max(0, m.start() - width // 2)
    return " ".join(text[start:m.end() + width // 2].split())


def inspect(page_text: str, *, status: int | None = None,
            attributes: Mapping[str, str] | None = None) -> Refusal | None:
    """The first reason to refuse this page, or None.

    `attributes` carries things that never appear in visible text -- a
    recaptcha script src, an iframe title -- because a CAPTCHA that has not
    rendered yet is still a CAPTCHA.
    """
    if status is not None and status in STATUS_REFUSALS:
        return Refusal(STATUS_REFUSALS[status], f"HTTP {status}")

    haystacks = [page_text or ""]
    haystacks += [f"{k}={v}" for k, v in (attributes or {}).items()]

    # Order matters: CAPTCHA and MFA are about who is applying and outrank a
    # rate limit, which is about how often.
    for kind in (CAPTCHA, MFA, LOGIN_WALL, RATE_LIMIT):
        pattern = PATTERNS[kind]
        for hay in haystacks:
            m = pattern.search(hay)
            if m:
                return Refusal(kind, _excerpt(hay, m))
    return None


def may_automate(policy: str) -> bool:
    """sources.automation_policy, enforced in code rather than documented.

    Architecture 5: BROWSER_ALLOWED is the only value that permits driving a
    form. API_ONLY means the source is read through its API and applications go
    elsewhere; MANUAL_ONLY means you apply yourself.
    """
    return str(policy or "").upper() == "BROWSER_ALLOWED"
