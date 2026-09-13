"""The Playwright driver is deliberately thin, and these tests check that it
stayed that way rather than exercising a browser.

The reasoning: everything with a decision in it lives in harness.py and
refusals.py and is tested against real captured pages without Chromium. If
judgement leaks down into the driver, it lands in the one file that cannot be
tested on a machine without a browser -- so the shape of this file is itself
the property worth asserting.
"""
import ast
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "src" / "job_agent"
       / "automation" / "playwright_driver.py")
BODY = SRC.read_text(encoding="utf-8")
TREE = ast.parse(BODY)


def methods():
    cls = next(n for n in ast.walk(TREE)
               if isinstance(n, ast.ClassDef) and n.name == "PlaywrightDriver")
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


def test_it_implements_exactly_the_page_driver_protocol():
    from job_agent.automation.harness import PageDriver
    required = {n for n in dir(PageDriver) if not n.startswith("_")}
    assert required <= set(methods()), f"missing {required - set(methods())}"


def test_the_driver_never_clicks_anything():
    """The click belongs to the engine, which writes SUBMIT_INTENT first and
    holds the DRY_RUN guard. A driver that could click is a second place a
    submission could originate."""
    assert ".click(" not in BODY and "def click" not in BODY


def test_the_driver_makes_no_decisions():
    """No refusal logic, no field mapping, no plan. If any of that appears
    here it has escaped the half of the system that is tested.

    Checked against identifiers in the AST, not against the file's text: the
    first version of this test failed on the module docstring, which mentions
    refusals.py by name. Twice now a grep has been mistaken for a property.
    """
    used = {n.id for n in ast.walk(TREE) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(TREE) if isinstance(n, ast.Attribute)}
    used |= {n.name for n in ast.walk(TREE)
             if isinstance(n, (ast.Import, ast.ImportFrom)) for n in n.names}
    banned = {"refusals", "plan_for", "FillPlan", "Applicant", "parse",
              "fillable", "inspect", "challenge_presented"}
    assert not (used & banned), f"{sorted(used & banned)} belongs in the tested half"


def test_the_user_agent_is_not_spoofed():
    """The brief draws the line at defeating bot detection, and claiming to be
    something we are not is where that starts. The suffix is additive."""
    assert "job-agent/0.1" in BODY
    assert "USER_AGENT_SUFFIX" in BODY
    assert "Mozilla/" not in BODY, "no hand-written UA string"


def test_visibility_is_checked_rather_than_presence():
    """The bug that made the first refusal rules reject every real form: the
    invisible captcha variants inject empty containers everywhere."""
    src = ast.get_source_segment(BODY, methods()["visible"])
    assert "is_visible" in src


def test_navigation_waits_for_the_network_to_settle():
    """Ashby serves no <form> at all; the fields only exist after the app
    renders. Reading at domcontentloaded finds an empty page."""
    src = ast.get_source_segment(BODY, methods()["goto"])
    assert "networkidle" in src


def test_a_page_that_never_settles_is_still_read():
    """Plenty of real pages keep a socket open forever. Waiting for idle must
    be best-effort, not a hard requirement."""
    src = ast.get_source_segment(BODY, methods()["goto"])
    assert "except Exception" in src


def test_playwright_is_imported_lazily():
    """Every other service builds from the shared Dockerfile and has no
    playwright. This module must not break their imports by existing."""
    top_level = [n for n in TREE.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [a.name for n in top_level if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in top_level if isinstance(n, ast.ImportFrom)]
    assert not any("playwright" in n for n in names), "import it inside __enter__"


def test_playwright_is_not_in_the_shared_requirements():
    root = Path(__file__).resolve().parents[2]
    shared = (root / "requirements.txt").read_text(encoding="utf-8")
    browser = (root / "requirements-browser.txt").read_text(encoding="utf-8")
    assert "playwright" not in shared
    assert "playwright==" in browser
