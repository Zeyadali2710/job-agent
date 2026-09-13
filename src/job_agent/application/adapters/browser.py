"""The real application adapter: a browser driving an ATS form.

Holds the page open between prepare() and submit(), because that is what the
protocol requires -- the engine commits a SUBMIT_INTENT row in between, and a
form filled in a browser that has since closed cannot be submitted.

The division of labour is the point:

    harness.prepare   opens, refuses, fills, refuses again   (tested, no browser)
    this adapter      lifecycle, artifacts, the click        (thin)
    the engine        SUBMIT_INTENT, DRY_RUN, the decision   (guarded)

Nothing here decides whether to submit. `submit()` is only ever reached after
the engine has written the intent row and passed its own guard, and it refuses
anyway under DRY_RUN -- the third statement of the same fact, in the place
where being wrong would cost the most.
"""
from __future__ import annotations

from collections.abc import Callable

from job_agent.application.adapters.base import (
    Artifact, Prepared, SubmitContext, SubmitResult, form_hash,
)
from job_agent.automation.harness import PageDriver, Refused, applicant_from, prepare

# The submit control, per platform, from the captured forms.
SUBMIT_SELECTOR = {
    "greenhouse": "#application-form button[type=submit]",
    "lever": "#application-form button[type=submit]",
    "ashby": "form button[type=submit]",
}


class BrowserAdapter:
    name = "browser"

    def __init__(self, driver_factory: Callable[[], PageDriver],
                 artifact_dir: str = "/app/artifacts") -> None:
        self._factory = driver_factory
        self._artifact_dir = artifact_dir.rstrip("/")
        self._driver: PageDriver | None = None

    # ------------------------------------------------------------------ prepare

    def prepare(self, ctx: SubmitContext) -> Prepared:
        driver = self._factory()
        self._driver = driver
        shot = f"{self._artifact_dir}/app-{ctx.application_id:06d}-pre-submit.png"
        try:
            form = prepare(
                driver, platform=ctx.platform, url=ctx.job_url,
                applicant=applicant_from(
                    ctx.identity, {d.kind: d.path for d in ctx.documents},
                    dict(ctx.answers)),
                screenshot_path=shot, policy=ctx.automation_policy)
        except Exception:
            # A refusal is still the end of this attempt, so the page goes with
            # it. Leaving a browser open on a page we declined to touch is a
            # resource leak and an invitation to touch it anyway.
            self.close()
            raise

        fields = {a.field_key or a.selector: a.value for a in form.plan.actions}
        artifacts = [Artifact(kind="PRE_SUBMIT_SCREENSHOT", path=shot)]
        return Prepared(fields=fields,
                        form_hash=form_hash(fields, ctx.documents),
                        artifacts=tuple(artifacts))

    # ------------------------------------------------------------------- submit

    def submit(self, ctx: SubmitContext, prepared: Prepared) -> SubmitResult:
        try:
            if ctx.dry_run:
                return SubmitResult(
                    submitted=False,
                    error="DRY_RUN: filled the form and stopped at the button")
            selector = SUBMIT_SELECTOR.get(ctx.platform.lower())
            if selector is None:
                return SubmitResult(
                    submitted=False,
                    error=f"no submit control known for platform {ctx.platform!r}")
            if self._driver is None:
                return SubmitResult(
                    submitted=False, error="the page was closed before submit")
            raise NotImplementedError(
                "the live click is not wired up: it needs one observed "
                "submission against a real form to know what success looks "
                "like, and DRY_RUN has never been off")
        finally:
            self.close()

    # ---------------------------------------------------------------- lifecycle

    def close(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None and hasattr(driver, "close"):
            try:
                driver.close()
            except Exception:
                pass

    def __enter__(self) -> "BrowserAdapter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["BrowserAdapter", "Refused", "SUBMIT_SELECTOR"]
