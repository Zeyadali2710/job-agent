"""The anti-fabrication check. Architecture 9: "Prompting alone is not sufficient."

Pure functions, no I/O, no model. Two rules:

1. Every `source_id` a paragraph cites must resolve to a real profile fact, and a
   paragraph that cites nothing is unverifiable and therefore fails.
2. Every named entity and every number in the generated text must already appear
   in the union of the cited facts, the job advertisement, and a small set of
   explicitly allowed strings (your own name, the employer's name).

There is deliberately **no retry path**. Architecture 9 is explicit: a failure
quarantines the draft and alerts, it does not regenerate until something passes.
Retrying a fabrication check until it succeeds converts a safety property into a
sampling exercise.

The validator is intentionally biased toward false positives. A wrongly flagged
draft costs you one review; a missed fabrication costs you a claim on a document
sent to an employer under your name.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# A token is "entity-shaped" if it is Capitalised, ALLCAPS, or carries internal
# capitals or digits (FastAPI, PostgreSQL, NV2, S3, CI/CD).
# The dot and slash runs must be *followed* by more word characters, or a
# sentence-ending period gets swallowed and every final word ("tests.") looks
# like node.js to the distinctive-shape rule below.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#&'-]*(?:[./][A-Za-z0-9+#&'-]+)*")
# Numbers, including 90%, 8.5, 1,200 and 2026. The most dangerous thing a model
# invents is a metric, so every digit run must be accounted for.
_NUMBER = re.compile(r"\d[\d,.]*%?")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Capitalised words that carry no factual claim. Kept short on purpose: every
# entry here is a hole in the check, so a word earns its place only by being
# both common and contentless.
STOPWORDS = frozenset("""
a an the and or but if then than that this these those with without within
i i'm i've my me we our you your it its as at by for from in into of on to
was were is are be been being have has had do does did can could will would
should may might must shall not no nor so such very more most much many few
all any both each either neither every some other another same own just only
during while when where which who whom whose what how why because since until
after before between through across over under again further once here there
who's what's it's they them their he she his her
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november
december jan feb mar apr jun jul aug sep sept oct nov dec
dear sincerely regards yours kind best thank thanks please role position team
company work working experience project projects skills skill years year
opportunity opportunities application applying apply candidate
""".split())


@dataclass(frozen=True)
class Finding:
    kind: str      # NO_CITATION | UNKNOWN_SOURCE_ID | UNSUPPORTED_ENTITY | UNSUPPORTED_NUMBER
    detail: str

    def __str__(self) -> str:          # what lands in the Discord alert
        return f"{self.kind}: {self.detail}"


def _corpus(parts: Sequence[str]) -> str:
    """One lowercased haystack. Substring matching rather than token matching is
    deliberate: it accepts "CI/CD" and "Node.js" written any way the source
    wrote them, and the cost is only that it is more permissive, never less."""
    return " \n ".join(p for p in parts if p).lower()


def _entities(text: str) -> list[str]:
    """Entity-shaped tokens, skipping the first word of each sentence unless its
    own shape gives it away. Without that rule every sentence-initial verb --
    "Building", "Delivering" -- reads as a proper noun and the check drowns in
    false positives."""
    out: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        for i, m in enumerate(_TOKEN.finditer(sentence)):
            tok = m.group(0)
            distinctive = (
                tok.isupper() and len(tok) > 1          # REST, AWS, NV2
                or any(c.isdigit() for c in tok)        # S3, NV2, 3D
                or any(c.isupper() for c in tok[1:])    # FastAPI, PostgreSQL
                or not tok[0].isupper() and "." in tok  # node.js
            )
            if not tok[0].isupper() and not distinctive:
                continue
            if i == 0 and not distinctive:
                continue                                # sentence-initial word
            if tok.lower() in STOPWORDS:
                continue
            out.append(tok)
    return out


def _numbers(text: str) -> list[str]:
    return [m.group(0) for m in _NUMBER.finditer(text)]


def _number_supported(raw: str, corpus: str) -> bool:
    """Compare on digits alone. "90%" is supported by "90", "1,200" by "1200",
    and "8.5" only by "8.5" -- a trailing period from a sentence end is stripped
    so "delivered 741." does not become the unsupported number "741.".
    """
    core = raw.rstrip("%").rstrip(".").replace(",", "")
    if not core:
        return True
    return core in corpus.replace(",", "")


def check_text(text: str, *, source_ids: Sequence[str], facts: Mapping[str, str],
               job_text: str = "", extra_allowed: Sequence[str] = ()) -> list[Finding]:
    """Findings for one paragraph or answer. Empty list means it passed."""
    findings: list[Finding] = []

    if not source_ids:
        findings.append(Finding("NO_CITATION", f"{text[:80]!r} cites no source_ids"))

    unknown = [sid for sid in source_ids if sid not in facts]
    for sid in unknown:
        findings.append(Finding("UNKNOWN_SOURCE_ID", f"{sid!r} is not a profile fact"))

    cited = [facts[sid] for sid in source_ids if sid in facts]
    corpus = _corpus([*cited, job_text, *extra_allowed])

    for ent in _entities(text):
        if ent.lower() not in corpus:
            findings.append(Finding(
                "UNSUPPORTED_ENTITY",
                f"{ent!r} appears in no cited fact and not in the job ad"))
    for num in _numbers(text):
        if not _number_supported(num, corpus):
            findings.append(Finding(
                "UNSUPPORTED_NUMBER",
                f"{num!r} appears in no cited fact and not in the job ad"))
    return findings


PASSED = "PASSED"
SUSPECTED = "FABRICATION_SUSPECTED"


def validate(blocks: Sequence[Mapping], *, facts: Mapping[str, str],
             job_text: str = "", extra_allowed: Sequence[str] = ()
             ) -> tuple[str, list[Finding]]:
    """Validate a whole document.

    `blocks` are mappings with "text" and "source_ids" -- the shape the
    generation prompts are required to emit. Returns the value for
    generated_documents.validator_state and every finding, so the quarantine
    alert can say precisely what was wrong rather than "validation failed".
    """
    findings: list[Finding] = []
    for b in blocks:
        findings += check_text(
            str(b.get("text") or ""),
            source_ids=list(b.get("source_ids") or []),
            facts=facts, job_text=job_text, extra_allowed=extra_allowed)
    return (PASSED if not findings else SUSPECTED), findings


def cited_ids(blocks: Sequence[Mapping]) -> list[str]:
    """Every id the document cites, de-duplicated, order preserved -- this is
    what goes into generated_documents.source_ids."""
    seen: dict[str, None] = {}
    for b in blocks:
        for sid in b.get("source_ids") or []:
            seen.setdefault(str(sid), None)
    return list(seen)
