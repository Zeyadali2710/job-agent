"""Map an applicant onto a real ATS form.

Every selector here came from a page that was actually fetched; the evidence
is in tests/fixtures/ats with its provenance, and the tests run against it.
Nothing in this module was written from memory of what these forms look like.

The three platforms address their fields three different ways, which is why
the architecture has three adapters rather than one:

    Greenhouse  by id            #first_name #last_name #email #phone
                                 #resume #cover_letter (both type=file)
                                 plus two honeypots that must stay empty
    Lever       by name          name email phone org location
                                 resume (file), urls[LinkedIn] ...
                                 custom questions as cards[uuid][fieldN]
    Ashby       neither          the served HTML has no <form> and no <input>;
                                 the form is rendered client side

Ashby therefore cannot be mapped from static HTML at all, and its adapter is
the one that needs a rendered DOM before it can be written honestly.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from job_agent.automation.fields import Field, FillAction, FillPlan, fillable


@dataclass(frozen=True)
class Applicant:
    """Only what a form asks for. Assembled by the applier from the profile and
    the generated documents; no field here is anything an employer would not
    receive anyway."""
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    current_org: str = ""
    links: Mapping[str, str] = None          # label -> url
    resume_path: str = ""
    cover_letter_path: str = ""
    answers: Mapping[str, str] = None        # question text/key -> answer

    def __post_init__(self):
        object.__setattr__(self, "links", dict(self.links or {}))
        object.__setattr__(self, "answers", dict(self.answers or {}))


# key (name or id, lowercased) -> attribute on Applicant
GREENHOUSE_MAP = {
    "first_name": "first_name", "last_name": "last_name",
    "preferred_name": "first_name", "email": "email", "phone": "phone",
}
LEVER_MAP = {
    "name": "full_name", "email": "email", "phone": "phone",
    "org": "current_org", "location": "location",
}
FILE_FIELDS = {"resume": "resume_path", "cover_letter": "cover_letter_path"}


def _action(field: Field, value: str) -> FillAction | None:
    if not value:
        return None
    kind = {"file": "file", "select-one": "select", "checkbox": "check"}.get(
        field.type, "select" if field.tag == "select" else "text")
    return FillAction(field.selector, kind, value, field.key)


def _plan(fields: Sequence[Field], applicant: Applicant,
          mapping: Mapping[str, str]) -> FillPlan:
    usable = fillable(fields)
    actions: list[FillAction] = []
    filled: set[str] = set()

    for f in usable:
        key = f.key.lower()
        attr = mapping.get(key) or FILE_FIELDS.get(key)
        if attr:
            a = _action(f, getattr(applicant, attr, "") or "")
            if a:
                actions.append(a)
                filled.add(f.key)
            continue
        # Lever exposes links as urls[LinkedIn]; match on the label inside.
        if key.startswith("urls[") and key.endswith("]"):
            label = f.key[5:-1]
            url = next((v for k, v in applicant.links.items()
                        if k.lower() == label.lower()), "")
            a = _action(f, url)
            if a:
                actions.append(a)
                filled.add(f.key)
            continue
        # Anything else is a question. Match on the visible label first, then
        # on the field key, because a custom question's key is a uuid.
        prompt = (f.aria_label or f.key).strip().lower()
        answer = next((v for k, v in applicant.answers.items()
                       if k.strip().lower() in prompt or prompt in k.strip().lower()),
                      "")
        a = _action(f, answer)
        if a:
            actions.append(a)
            filled.add(f.key)

    unmapped = [f.key for f in usable if f.required and f.key not in filled]
    honeypots = sum(1 for f in fields if f.honeypot)
    return FillPlan(actions=tuple(actions), unmapped=tuple(unmapped),
                    skipped_honeypots=honeypots)


def greenhouse(fields, applicant: Applicant) -> FillPlan:
    return _plan(fields, applicant, GREENHOUSE_MAP)


def lever(fields, applicant: Applicant) -> FillPlan:
    return _plan(fields, applicant, LEVER_MAP)


def ashby(fields, applicant: Applicant) -> FillPlan:
    """Ashby renders its form in the browser, so the harness must pass fields
    parsed from the *rendered* DOM. The mapping is Lever-shaped -- Ashby uses
    name attributes once rendered -- but that is unverified until a rendered
    page has been captured, so it is stated as an assumption rather than
    presented as evidence."""
    return _plan(fields, applicant, {**LEVER_MAP, **GREENHOUSE_MAP})


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def plan_for(platform: str, fields, applicant: Applicant) -> FillPlan:
    adapter = ADAPTERS.get(str(platform or "").lower())
    if adapter is None:
        raise ValueError(f"no form adapter for platform {platform!r}")
    return adapter(fields, applicant)
