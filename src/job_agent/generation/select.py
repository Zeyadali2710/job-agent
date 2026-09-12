"""Resume selection. Architecture 14 phase 12: "Resume selection engine".

Deterministic and model-free, which is the whole point: selection reorders facts
that already exist, so it is structurally incapable of inventing one. The LLM is
only ever handed the *result* of this, never the full profile, so a cover letter
can only draw on material that survived the ranking.

Pure functions. No DB, no network, no clock.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# A term is worth matching only if it is specific enough to mean something.
# "a", "or", "and" match everything; so does "it" inside "monitoring".
MIN_TERM = 2
_WORD = re.compile(r"[a-z0-9+#./&-]+")

# Terms that appear in nearly every ad and therefore rank nothing.
GENERIC_TERMS = frozenset("""
communication teamwork team work working ability able strong good excellent
experience experienced knowledge understanding skills skill years year
software development developer engineer engineering technical technology
computer science degree university problem solving written verbal
""".split())


@dataclass(frozen=True)
class Fact:
    """One row of app.profile_facts, narrowed to what selection needs."""
    id: str
    kind: str
    body: str
    tags: tuple[str, ...] = ()
    data: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class Selection:
    experience: tuple[str, ...]   # experience_bullet ids, chronological
    projects: tuple[str, ...]     # project_bullet ids, chronological
    skills: tuple[str, ...]       # skill fact ids, job-relevant first
    star: tuple[str, ...]         # star ids, most relevant first
    education: tuple[str, ...]    # education ids, chronological

    @property
    def all_ids(self) -> tuple[str, ...]:
        return (*self.education, *self.experience, *self.projects,
                *self.skills, *self.star)


def terms(*sources: Iterable[str]) -> set[str]:
    """The job's vocabulary, lowercased and stripped of words that rank nothing."""
    out: set[str] = set()
    for src in sources:
        for item in src or ():
            for w in _WORD.findall(str(item).lower()):
                if len(w) >= MIN_TERM and w not in GENERIC_TERMS:
                    out.add(w)
    return out


def _haystack(f: Fact) -> str:
    return f"{f.body} {' '.join(f.tags)}".lower()


def relevance(f: Fact, job_terms: set[str]) -> float:
    """How many distinct job terms this fact evidences.

    A bullet carrying a measured outcome breaks ties upward -- "reduced manual
    data entry by 90%" outranks an equally relevant bullet without a number,
    because that is the one worth the space on a page.
    """
    hay = _haystack(f)
    hits = sum(1 for t in job_terms if t in hay)
    return hits + (0.5 if any(c.isdigit() for c in f.body) else 0.0)


def _rank(facts: Sequence[Fact], job_terms: set[str], limit: int,
          keep_order: bool) -> tuple[str, ...]:
    """Top `limit` by relevance. `keep_order` restores the input order after
    selecting, so a resume still reads chronologically rather than by score."""
    scored = sorted(enumerate(facts), key=lambda p: (-relevance(p[1], job_terms), p[0]))
    chosen = scored[:limit]
    if keep_order:
        chosen = sorted(chosen, key=lambda p: p[0])
    return tuple(f.id for _, f in chosen)


def select(facts: Sequence[Fact], *, job_skills: Sequence[str] = (),
           job_requirements: Sequence[str] = (), job_title: str = "",
           max_experience: int = 8, max_projects: int = 6,
           max_skills: int = 14, max_star: int = 2) -> Selection:
    """Pick the facts this particular job should see.

    Education is never ranked -- a graduate resume leads with the degree whether
    or not the ad mentions it, and dropping it to make room for a better-matching
    bullet would be a worse resume, not a more relevant one.
    """
    jt = terms(job_skills, job_requirements, [job_title])
    by_kind: dict[str, list[Fact]] = {}
    for f in facts:
        by_kind.setdefault(f.kind, []).append(f)

    return Selection(
        experience=_rank(by_kind.get("experience_bullet", []), jt, max_experience, True),
        projects=_rank(by_kind.get("project_bullet", []), jt, max_projects, True),
        skills=_rank(by_kind.get("skill", []), jt, max_skills, False),
        star=_rank(by_kind.get("star", []), jt, max_star, False),
        education=tuple(f.id for f in by_kind.get("education", [])),
    )


def facts_by_id(facts: Sequence[Fact]) -> dict[str, str]:
    """The mapping the provenance validator checks citations against.

    Every fact goes in, not just the selected ones: citing a real fact that
    selection happened to drop is a worse document, not a fabrication, and
    UNKNOWN_SOURCE_ID must mean "no such fact" rather than "not shortlisted".
    """
    return {f.id: f.body for f in facts}


def selected_facts(facts: Sequence[Fact], sel: Selection) -> list[Fact]:
    """The chosen facts, in Selection order -- what the prompt is built from."""
    index = {f.id: f for f in facts}
    return [index[i] for i in sel.all_ids if i in index]
