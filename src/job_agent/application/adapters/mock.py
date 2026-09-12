"""A mock application adapter. No browser, no network, no employer.

Phase 14's deliverable is the engine -- the attempt lifecycle, idempotency and
the crash-recovery rules -- and those need an adapter to exercise but not a
real one. This writes the form it would have submitted to disk as an artifact
and returns a confirmation reference derived from the form hash, so the whole
path can be tested end to end without anything leaving the machine.

It refuses to claim a submission under DRY_RUN. That refusal is redundant --
the engine asserts before calling and a database trigger guards the row -- and
it is here anyway, because a mock that lies about having submitted would make
every test of the layers above it meaningless.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from job_agent.application.adapters.base import (
    Artifact, Prepared, SubmitContext, SubmitResult, form_hash,
)


class MockAdapter:
    name = "mock"

    def __init__(self, artifact_dir: str | Path = "/app/artifacts") -> None:
        self.artifact_dir = Path(artifact_dir)

    def prepare(self, ctx: SubmitContext) -> Prepared:
        fields = {
            "job_title": ctx.job_title,
            "company": ctx.company,
            "job_url": ctx.job_url,
            **{f"answer.{k}": v for k, v in sorted(ctx.answers.items())},
            **{f"document.{d.kind}": d.sha256 for d in ctx.documents},
        }
        fh = form_hash(fields, ctx.documents)

        snapshot = self.artifact_dir / f"app-{ctx.application_id:06d}-form-{fh[:12]}.html"
        body = (
            "<!doctype html><meta charset=utf-8>"
            f"<title>Mock application form {ctx.application_id}</title>"
            f"<h1>{ctx.job_title} — {ctx.company}</h1><dl>"
            + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in sorted(fields.items()))
            + f"</dl><p>form_hash {fh}</p><p>dry_run {ctx.dry_run}</p>"
        )
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(body, encoding="utf-8")

        return Prepared(
            fields=fields, form_hash=fh,
            artifacts=(Artifact(
                kind="HTML_SNAPSHOT", path=str(snapshot),
                sha256=hashlib.sha256(body.encode()).hexdigest(),
                bytes=len(body.encode())),))

    def submit(self, ctx: SubmitContext, prepared: Prepared) -> SubmitResult:
        if ctx.dry_run:
            # The engine already asserted and the database trigger would refuse
            # the row. Saying so here too means a mock can never be the reason a
            # test above it passes.
            return SubmitResult(
                submitted=False,
                error="DRY_RUN: rehearsed the submission, sent nothing")
        return SubmitResult(
            submitted=True,
            confirmation_ref=f"MOCK-{prepared.form_hash[:12].upper()}")
