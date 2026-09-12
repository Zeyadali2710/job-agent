from __future__ import annotations

import re

TERMS = {
    # Bare \bbaseline\b matches "baseline performance" and "baseline salary".
    # Require an actual vetting word within 30 characters, either order.
    "Baseline": r"\bbaseline\b[\s\S]{0,30}?\b(?:clearance|vetting)\b|"
                r"\b(?:clearance|vetting)\b[\s\S]{0,30}?\bbaseline\b",
    "NV1": r"\bnv1\b|\bnegative vetting 1\b",
    "NV2": r"\bnv2\b|\bnegative vetting 2\b",
    "PV": r"\bpositive vetting\b|\bpv clearance\b",
    "TSPV": r"\btspv\b",
    "AGSVA": r"\bagsva\b",
    "GENERIC": r"\b(defence clearance|australian government security clearance|security clearance)\b",
}
MUST_HOLD = re.compile(r"\b(must (currently )?hold|current(ly)? (hold|holding)|active clearance)\b", re.I)
OBTAINABLE = re.compile(r"\b(must be able to obtain|willing(ness)? to obtain|eligible to obtain)\b", re.I)
SPONSORED = re.compile(r"\b(agsva sponsorship provided|we will sponsor.{0,30}clearance|clearance will be sponsored)\b", re.I)
PREFERRED = re.compile(r"\b(clearance (is )?(desirable|preferred|advantageous)|highly regarded)\b", re.I)

# AGSVA is an Australian scheme. An ad wanting a "U.S. Secret security
# clearance" is not asking for one, and reading it as one screened out 1,056
# American defence listings under an Australian citizenship reason.
FOREIGN = re.compile(r"U\.S\.|\b(?:US|USA|UK|DoD|NATO)\b")
FOREIGN_WORDS = re.compile(
    r"\b(?:united states|american|british|canadian|five eyes)\b", re.I)


def _mentioned_as_agsva(text: str, pat: str) -> bool:
    """True if the term appears at least once with no foreign jurisdiction
    marker in the 60 characters before it.

    ponytail: 60 chars is a proximity window, not parsing. A US ad that says
    only "an active Top Secret clearance", with no U.S. marker nearby, still
    reads as AGSVA. Add "top secret"/"SCI" to FOREIGN_WORDS if the re-screen
    shows that happening -- measure before widening, because the same widening
    would unblock an Australian ad that says "NV2 (Top Secret)".
    """
    for m in re.finditer(pat, text, re.I):
        before = text[max(0, m.start() - 60):m.start()]
        if not (FOREIGN.search(before) or FOREIGN_WORDS.search(before)):
            return True
    return False


def classify(text: str | None, profile_citizenship: str) -> tuple[list[str], str, str | None, bool]:
    """Return (detected, strength, clause, hard_block)."""
    if not text:
        return [], "UNCLEAR", None, False
    detected = [k for k, pat in TERMS.items() if _mentioned_as_agsva(text, pat)]
    if not detected:
        return [], "UNCLEAR", None, False

    m = re.search(TERMS[detected[0]], text, re.I)
    clause = text[max(0, m.start() - 120):m.end() + 120].strip() if m else None

    if SPONSORED.search(text):
        strength = "SPONSORED_AFTER_HIRE"
    elif MUST_HOLD.search(text):
        strength = "MANDATORY_HELD"
    elif OBTAINABLE.search(text):
        strength = "MANDATORY_OBTAINABLE"
    elif PREFERRED.search(text):
        strength = "PREFERRED"
    else:
        strength = "UNCLEAR"

    # AGSVA clearances require Australian citizenship, full stop. Employer
    # sponsorship pays for the vetting, it does not waive the citizenship
    # requirement, so SPONSORED_AFTER_HIRE blocks a non-citizen just as hard.
    blocking = strength.startswith("MANDATORY") or strength == "SPONSORED_AFTER_HIRE"
    hard_block = blocking and profile_citizenship != "AU"
    return detected, strength, clause, hard_block
