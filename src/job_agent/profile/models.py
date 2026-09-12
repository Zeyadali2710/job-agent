"""Profile schema. Every atomic fact carries a stable ID."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ID_RE = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$")

WorkRightStatus = Literal[
    "CITIZEN", "PR", "TEMP_GRADUATE_485", "STUDENT_500", "REQUIRES_SPONSORSHIP"
]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ided(Base):
    id: str

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError(f"bad id {v!r}: expected lowercase dotted, e.g. exp.acme.b1")
        return v


class Bullet(Ided):
    text: str = Field(min_length=10)
    tags: list[str] = []
    metrics: dict[str, float | int | str] = {}


class Link(Ided):
    label: str
    url: str


class Identity(Ided):
    full_name: str
    preferred_name: str
    email: str
    phone_display: str | None = None
    location_city: str
    location_state: str
    links: list[Link] = []


class WorkRights(Ided):
    status: WorkRightStatus
    citizenship: str
    clearance_held: str | None = None
    clearance_eligible: bool = False
    requires_sponsorship: bool = False


class Education(Ided):
    institution: str
    qualification: str
    start: date | str
    end: date | str | None = None
    wam: float | None = None
    completed: bool = False
    highlights: list[Bullet] = []


class Experience(Ided):
    organisation: str
    title: str
    location: str | None = None
    start: date | str
    end: date | str | None = None
    bullets: list[Bullet] = []


class Project(Ided):
    name: str
    url: str | None = None
    start: date | str | None = None
    end: date | str | None = None
    bullets: list[Bullet] = []


class Skill(Ided):
    name: str
    level: Literal["BASIC", "WORKING", "ADVANCED", "EXPERT"]
    years: float | None = None
    evidence_ids: list[str] = []


class Preferences(Ided):
    seniority_targets: list[str]
    location_classes_accepted: list[str]
    allow_relocation: bool = False
    employment_types: list[str]
    salary_min: int
    salary_target: int
    salary_is_mandatory: bool = False
    company_blacklist: list[str] = []
    # Architecture 6. Literal, not str: a typo must fail in the profile
    # validator rather than silently select the stricter branch.
    uncertain_policy: Literal["proceed_with_flag", "review"] = "proceed_with_flag"


class StarAnswer(Ided):
    prompt_themes: list[str] = []
    situation: str
    task: str
    action: str
    result: str
    source_ids: list[str] = []


class Sensitive(Base):
    """Loaded into schema `sensitive`. Never referenced by PublicProfile."""
    address: str | None = None
    dob: date | str | None = None
    phone: str | None = None
    tfn: str | None = None
    visa_grant_number: str | None = None
    passport: str | None = None
    referee_contacts: str | None = None
    salary_expectations_floor: int | None = None
    emergency_contact: str | None = None


class Profile(Base):
    identity: Identity
    work_rights: WorkRights
    preferences: Preferences
    education: list[Education] = []
    experience: list[Experience] = []
    projects: list[Project] = []
    skills: list[Skill] = []
    star: list[StarAnswer] = []


class PublicProfile(Base):
    """The ONLY profile type an LLM prompt may be built from.

    Address, dob, phone, tfn, visa numbers, passport, referees and the salary floor
    are not fields on this model, so they cannot be serialised into a prompt.
    This is a type-system guarantee, not a code-review guarantee.
    """
    preferred_name: str
    location_city: str
    location_state: str
    work_right_status: WorkRightStatus
    clearance_held: str | None
    education: list[Education]
    experience: list[Experience]
    projects: list[Project]
    skills: list[Skill]
    star: list[StarAnswer]

    @classmethod
    def project(cls, p: Profile) -> "PublicProfile":
        return cls(
            preferred_name=p.identity.preferred_name,
            location_city=p.identity.location_city,
            location_state=p.identity.location_state,
            work_right_status=p.work_rights.status,
            clearance_held=p.work_rights.clearance_held,
            education=p.education,
            experience=p.experience,
            projects=p.projects,
            skills=p.skills,
            star=p.star,
        )
