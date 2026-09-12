"""The deterministic scorer. Pure function, no DB, no model."""
from datetime import datetime, timedelta, timezone

import pytest

from job_agent.pipeline.score import (
    BAND_HIGH, BAND_MEDIUM, DEFAULT_WEIGHTS, UNKNOWN,
    final_score, score, why_and_concerns,
)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)

PREFS = {
    "seniority_targets": ["GRADUATE", "JUNIOR", "INTERN"],
    "location_classes_accepted": ["SYDNEY_CBD", "SYDNEY_METRO", "GREATER_SYDNEY",
                                  "HYBRID_SYDNEY", "REMOTE_AU"],
    "employment_types": ["FULL_TIME", "GRAD_PROGRAM", "INTERNSHIP"],
    "salary_target": 90000,
}

PERFECT = {
    "seniority_class": "GRADUATE",
    "location_class": "SYDNEY_CBD",
    "salary": {"total_package_min": 95000},
    "employment_type": "FULL_TIME",
    "posted_at": NOW,
    "is_grad_program": True,
    "work_rights_class": "COMPATIBLE",
    # Four, not two: the denominator floor means a two-skill ad can no longer
    # be a perfect skill match, and this fixture is meant to be one.
    "job_skills": ["python", "sql", "linux", "docker"],
    "profile_skills": ["python", "sql", "linux", "docker", "go"],
}


def s(**over):
    return score({**PERFECT, **over}, PREFS, now=NOW)


def feat(features, name):
    return next(f for f in features if f["feature"] == name)


# ------------------------------------------------------------------- shape

def test_a_perfect_match_scores_at_the_top():
    base, band, _ = s()
    assert base == 100.0 and band == "HIGH"


def test_score_never_leaves_the_range_the_check_constraint_allows():
    """job_scores.base_score has CHECK (base_score BETWEEN 0 AND 100). The
    bonus can push a strong job past 100 and the penalty can push a weak one
    below 0, so both ends must clamp before they reach the database."""
    high, _, _ = s()
    low, _, _ = score({"seniority_class": "MID", "location_class": "REQUIRES_RELOCATION",
                       "salary": {}, "employment_type": "CONTRACT", "posted_at": None,
                       "is_grad_program": False, "work_rights_class": "UNCERTAIN",
                       "job_skills": ["rust"], "profile_skills": []}, PREFS, now=NOW)
    assert 0.0 <= low <= high <= 100.0


def test_every_feature_produces_a_row_with_evidence():
    _, _, features = s()
    assert {f["feature"] for f in features} == set(DEFAULT_WEIGHTS)
    assert all(f["evidence"] for f in features), "every row must explain itself"
    assert all(f["contribution"] == pytest.approx(f["weight"] * f["value"], abs=0.01)
               for f in features)


def test_bands_follow_the_notification_threshold():
    assert BAND_HIGH == 80, "HIGH must line up with HIGH_MATCH_JOB at score >= 80"
    assert BAND_MEDIUM < BAND_HIGH


# ------------------------------------------------- silence is neutral, not bad

@pytest.mark.parametrize("field,override", [
    ("seniority_match", {"seniority_class": "UNKNOWN"}),
    ("salary_vs_target", {"salary": {}}),
    ("employment_type_match", {"employment_type": None}),
    ("freshness", {"posted_at": None}),
    ("skill_overlap", {"job_skills": []}),
    ("location_rank", {"location_class": "UNKNOWN"}),
])
def test_missing_data_scores_neutral_not_zero(field, override):
    """A silent ad is not a worse job. Scoring silence as zero would bury every
    posting that simply omits a field -- the same mistake as holding UNCERTAIN
    work rights in review."""
    _, _, features = s(**override)
    assert feat(features, field)["value"] == UNKNOWN


def test_the_scorer_is_useful_before_enrichment_exists():
    """skill_overlap needs job_analyses, which needs the LLM. Until then it is
    neutral and the rest of the score still works."""
    base, band, _ = s(job_skills=[])
    assert base > BAND_MEDIUM


# ---------------------------------------------------------------- individual

def test_location_preference_order_is_respected():
    first, _, _ = s(location_class="SYDNEY_CBD")
    last, _, _ = s(location_class="REMOTE_AU")
    rejected, _, _ = s(location_class="OTHER_CAPITAL")
    assert first > last > rejected


def test_salary_above_target_does_not_score_above_full_marks():
    _, _, features = s(salary={"total_package_min": 500000})
    assert feat(features, "salary_vs_target")["value"] == 1.0


def test_freshness_decays_and_floors_at_zero():
    fresh = feat(s(posted_at=NOW)[2], "freshness")["value"]
    mid = feat(s(posted_at=NOW - timedelta(days=15))[2], "freshness")["value"]
    ancient = feat(s(posted_at=NOW - timedelta(days=400))[2], "freshness")["value"]
    assert fresh == 1.0 and 0 < mid < 1 and ancient == 0.0


def test_uncertain_work_rights_is_a_penalty_not_a_block():
    """Architecture 6 moved this out of the hard screen. It should cost a few
    points and appear as a concern -- not remove the job."""
    clean, _, _ = s()
    uncertain, _, features = s(work_rights_class="UNCERTAIN")
    assert uncertain < clean
    assert feat(features, "work_rights_uncertain")["contribution"] < 0


def test_skill_overlap_caps_the_denominator():
    """A job listing twenty skills should not require twenty matches."""
    many = [f"skill{i}" for i in range(20)]
    _, _, features = s(job_skills=many, profile_skills=many[:8])
    assert feat(features, "skill_overlap")["value"] == 1.0


def test_skill_overlap_floors_the_denominator():
    """Production: "Talent Engineer" matched 1 of 1 extracted skills and scored
    a perfect 1.00, outranking a security role that matched 5 of 19. An ad
    naming one technology has not described a stack."""
    _, _, features = s(job_skills=["python"], profile_skills=["python", "sql"])
    assert feat(features, "skill_overlap")["value"] == 0.25
    # the real match it was beating
    nineteen = [f"skill{i}" for i in range(19)]
    _, _, deep = s(job_skills=nineteen, profile_skills=nineteen[:5])
    assert feat(deep, "skill_overlap")["value"] >         feat(features, "skill_overlap")["value"]


def test_empty_extraction_is_not_neutral():
    """An analysis that ran and named no technologies is information -- the ad
    is not a technology role. Only a missing analysis is neutral."""
    _, _, ran = s(job_skills=[], analysed=True)
    assert feat(ran, "skill_overlap")["value"] == 0.0
    _, _, absent = s(job_skills=[])
    assert feat(absent, "skill_overlap")["value"] == UNKNOWN


# ------------------------------------------------------- llm adjustment bound

@pytest.mark.parametrize("adj,expected", [(0, 50.0), (10, 60.0), (-10, 40.0),
                                          (999, 60.0), (-999, 40.0)])
def test_llm_adjustment_is_bounded_at_ten(adj, expected):
    """job_scores.llm_adjustment has CHECK (BETWEEN -10 AND 10). A prompt
    injection must not be able to turn a 40 into a 90."""
    assert final_score(50.0, adj) == expected


def test_final_score_stays_inside_the_range():
    assert final_score(96.0, 10) == 100.0
    assert final_score(4.0, -10) == 0.0


# ------------------------------------------------------------------ the card

def test_why_and_concerns_split_by_contribution():
    _, _, features = s(work_rights_class="UNCERTAIN", location_class="UNKNOWN")
    why, concerns = why_and_concerns(features)
    assert why and concerns
    assert "the ad says nothing about work rights" in concerns
    assert all(isinstance(x, str) for x in why + concerns)


def test_why_is_ordered_by_how_much_it_actually_moved_the_score():
    _, _, features = s()
    why, _ = why_and_concerns(features)
    contributions = [feat(features, f["feature"])["contribution"]
                     for f in features if f["evidence"] in why]
    assert contributions == sorted(contributions, reverse=True)


def test_positive_weights_sum_to_exactly_one_hundred():
    """No feature combination may exceed 100 before clamping. If it can, the
    clamp flattens the top of the range and penalties below it stop mattering
    -- which is exactly where discrimination matters most."""
    positives = sum(w for w in DEFAULT_WEIGHTS.values() if w > 0)
    assert positives == 100, f"positive weights sum to {positives}"


def test_a_penalty_is_still_visible_on_the_best_possible_job():
    """The regression that rebalancing fixed: at 100 the clamp used to absorb
    the work-rights penalty entirely."""
    best, _, _ = s()
    with_concern, _, _ = s(work_rights_class="UNCERTAIN")
    assert best == 100.0
    assert with_concern == 95.0


# ------------------------------------------------------- persistence helpers

def test_effective_version_changes_when_weights_change():
    """job_scores has UNIQUE (job_group_id, scorer_version) and ja_worker has
    no DELETE on score_features, so a score cannot be rewritten in place. The
    weights live in app.settings to be tuned without a deploy -- folding them
    into the version is what makes a tweak re-score instead of leaving every
    existing score stale and unreachable."""
    from job_agent.pipeline.score import SCORER_VERSION, effective_version
    base = effective_version(DEFAULT_WEIGHTS)
    assert base.startswith(f"{SCORER_VERSION}+")
    assert effective_version({**DEFAULT_WEIGHTS, "freshness": 9}) != base
    assert effective_version(dict(DEFAULT_WEIGHTS)) == base, "must be stable"


def test_effective_version_changes_when_the_profile_changes():
    """skill_overlap reads the profile's skills, so loading a new profile
    invalidates every score. Without the profile sha in the version, a
    re-score after `make profile-load` reports scored: 0 and leaves the
    corpus ranked against the previous profile."""
    from job_agent.pipeline.score import effective_version
    a = effective_version(DEFAULT_WEIGHTS, "a" * 64)
    b = effective_version(DEFAULT_WEIGHTS, "b" * 64)
    assert a != b, "a new profile must force a re-score"
    assert effective_version(DEFAULT_WEIGHTS, "a" * 64) == a, "must be stable"
    assert effective_version(DEFAULT_WEIGHTS) != a, "no profile is its own input"


def test_effective_version_ignores_dict_ordering():
    from job_agent.pipeline.score import effective_version
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert effective_version(a) == effective_version(b)


@pytest.mark.parametrize("raw,expected", [
    (["Python", "SQL"], ["Python", "SQL"]),
    ([{"name": "Python"}, {"skill": "SQL"}], ["Python", "SQL"]),
    ([{"nope": "x"}], []),
    ([], []),
])
def test_skill_names_accepts_both_prompt_shapes(raw, expected):
    """job_analyses.skills is model output. If a prompt revision switches
    between plain strings and objects, skill_overlap must not silently drop to
    zero for every job."""
    from job_agent.pipeline.score import _skill_names
    assert _skill_names(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (["a"], ["a"]), ('["a"]', ["a"]), (None, []), ("", []), ("junk", []), ('{"a":1}', []),
])
def test_as_list_guards_the_jsonb_driver_difference(raw, expected):
    from job_agent.pipeline.score import _as_list
    assert _as_list(raw) == expected
