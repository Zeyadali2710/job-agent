"""A PageDriver backed by Playwright. Runs only in the applier's image.

Deliberately the thinnest thing that satisfies the protocol. Every decision --
refuse or proceed, which selector gets which value, whether a plan may go
forward -- lives in `harness.py` and `refusals.py`, which are tested without a
browser. What is left here is the part with no judgement in it, so that the
part that cannot easily be tested is also the part with nothing to get wrong.

Nothing in this file clicks a submit button. The harness returns a prepared
form, the engine writes SUBMIT_INTENT and holds the DRY_RUN guard, and the
click -- when there ever is one -- belongs there.
"""
from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType

DEFAULT_TIMEOUT_MS = 20_000
# A real browser, identified as itself. Not a spoofed string: the brief draws
# the line at defeating bot detection, and lying about what we are is the
# first step of that.
USER_AGENT_SUFFIX = " job-agent/0.1 (personal job-search assistant)"


class PlaywrightDriver:
    """Opens one browser for the life of the object. Use as a context manager.

    `headless` is a constructor argument rather than a setting because a
    headed run is a debugging session someone is watching, not a mode the
    scheduler should be able to pick.
    """

    def __init__(self, *, headless: bool = True,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self._headless = headless
        self._timeout = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---------------------------------------------------------------- lifecycle

    def __enter__(self) -> "PlaywrightDriver":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            # --no-sandbox because the container already drops every capability
            # and runs unprivileged; Chromium's own sandbox needs user
            # namespaces it has no way to obtain here.
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ua = (self._browser.new_context().new_page()
              .evaluate("() => navigator.userAgent"))
        self._context = self._browser.new_context(
            user_agent=ua + USER_AGENT_SUFFIX,
            viewport={"width": 1280, "height": 1600},
            accept_downloads=False)
        self._context.set_default_timeout(self._timeout)
        self._page = self._context.new_page()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = self._page = None

    # ------------------------------------------------------------- the protocol

    def goto(self, url: str) -> int:
        response = self._page.goto(url, wait_until="domcontentloaded")
        # networkidle rather than load: an ATS form is a React app, and the
        # fields do not exist at load. Ashby serves no <form> at all, so
        # waiting for the network to settle is the difference between finding
        # a form and finding an empty page.
        try:
            self._page.wait_for_load_state("networkidle", timeout=self._timeout)
        except Exception:
            pass          # a page that never goes idle is still worth reading
        return response.status if response is not None else 0

    def content(self) -> str:
        return self._page.content()

    def fill(self, selector: str, value: str) -> None:
        self._page.fill(selector, value)

    def set_files(self, selector: str, path: str) -> None:
        self._page.set_input_files(selector, path)

    def visible(self, selectors: Sequence[str]) -> list[str]:
        """Present AND visible. Presence alone is not enough: the invisible
        captcha variants inject empty containers on every page, which is the
        bug that made the first version of the refusal rules reject every real
        application form."""
        out: list[str] = []
        for sel in selectors or ():
            try:
                if self._page.locator(sel).first.is_visible(timeout=1_000):
                    out.append(sel)
            except Exception:
                continue      # a selector that does not resolve is not visible
        return out

    def screenshot(self, path: str) -> None:
        self._page.screenshot(path=path, full_page=True)
