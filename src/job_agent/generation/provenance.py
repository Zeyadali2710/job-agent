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

# A model that cannot write "741" will happily write "seven hundred". Digits
# were checked from the start; spelled-out numbers were not, and "a five-person
# team" is exactly the kind of claim worth checking. Each word is supported by
# either the word or the digit appearing in the corpus, so a fact saying
# "5-person" supports "five-person" and vice versa.
NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80",
    "ninety": "90", "hundred": "100", "thousand": "1000",
}
_WORD_NUMBER = re.compile(
    r"\b(" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")\b", re.I)

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


def _word_numbers(text: str) -> list[str]:
    """"a five-person team" is a factual claim. "I have been here for one year"
    is not, but checking it costs nothing: the corpus almost always contains the
    word, and a false positive here is one review rather than a false claim."""
    return [m.group(0) for m in _WORD_NUMBER.finditer(text)]


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
    for word in _word_numbers(text):
        digit = NUMBER_WORDS[word.lower()]
        if word.lower() not in corpus and digit not in corpus:
            findings.append(Finding(
                "UNSUPPORTED_NUMBER",
                f"{word!r} (={digit}) appears in no cited fact and not in the job ad"))
    return findings


def merged_source_advisories(blocks: Sequence[Mapping],
                             parent_of: Mapping[str, str]) -> list[Finding]:
    """Advisory, never a quarantine: a paragraph resting on bullets from two
    different engagements.

    Entity and number checking is blind to this. The first real cover letter
    wrote "as Product Owner for a five-person capstone team, I directed the
    integration of five codebases" -- every entity real, both numbers cited,
    and the sentence still merged a five-person university project with a
    three-person client contract into one engagement. Prompting alone did not
    stop it.

    It cannot be a hard failure: a good cover letter legitimately draws on two
    roles in one paragraph. So this records what to look at, and the approval
    step is where a person decides.
    """
    out: list[Finding] = []
    for i, b in enumerate(blocks, 1):
        parents = {parent_of[s] for s in (b.get("source_ids") or ())
                   if s in parent_of}
        if len(parents) > 1:
            out.append(Finding(
                "MERGED_SOURCES",
                f"paragraph {i} draws on {len(parents)} separate engagements "
                f"({', '.join(sorted(parents))}); check it does not imply they "
                f"were one"))
    return out


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
