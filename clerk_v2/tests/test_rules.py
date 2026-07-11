"""
Rule engine tests — verdicts implemented from docs/14_CFR_60_13_h2_Verbatim.md
(source-verified eCFR text). Branch verdicts unfrozen 2026-07-02.

G4/G9/G10 vectors are RE-DERIVED from the verbatim text below, not carried
from the spec's paraphrase. One spec arithmetic slip found and corrected in
place (see test_g4_second_window_alone — the verdict stands, the stated span
number was wrong).
"""
from datetime import datetime, timedelta, timezone

import pytest

from clerk.rules import HourContext, _select_paragraph, _unit_offline, evaluate_hour, quadrants_operated, reason_to_paragraph
from clerk.schemas import CellValid, SiteConfig

HOUR = datetime(2026, 2, 3, 9, 0, tzinfo=timezone.utc)


def _m(minutes: float) -> datetime:
    return HOUR + timedelta(minutes=minutes)


def _ctx(**kw):
    defaults = dict(
        analyzer="CEMS-001",
        hour_start=HOUR,
        seeq_covered=False,
        operating=[(_m(0), _m(60))],  # full-hour operating
    )
    defaults.update(kw)
    return HourContext(**defaults)


# ---------------------------------------------------------------------------
# D3: unit-offline mask → paragraph selection → verdict fold (W3)
# ---------------------------------------------------------------------------

def test_offline_mask_fires_before_paragraph_selection():
    """Unit-offline mask (step 1) must short-circuit before paragraph selection
    (step 2) even when conditions that would select branch (iv) are present."""
    ctx = _ctx(operating=[], failed_cal_at=_m(5))
    assert _unit_offline(ctx) is True, "mask must see the unit as offline"
    verdict, rule = evaluate_hour(ctx)
    assert verdict is CellValid.not_operating
    assert rule == "not-operating"


def test_select_paragraph_skips_mask_step():
    """_select_paragraph never sees an offline unit — evaluate_hour guarantees
    the mask fires first. Confirm _select_paragraph returns a callable."""
    ctx = _ctx(seeq_covered=True)  # normal operating hour
    branch = _select_paragraph(ctx)
    assert callable(branch)


# ---------------------------------------------------------------------------
# W4: config-driven reason→paragraph mapping
# ---------------------------------------------------------------------------

def _cfg(**reason_map) -> SiteConfig:
    return SiteConfig(
        SiteTimeZoneIANA="America/New_York",
        LookbackMonths=8,
        LateXThresholdDays=7,
        JitterToleranceMin=5,
        PartialOperatingHourApplicability={},
        ReasonParagraphMap=dict(reason_map),
    )


def test_reason_to_paragraph_returns_mapped_value():
    cfg = _cfg(BKD="(iii)(A)", MAINT="(iii)(A)")
    assert reason_to_paragraph("BKD", cfg) == "(iii)(A)"
    assert reason_to_paragraph("MAINT", cfg) == "(iii)(A)"


def test_reason_to_paragraph_returns_none_for_unknown():
    cfg = _cfg(BKD="(iii)(A)")
    assert reason_to_paragraph("UNKNOWN-CODE", cfg) is None
    assert reason_to_paragraph("", cfg) is None


def test_resolved_paragraph_overrides_auto_selection():
    """ctx.resolved_paragraph wins over auto-selection. An uncovered single-
    quadrant hour would normally select (iii)(B); pre-resolving to (iii)(A)
    routes it through the wider separation test instead."""
    ctx = _ctx(
        operating=[(_m(50), _m(58))],   # one quadrant → auto would pick (iii)(B)
        resolved_paragraph="(iii)(A)",
    )
    verdict, rule = evaluate_hour(ctx)
    assert rule == "(iii)(A)", "resolved_paragraph must override the single-quadrant branch"


def test_resolved_paragraph_unknown_label_falls_through():
    """An unrecognised paragraph label in resolved_paragraph is silently
    ignored — auto-selection takes over rather than crashing."""
    ctx = _ctx(seeq_covered=True, resolved_paragraph="(xiv)(Z)")
    verdict, rule = evaluate_hour(ctx)
    assert rule in {"(i)", "(ii)"}  # normal branch auto-selected


# ---------------------------------------------------------------------------
# SeeqCovered gate + NOT-ASSESSED (Ryan, 2026-07-02) — unchanged behavior
# ---------------------------------------------------------------------------

def test_uncovered_normal_hour_is_not_assessed():
    verdict, rule = evaluate_hour(_ctx(seeq_covered=False))
    assert verdict is CellValid.not_assessed
    assert rule == "not-assessed:no-seeq-coverage"


def test_not_operating_hour_needs_no_coverage_and_no_regulation():
    verdict, rule = evaluate_hour(_ctx(seeq_covered=False, operating=[]))
    assert verdict is CellValid.not_operating
    assert rule == "not-operating"


def test_manual_window_fires_iii_a_regardless_of_coverage():
    """Full-hour manual window, uncovered analyzer → (iii)(A) claims the hour
    and its verdict is invalid (V is empty) — never NOT-ASSESSED."""
    verdict, rule = evaluate_hour(
        _ctx(seeq_covered=False, manual_qa_windows=[(_m(0), _m(60))]))
    assert (verdict, rule) == (CellValid.invalid, "(iii)(A)")


# ---------------------------------------------------------------------------
# Branch (i)/(ii) — normal hour (covered analyzers only)
# ---------------------------------------------------------------------------

def test_clean_full_hour_is_valid_under_i():
    verdict, rule = evaluate_hour(_ctx(seeq_covered=True))
    assert (verdict, rule) == (CellValid.valid, "(i)")


def test_detected_window_consuming_a_quadrant_invalidates_under_i():
    """Q1 fully consumed by a detection window → some operated quadrant has
    no valid data point → invalid."""
    verdict, rule = evaluate_hour(
        _ctx(seeq_covered=True, detected_invalid_windows=[(_m(0), _m(20))]))
    assert (verdict, rule) == (CellValid.invalid, "(i)")


def test_partial_hour_valid_when_every_operated_quadrant_has_valid_time():
    """Operates Q2+Q3 only, clean → (ii) valid."""
    verdict, rule = evaluate_hour(
        _ctx(seeq_covered=True, operating=[(_m(20), _m(55))]))
    assert (verdict, rule) == (CellValid.valid, "(ii)")


def test_partial_hour_invalid_when_an_operated_quadrant_is_consumed():
    verdict, rule = evaluate_hour(
        _ctx(seeq_covered=True, operating=[(_m(20), _m(55))],
             detected_invalid_windows=[(_m(45), _m(60))]))
    assert (verdict, rule) == (CellValid.invalid, "(ii)")


# ---------------------------------------------------------------------------
# Branch (iii)(B) — single-quadrant hour
# ---------------------------------------------------------------------------

def test_single_quadrant_with_any_valid_instant_is_valid():
    verdict, rule = evaluate_hour(
        _ctx(operating=[(_m(50), _m(58))]))
    assert (verdict, rule) == (CellValid.valid, "(iii)(B)")


def test_single_quadrant_fully_invalid_is_invalid():
    verdict, rule = evaluate_hour(
        _ctx(operating=[(_m(50), _m(58))],
             detected_invalid_windows=[(_m(50), _m(58))]))
    assert (verdict, rule) == (CellValid.invalid, "(iii)(B)")


# ---------------------------------------------------------------------------
# G4 — (iii)(A) composition across windows + branch selection changes answer
# Vectors re-derived from the verbatim two-point-separation test.
# ---------------------------------------------------------------------------

def test_g4_first_window_alone_valid():
    """Manual :00–:20 alone: V = :20–:60, span 40 ≥ 15 → valid. Matches spec."""
    verdict, rule = evaluate_hour(_ctx(manual_qa_windows=[(_m(0), _m(20))]))
    assert (verdict, rule) == (CellValid.valid, "(iii)(A)")


def test_g4_second_window_alone_valid():
    """Manual :12–:35 alone: V = :00–:12 ∪ :35–:60. VECTOR CORRECTION: the
    spec said "V spans 25 min" — under the verbatim test the separation is
    between points at :00 and :60, i.e. span(V) = 60 min (the spec's 25 was
    the longest contiguous block, which the rule does not ask for). Verdict
    unchanged: valid either way."""
    verdict, rule = evaluate_hour(_ctx(manual_qa_windows=[(_m(12), _m(35))]))
    assert (verdict, rule) == (CellValid.valid, "(iii)(A)")


def test_g4_union_valid_under_iii_a():
    """Both windows: V = :35–:60 → span 25 ≥ 15 → valid. Matches spec."""
    verdict, rule = evaluate_hour(
        _ctx(manual_qa_windows=[(_m(0), _m(20)), (_m(12), _m(35))]))
    assert (verdict, rule) == (CellValid.valid, "(iii)(A)")


def test_g4_same_union_invalid_under_normal_branch():
    """The same invalid union arriving as DETECTION windows (no maintenance
    hour → normal branch): Q1 fully consumed → invalid. Branch selection
    changes the answer and the engine honors it. Matches spec."""
    verdict, rule = evaluate_hour(
        _ctx(seeq_covered=True,
             detected_invalid_windows=[(_m(0), _m(20)), (_m(12), _m(35))]))
    assert (verdict, rule) == (CellValid.invalid, "(i)")


# ---------------------------------------------------------------------------
# G9 — (iii)(A) boundaries. Re-derived: "separated by AT LEAST 15 minutes"
# makes exactly-15 valid; 14 fails. Confirmed as spec stated.
# ---------------------------------------------------------------------------

def test_g9_span_exactly_15_is_valid():
    verdict, rule = evaluate_hour(_ctx(manual_qa_windows=[(_m(0), _m(45))]))
    assert (verdict, rule) == (CellValid.valid, "(iii)(A)")


def test_g9_span_14_is_invalid():
    verdict, rule = evaluate_hour(_ctx(manual_qa_windows=[(_m(0), _m(46))]))
    assert (verdict, rule) == (CellValid.invalid, "(iii)(A)")


def test_g9_fail_case_two_windows_leave_10_min():
    """Invalid :00–:25 and :35–:60 → V = :25–:35 → span 10 → invalid. Confirmed."""
    verdict, rule = evaluate_hour(
        _ctx(manual_qa_windows=[(_m(0), _m(25)), (_m(35), _m(60))]))
    assert (verdict, rule) == (CellValid.invalid, "(iii)(A)")


# ---------------------------------------------------------------------------
# G10 — (iv). Re-derived from "unless a subsequent calibration error test is
# passed in the same hour and the requirements of (h)(2)(iii) are met, based
# solely on valid data recorded after the successful calibration". Confirmed.
# ---------------------------------------------------------------------------

def test_g10_fail_then_pass_with_sufficient_post_cal_data_is_valid():
    """Fail :05, pass :30, valid :30–:60 → (iii)(A) on post-cal V: span 30 → valid."""
    verdict, rule = evaluate_hour(
        _ctx(failed_cal_at=_m(5), passing_cal_at=_m(30)))
    assert (verdict, rule) == (CellValid.valid, "(iv)")


def test_g10_fail_with_no_in_hour_pass_is_invalid_regardless_of_data():
    verdict, rule = evaluate_hour(_ctx(failed_cal_at=_m(5)))
    assert (verdict, rule) == (CellValid.invalid, "(iv)")


def test_g10_pass_too_late_for_15_min_separation_is_invalid():
    """Pass at :50 → post-cal V = :50–:60 → span 10 < 15 → invalid."""
    verdict, rule = evaluate_hour(
        _ctx(failed_cal_at=_m(5), passing_cal_at=_m(50)))
    assert (verdict, rule) == (CellValid.invalid, "(iv)")


def test_g10_pass_must_be_after_the_fail():
    """A passing cal EARLIER in the hour than the fail is not 'subsequent'."""
    verdict, rule = evaluate_hour(
        _ctx(failed_cal_at=_m(30), passing_cal_at=_m(5)))
    assert (verdict, rule) == (CellValid.invalid, "(iv)")


def test_g10_single_quadrant_hour_uses_iii_b_requirement_post_cal():
    """(iv)'s exception applies (h)(2)(iii) — for a one-quadrant hour that is
    (iii)(B): any valid instant after the pass suffices."""
    verdict, rule = evaluate_hour(
        _ctx(operating=[(_m(45), _m(60))],
             failed_cal_at=_m(46), passing_cal_at=_m(50)))
    assert (verdict, rule) == (CellValid.valid, "(iv)")


def test_g10_post_cal_data_also_subtracts_other_invalid_windows():
    """'based solely on VALID data recorded after' — a detection window after
    the passing cal still subtracts: pass :30 but :30–:50 detected-invalid →
    post-cal V = :50–:60 → span 10 → invalid."""
    verdict, rule = evaluate_hour(
        _ctx(failed_cal_at=_m(5), passing_cal_at=_m(30),
             detected_invalid_windows=[(_m(30), _m(50))]))
    assert (verdict, rule) == (CellValid.invalid, "(iv)")


# ---------------------------------------------------------------------------
# W5 — real ≥15-min temporal separation, not quadrant-membership proxy
# ---------------------------------------------------------------------------

def test_w5_falsifying_case_adjacent_quadrants_1min_apart_is_invalid():
    """W5 falsifying case: valid data only from :14:30 to :15:30.
    Points exist in two different quadrants (Q0 and Q1) — a quadrant-
    membership proxy would accept this as satisfying (iii)(A). The real
    ≥15-min temporal separation test (span = 1 min) correctly rejects it.
    This is the boundary case D5 requires the implementation to get right."""
    verdict, rule = evaluate_hour(
        _ctx(manual_qa_windows=[(_m(0), _m(14.5)), (_m(15.5), _m(60))]))
    assert (verdict, rule) == (CellValid.invalid, "(iii)(A)")


def test_w5_exactly_15min_separation_is_valid():
    """The boundary from the other side: span exactly 15 min → valid.
    'separated by AT LEAST 15 minutes' makes the boundary inclusive."""
    verdict, rule = evaluate_hour(_ctx(manual_qa_windows=[(_m(0), _m(45))]))
    assert (verdict, rule) == (CellValid.valid, "(iii)(A)")


def test_w5_14min_separation_is_invalid():
    """14-min span falls just short of the ≥15-min threshold → invalid."""
    verdict, rule = evaluate_hour(_ctx(manual_qa_windows=[(_m(0), _m(46))]))
    assert (verdict, rule) == (CellValid.invalid, "(iii)(A)")


# ---------------------------------------------------------------------------
# The (iii)(A) literal-vs-interval-algebra boundary (docs/14 equivalence note)
# ---------------------------------------------------------------------------

def test_iii_a_dense_sampling_assumption_boundary():
    """Where the interval-algebra form could diverge from the literal test:
    V = two 30-second slivers at the hour's extremes. span(V) = 59.5 min →
    the implemented form says VALID. The literal test needs actual data
    points inside those slivers — with a sampling interval wider than 30 s
    no such points may exist. This test PINS the implemented (dense-sampling)
    behavior as the documented, deliberate choice per docs/14; if sampling
    cadence ever becomes an input, revisit this vector."""
    verdict, rule = evaluate_hour(
        _ctx(manual_qa_windows=[(_m(0.5), _m(59.5))]))
    assert (verdict, rule) == (CellValid.valid, "(iii)(A)")


# ---------------------------------------------------------------------------
# Quadrant arithmetic (pure interval math)
# ---------------------------------------------------------------------------

def test_quadrants_full_hour():
    assert quadrants_operated(_ctx()) == {0, 1, 2, 3}


def test_quadrants_single():
    ctx = _ctx(operating=[(_m(16), _m(29))])
    assert quadrants_operated(ctx) == {1}


def test_quadrants_boundary_touch_is_not_occupancy():
    ctx = _ctx(operating=[(_m(0), _m(15))])
    assert quadrants_operated(ctx) == {0}
