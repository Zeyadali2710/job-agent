"""Form adapters, tested against real captured ATS pages.

Every assertion here is about a page that was actually fetched. The fixtures
carry their provenance in PROVENANCE.json, and nothing in the adapters was
written from memory of what these forms look like.
"""
import json
from pathlib import Path

import pytest

from job_agent.automation.adapters.ats import Applicant, plan_for
from job_agent.automation.fields import Field, fillable, parse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats"

APPLICANT = Applicant(
    first_name="Zeyad", last_name="Ali", full_name="Zeyad Ali",
    email="z@example.test", phone="0400000000", location="Sydney, NSW",
    current_org="ArmsOA",
    links={"GitHub": "https://github.com/x", "LinkedIn": "https://li/x"},
    resume_path="/app/artifacts/r.pdf", cover_letter_path="/app/artifacts/c.pdf",
    answers={"Website": "https://zeyad.dev"})


def load(name):
    return parse((FIXTURES / f"{name}.html").read_text(encoding="utf-8"))


def plan(name):
    platform = "ashby" if name.startswith("ashby") else name
    return plan_for(platform, load(name), APPLICANT)


def selectors(p):
    return {a.selector for a in p.actions}


# ------------------------------------------------------------- provenance

def test_every_fixture_records_where_it_came_from():
    """A fixture without provenance is indistinguishable from one I invented."""
    prov = json.loads((FIXTURES / "PROVENANCE.json").read_text(encoding="utf-8"))
    for name in ("greenhouse", "lever", "ashby"):
        assert prov[name]["url"].startswith("https://")
        assert prov[name]["bytes_original"] > prov[name]["bytes_fixture"]


# ------------------------------------------------------------- greenhouse

def test_greenhouse_honeypots_are_found_and_never_filled():
    """Two real inputs: required, aria-hidden, tabindex=-1, no label. An
    adapter that filled every required field would fill them and announce
    itself as a bot on every application."""
    p = plan("greenhouse")
    assert p.skipped_honeypots == 2
    honeypots = [f for f in load("greenhouse") if f.honeypot]
    assert honeypots and all(f not in fillable(load("greenhouse")) for f in honeypots)


def test_greenhouse_maps_the_fields_it_addresses_by_id():
    s = selectors(plan("greenhouse"))
    assert {"#first_name", "#last_name", "#email", "#phone"} <= s


def test_greenhouse_uploads_both_documents():
    p = plan("greenhouse")
    files = {a.selector: a.value for a in p.actions if a.kind == "file"}
    assert files == {"#resume": "/app/artifacts/r.pdf",
                     "#cover_letter": "/app/artifacts/c.pdf"}


def test_a_custom_question_is_matched_on_its_visible_label():
    """Greenhouse names custom questions question_<id>, which tells you
    nothing. The aria-label is the only thing that says what is being asked."""
    p = plan("greenhouse")
    assert any(a.value == "https://zeyad.dev" for a in p.actions)


# ------------------------------------------------------------------ lever

def test_lever_prefers_the_data_qa_hook_over_anything_else():
    """data-qa is a deliberate test hook and survives a redesign that renames
    every class."""
    s = selectors(plan("lever"))
    assert '[data-qa="name-input"]' in s and '[data-qa="email-input"]' in s


def test_lever_hidden_state_fields_are_left_alone():
    """accountId, origin, referer, resumeStorageId and friends carry the
    site's own state. Writing to them is meaningless at best."""
    keys = {f.key for f in fillable(load("lever"))}
    for hidden in ("accountId", "origin", "referer", "resumeStorageId",
                   "socialSource", "linkedInData"):
        assert hidden not in keys


def test_lever_links_are_matched_by_their_label():
    actions = {a.selector: a.value for a in plan("lever").actions}
    assert actions['input[name="urls[GitHub]"]'] == "https://github.com/x"
    assert actions['input[name="urls[LinkedIn]"]'] == "https://li/x"


def test_a_link_the_applicant_does_not_have_is_not_invented():
    s = selectors(plan("lever"))
    assert 'input[name="urls[Twitter]"]' not in s


# ------------------------------------------------------------------ ashby

def test_ashby_serves_no_form_at_all():
    """The whole reason a browser is required rather than an HTTP client."""
    assert load("ashby") == []


def test_an_empty_plan_is_never_complete():
    """Ashby's served HTML yields no fields. An emptiness-blind check called
    that a finished plan, which is one step from submitting a blank
    application."""
    assert plan("ashby").complete is False


# ------------------------------------------------------------------ shared

@pytest.mark.parametrize("name", ["greenhouse", "lever"])
def test_a_real_form_produces_a_complete_plan(name):
    assert plan(name).complete is True


def test_an_unfillable_required_field_blocks_the_plan():
    fields = [Field(tag="input", name="need_this", required=True)]
    p = plan_for("lever", fields, APPLICANT)
    assert p.unmapped == ("need_this",) and p.complete is False


def test_an_unknown_platform_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        plan_for("workday", [], APPLICANT)


# ------------------------------------------------- ashby, from a rendered DOM

def test_ashby_maps_its_system_fields_once_rendered():
    """The served HTML has nothing; these exist only after the app runs.
    Captured with Playwright at networkidle -- see the fixture header."""
    actions = {a.selector: a.value for a in plan("ashby_rendered").actions}
    assert actions["#_systemfield_name"] == "Zeyad Ali"
    assert actions["#_systemfield_email"] == "z@example.test"
    assert actions["#_systemfield_resume"] == "/app/artifacts/r.pdf"


def test_a_uuid_named_field_is_resolved_through_its_visible_label():
    """Ashby's phone field is named a1b76c17-a1e9-40d5-... and labelled
    "Phone". The name says nothing; the label is the whole signal."""
    actions = {a.field_key: a.value for a in plan("ashby_rendered").actions}
    phone = [v for k, v in actions.items() if v == "0400000000"]
    assert phone, actions


def test_the_cover_letter_upload_does_not_receive_the_applicants_name():
    """"Cover letter" contains "name"? No -- but "full name" and "name" both
    match loosely, so labels are matched longest-first. Without that, the
    cover-letter file input gets a person's name typed into it."""
    p = plan("ashby_rendered")
    files = {a.selector: a.value for a in p.actions if a.kind == "file"}
    assert all(v.endswith(".pdf") for v in files.values()), files


def test_the_captcha_token_field_is_never_written_to():
    """Ashby exposes <textarea name="g-recaptcha-response"> and Lever exposes
    <input name="h-captcha-response">. Both look like somewhere to put an
    answer. Writing there is forging a captcha result."""
    for name in ("ashby_rendered", "lever"):
        keys = {f.key for f in fillable(load(name))}
        assert not any("captcha" in k.lower() for k in keys), name
        assert not any("captcha" in (a.field_key or "").lower()
                       for a in plan(name).actions), name


def test_a_work_rights_radio_group_is_never_answered_automatically():
    """The real form offers six options including "Graduate visa (e.g 485)".
    Choosing one is a judgement about what the employer is asking, and it is
    not a small thing to get wrong."""
    p = plan("ashby_rendered")
    assert not any(a.kind in ("check", "select") for a in p.actions)
    assert any("Australian citizen" in c for c in p.choices_left)


def test_a_radio_group_is_reported_as_one_question_not_one_per_option():
    p = plan("ashby_rendered")
    visa = [c for c in p.choices_left if "Australian citizen" in c]
    assert len(visa) == 1 and "+5 more options" in visa[0]


def test_an_optional_choice_left_blank_is_still_surfaced():
    """"Optional" is the form's opinion. Submitting with work rights blank is
    an omission, not a tidy default, so it is reported even though it does not
    block the plan."""
    p = plan("ashby_rendered")
    assert p.complete is True and p.choices_left
