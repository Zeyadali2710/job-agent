"""Map an applicant onto a real ATS form.

Every selector here came from a page that was actually fetched; the evidence is
in tests/fixtures/ats with its provenance, and the tests run against it. Nothing
in this module was written from memory of what these forms look like.

The three platforms address their fields three different ways, which is why the
architecture has three adapters rather than one:

    Greenhouse  by id            #first_name #last_name #email #phone
                                 #resume #cover_letter (both type=file)
                                 plus two honeypots that must stay empty
    Lever       by name          name email phone org location
                                 resume (file), urls[LinkedIn] ...
                                 custom questions as cards[uuid][fieldN]
    Ashby       _systemfield_    rendered only -- the served HTML has no <form>
                + uuids          and no <input> at all, and even rendered there
                                 is still no <form>. Standard fields are
                                 _systemfield_name / _email / _resume; every
                                 other field is a uuid whose meaning lives in
                                 its <label for=...>.

A choice field -- radio, checkbox, select -- is never answered automatically.
The rendered Ashby form in tests/fixtures is the reason: it asks work rights as
a radio group whose options include "Graduate visa (e.g 485)", and getting that
wrong on an application is not a small mistake. A required choice field makes
the plan incomplete, and a person answers it.
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


# field key (name or id, lowercased) -> attribute on Applicant
GREENHOUSE_MAP = {
    "first_name": "first_name", "last_name": "last_name",
    "preferred_name": "first_name", "email": "email", "phone": "phone",
}
LEVER_MAP = {
    "name": "full_name", "email": "email", "phone": "phone",
    "org": "current_org", "location": "location",
}
ASHBY_MAP = {
    "_systemfield_name": "full_name", "_systemfield_email": "email",
    "_systemfield_resume": "resume_path",
}
FILE_FIELDS = {"resume": "resume_path", "cover_letter": "cover_letter_path",
               "_systemfield_resume": "resume_path"}

# When a field key is a uuid the visible label is the only thing that says what
# is being asked. Ashby's phone field is a uuid labelled "Phone".
LABEL_MAP = {
    "full name": "full_name", "name": "full_name", "email": "email",
    "phone": "phone", "mobile": "phone", "location": "location",
    "cover letter": "cover_letter_path", "resume": "resume_path",
    "cv": "resume_path",
}


def _action(field: Field, value: str) -> FillAction | None:
    if not value:
        return None
    kind = {"file": "file", "select-one": "select", "checkbox": "check"}.get(
        field.type, "select" if field.tag == "select" else "text")
    return FillAction(field.selector, kind, value, field.key)


def _by_label(field: Field, applicant: Applicant) -> str:
    """Resolve a uuid-named field through its visible label.

    Longest label first, so "cover letter" beats "name" -- otherwise Ashby's
    "Cover letter" upload matches on "name" and receives the applicant's name
    where a file belongs.
    """
    prompt = field.prompt.lower()
    if not prompt:
        return ""
    for label in sorted(LABEL_MAP, key=len, reverse=True):
        if label in prompt:
            return getattr(applicant, LABEL_MAP[label], "") or ""
    for name, url in applicant.links.items():
        if name.lower() in prompt:
            return url
    return ""


def _plan(fields: Sequence[Field], applicant: Applicant,
          mapping: Mapping[str, str]) -> FillPlan:
    usable = fillable(fields)
    actions: list[FillAction] = []
    filled: set[str] = set()
    choice_groups: dict[str, list[str]] = {}
    optional_groups: dict[str, list[str]] = {}

    for f in usable:
        # A radio, checkbox or select is a question with prescribed answers.
        # Choosing one is a judgement about what the employer is asking, and
        # the work-rights group on the real Ashby form -- "Graduate visa
        # (e.g 485)" among six options -- is exactly the kind that must not be
        # guessed. Required ones block the plan; optional ones are left alone.
        if f.is_choice:
            # Radios in a group share a name and differ only by option label,
            # so the group is one question and its options are its answers.
            # Keyed by name, the six-option work-rights group on the real Ashby
            # form reads as one unanswered question rather than six.
            group = f.name or f.id
            bucket = choice_groups if f.required else optional_groups
            bucket.setdefault(group, []).append(f.prompt or f.key)
            continue

        key = f.key.lower()
        attr = mapping.get(key) or FILE_FIELDS.get(key)
        if attr:
            a = _action(f, getattr(applicant, attr, "") or "")
            if a:
                actions.append(a)
                filled.add(f.key)
            continue

        # Lever exposes links as urls[LinkedIn]; the label is inside the name.
        if key.startswith("urls[") and key.endswith("]"):
            label = f.key[5:-1]
            url = next((v for k, v in applicant.links.items()
                        if k.lower() == label.lower()), "")
            a = _action(f, url)
            if a:
                actions.append(a)
                filled.add(f.key)
            continue

        # A uuid-named standard field -- Ashby's phone is one -- resolves
        # through its label rather than its name.
        value = _by_label(f, applicant)
        if value:
            a = _action(f, value)
            if a:
                actions.append(a)
                filled.add(f.key)
                continue

        # Anything else is a question. Match on the visible label, because a
        # custom question's key is a uuid and says nothing at all.
        prompt = (f.prompt or f.key).strip().lower()
        answer = next((v for k, v in applicant.answers.items()
                       if k.strip().lower() in prompt or prompt in k.strip().lower()),
                      "")
        a = _action(f, answer)
        if a:
            actions.append(a)
            filled.add(f.key)

    def describe(options: list[str]) -> str:
        real = [o for o in options if o]
        if len(real) > 1:
            return f"{real[0]} (+{len(real) - 1} more options)"
        return real[0] if real else "an unlabelled choice"

    unmapped = [f.key for f in usable
                if f.required and not f.is_choice and f.key not in filled]
    unmapped += [describe(v) for v in choice_groups.values()]
    honeypots = sum(1 for f in fields if f.honeypot)
    return FillPlan(actions=tuple(actions), unmapped=tuple(unmapped),
                    skipped_honeypots=honeypots,
                    choices_left=tuple(describe(v) for v in optional_groups.values()))


def greenhouse(fields, applicant: Applicant) -> FillPlan:
    return _plan(fields, applicant, GREENHOUSE_MAP)


def lever(fields, applicant: Applicant) -> FillPlan:
    return _plan(fields, applicant, LEVER_MAP)


def ashby(fields, applicant: Applicant) -> FillPlan:
    """Needs fields parsed from the RENDERED DOM: the served HTML has no form
    and no inputs at all."""
    return _plan(fields, applicant, ASHBY_MAP)


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def plan_for(platform: str, fields, applicant: Applicant) -> FillPlan:
    adapter = ADAPTERS.get(str(platform or "").lower())
    if adapter is None:
        raise ValueError(f"no form adapter for platform {platform!r}")
    return adapter(fields, applicant)
