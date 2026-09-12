"""What an application adapter has to provide.

Phase 14 ships one adapter, a mock, and no browser. Phase 15 adds Greenhouse,
Lever and Ashby behind this same interface, so the engine never learns which it
is talking to and the DRY_RUN assertion sits in one place rather than in each
adapter.

The split into prepare/submit is the whole protocol. `prepare` may be re-run
freely -- it reads a form and fills boxes -- and returns a hash of what it is
about to send. `submit` is the irreversible half, and the engine writes and
commits a SUBMIT_INTENT row carrying that hash before calling it.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Document:
    kind: str            # RESUME | COVER_LETTER
    path: str
    sha256: str


@dataclass(frozen=True)
class SubmitContext:
    application_id: int
    job_title: str
    company: str
    job_url: str
    documents: Sequence[Document]
    answers: Mapping[str, str]
    dry_run: bool


@dataclass(frozen=True)
class Artifact:
    kind: str            # matches attempt_artifacts.kind
    path: str
    sha256: str | None = None
    bytes: int | None = None


@dataclass(frozen=True)
class Prepared:
    """What is about to be sent, and a hash of it.

    form_hash is what makes a duplicate submission detectable after a crash:
    the same application prepared twice produces the same hash, so a
    SUBMIT_INTENT row found on restart can be compared against a fresh
    preparation to see whether it was the same form.
    """
    fields: Mapping[str, str]
    form_hash: str
    artifacts: Sequence[Artifact] = field(default_factory=tuple)


@dataclass(frozen=True)
class SubmitResult:
    submitted: bool
    confirmation_ref: str | None = None
    artifacts: Sequence[Artifact] = field(default_factory=tuple)
    error: str | None = None


def form_hash(fields: Mapping[str, str], documents: Sequence[Document]) -> str:
    """Stable across runs and across processes: sorted keys, document digests
    rather than paths, and no timestamps. A hash that changed every run would
    make the crash-recovery comparison meaningless."""
    payload = {
        "fields": {k: fields[k] for k in sorted(fields)},
        "documents": sorted((d.kind, d.sha256) for d in documents),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


class Adapter(Protocol):
    name: str

    def prepare(self, ctx: SubmitContext) -> Prepared:
        ...

    def submit(self, ctx: SubmitContext, prepared: Prepared) -> SubmitResult:
        ...
