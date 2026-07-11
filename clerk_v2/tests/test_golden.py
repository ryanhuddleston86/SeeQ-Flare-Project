"""
F8 — Golden validation set (Doc 50 stand-in).

# FLAG: Doc 50 ("Clerk v2 Golden Traps — Answer Key") was NOT FOUND in the
# repository (checked the working tree and every remote branch/commit).
# The scenarios below encode every trap whose expected output is
# hand-specified in task #49's fix descriptions, plus traps derived
# directly from the verbatim 40 CFR 60.13(h)(2) text quoted in
# clerk/rules.py (fetched from eCFR 2026-07-01). Expected values here are
# therefore reg-derived, NOT copied from Doc 50 — when Doc 50 lands, each
# GT below must be reconciled against it and any missing scenarios added.
# Per the fix-pass rules, where clerk output disagrees with a hand-derived
# expected value, the CLERK is presumed wrong: fix code, don't bend the
# expected value.

These tests are deliberately independent of the module unit tests: they
assert what the REG says, end to end, not what the code assumed. All data
is synthetic.
"""
from datetime import datetime, timedelta, timezone

import pytest

from clerk.grid import build_grid, source_down_hours
from clerk.rules import HourContext, NotAssessedHourError, evaluate_hour
from clerk.schemas import (
    AnalyzerUnit,
    Capsule,
    CellValid,
    Event,
    EventType,
    GridCell,
    OperatingWindow,
    SiteConfig,
    ValidationEvent,
)

H = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def _m(minutes):
    return H + timedelta(minutes=minutes)


def _cfg():
    return SiteConfig(
        SiteTimeZoneIANA="America/New_York", LookbackMonths=8,
        LateXThresholdDays=7, JitterToleranceMin=5,
        PartialOperatingHourApplicability={}, ReasonParagraphMap={},
    )


def _ctx(**kw):
    defaults = dict(analyzer="G1", hour_start=H, seeq_covered=True,
                    operating=[(_m(0), _m(60))])
    defaults.update(kw)
    return HourContext(**defaults)


def _cell(analyzer, hour, valid):
    return GridCell(Analyzer=analyzer, HourStartUTC=hour, HourLocalLabel="",
                    OperatingFraction=1.0, Valid=valid, RuleApplied="(i)",
                    ContributingEventIDs=[])


# ---------------------------------------------------------------------------
# GT-1..GT-3 — (iii)(A): "two valid data points, separated by at least
# 15 minutes" (verbatim). Canonical form: max(V) − min(V) >= 15 min.
# ---------------------------------------------------------------------------

def test_gt1_span_exactly_15_min_is_valid():
    """Expected (reg, 'at least'): 15-min separation exactly -> VALID."""
    verdict, rule = evaluate_hour(_ctx(manual_qa_windows=[(_m(0), _m(45))]))
    assert (verdict, rule) == (CellValid.valid, "(iii)(A)")


def test_gt2_span_14_min_is_invalid():
    """Expected: 14 min < 15 -> INVALID."""
    verdict, rule = evaluate_hour(_ctx(manual_qa_windows=[(_m(0), _m(46))]))
    assert (verdict, rule) == (CellValid.invalid, "(iii)(A)")


def test_gt3_span_17_min_is_valid():
    """Expected: 17 min >= 15 -> VALID. Discriminator: a 30-minute-span
    implementation fails exactly this case."""
    verdict, rule = evaluate_hour(
        _ctx(manual_qa_windows=[(_m(0), _m(20)), (_m(37), _m(60))]))
    assert (verdict, rule) == (CellValid.valid, "(iii)(A)")


# ---------------------------------------------------------------------------
# GT-4..GT-6 — source rollup: intersection of IN-COVERAGE monitors
# ---------------------------------------------------------------------------

def _rollup_cells(temp_in_service=None):
    units = [
        AnalyzerUnit("PERM", "FCC", SeeqCovered=True),
        AnalyzerUnit("TEMP", "FCC", SeeqCovered=True,
                     InServiceDateUTC=temp_in_service),
    ]
    cells = []
    for h in range(10, 16):
        cells.append(_cell("PERM", _utc(2026, 4, 1, h),
                           CellValid.invalid if h in (10, 11, 12, 13) else CellValid.valid))
        cells.append(_cell("TEMP", _utc(2026, 4, 1, h),
                           CellValid.invalid if h in (12, 13, 14, 15) else CellValid.valid))
    return units, cells


def test_gt4_fcc_full_coverage_source_down_is_the_intersection():
    """Permanent down 10:00–14:00, temp down 12:00–16:00, both in coverage
    all window. Expected: source down = {12:00, 13:00} ONLY."""
    units, cells = _rollup_cells()
    assert source_down_hours(units, cells) == \
        {"FCC": [_utc(2026, 4, 1, 12), _utc(2026, 4, 1, 13)]}


def test_gt5_coverage_gating_lone_permanent_hours_count():
    """Same outages, temp InServiceDate = 12:00. Expected: at 10:00–11:00
    only the permanent is in coverage and it is down -> source down =
    {10:00, 11:00, 12:00, 13:00}."""
    units, cells = _rollup_cells(temp_in_service=_utc(2026, 4, 1, 12))
    assert source_down_hours(units, cells) == \
        {"FCC": [_utc(2026, 4, 1, h) for h in (10, 11, 12, 13)]}


def test_gt6_manual_only_temp_participates_in_rollup():
    """Temp's downtime originates from a manual (List A) TechEntry with no
    capsule anywhere. Expected: temp still participates -> source-down."""
    hour, hour_end = H, H + timedelta(hours=1)
    events = [Event(
        EventID="GT6", EventType=EventType.TechEntry, TargetEventID=None,
        ExtentStartUTC=hour, ExtentEndUTC=hour_end, AnalyzerCEMIDs=["TEMP"],
        Category="", ReasonCode="", Actor="tech", ActedAt=hour, Reason="",
        CorrectiveAction="", DetectionClass="",
    )]
    units = [AnalyzerUnit("PERM", "FCC", SeeqCovered=True),
             AnalyzerUnit("TEMP", "FCC", SeeqCovered=True)]
    cells = build_grid(
        events=events,
        capsules=[Capsule("PERM", "status-offline", hour, hour_end)],
        operating_windows=[OperatingWindow("FCC", hour, hour_end)],
        analyzer_units=units, qa_windows=[], config=_cfg(),
        window_start=hour, window_end=hour_end,
    )
    assert source_down_hours(units, cells) == {"FCC": [hour]}


# ---------------------------------------------------------------------------
# GT-7 — backdate to the last passing VALIDATION EVENT
# ---------------------------------------------------------------------------

def test_gt7_failed_validation_backdates_to_prior_passing_validation():
    """Validation passes Day1 06:00; data reads valid through Day2 06:00;
    validation FAILS Day2 06:00. Expected: invalidate back to Day1 06:00,
    not ~Day2 05:00."""
    day1, day2 = _utc(2026, 1, 15, 6), _utc(2026, 1, 16, 6)
    start, end = _utc(2026, 1, 15, 4), _utc(2026, 1, 16, 8)
    cells = build_grid(
        events=[], capsules=[],
        operating_windows=[OperatingWindow("U1", start, end)],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_cfg(), window_start=start, window_end=end,
        validations=[ValidationEvent("A1", day1, True),
                     ValidationEvent("A1", day2, False)],
    )
    by_hour = {c.HourStartUTC: c.Valid for c in cells}
    assert by_hour[_utc(2026, 1, 15, 5)] is CellValid.valid, "pre-anchor untouched"
    hour = day1
    while hour <= day2:
        assert by_hour[hour] is CellValid.invalid, f"{hour} inside the backdate"
        hour += timedelta(hours=1)
    assert by_hour[_utc(2026, 1, 16, 7)] is CellValid.valid, "post-failure clean"


# ---------------------------------------------------------------------------
# GT-8 — every hour assessed: zero not_assessed, dead end fails loud
# ---------------------------------------------------------------------------

def test_gt8_no_golden_scenario_emits_not_assessed():
    """Expected: every golden build in this module yields zero not_assessed
    cells (build_grid raises NotAssessedHourError before ever emitting one)."""
    hour, hour_end = H, H + timedelta(hours=1)
    cells = build_grid(
        events=[], capsules=[],
        operating_windows=[OperatingWindow("U1", hour, hour_end)],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_cfg(), window_start=hour, window_end=hour_end,
    )
    assert sum(1 for c in cells if c.Valid is CellValid.not_assessed) == 0
    with pytest.raises(NotAssessedHourError):
        evaluate_hour(_ctx(seeq_covered=False))


# ---------------------------------------------------------------------------
# GT-9 — verbatim (i): four valid data points, one per quadrant
# ---------------------------------------------------------------------------

def test_gt9_full_hour_missing_one_quadrant_is_invalid():
    """'at least four valid data points ... one data point in each of the
    15-minute quadrants'. Expected: Q2 (:15–:30) fully consumed -> INVALID
    even though 45 minutes of the hour are valid."""
    verdict, rule = evaluate_hour(
        _ctx(detected_invalid_windows=[(_m(15), _m(30))]))
    assert (verdict, rule) == (CellValid.invalid, "(i)")


# ---------------------------------------------------------------------------
# GT-10 — verbatim (iv): failed daily cal, in-hour recovery
# ---------------------------------------------------------------------------

def test_gt10_iv_recovery_needs_subsequent_pass_and_iii_on_post_cal_data():
    """Fail :05 + pass :30 + clean data after -> post-cal V spans 30 min,
    (iii) met -> VALID. Fail :05 + pass :50 -> post-cal V spans 10 min ->
    INVALID. Fail with no in-hour pass -> INVALID regardless of data."""
    valid_case = evaluate_hour(_ctx(failed_cal_at=_m(5), passing_cal_at=_m(30)))
    assert valid_case == (CellValid.valid, "(iv)")
    late_pass = evaluate_hour(_ctx(failed_cal_at=_m(5), passing_cal_at=_m(50)))
    assert late_pass == (CellValid.invalid, "(iv)")
    no_pass = evaluate_hour(_ctx(failed_cal_at=_m(5)))
    assert no_pass == (CellValid.invalid, "(iv)")
