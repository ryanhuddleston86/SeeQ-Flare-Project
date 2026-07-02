"""
Rule engine skeleton tests — routing + the SeeqCovered gate ONLY.

Branch VERDICTS are stubbed pending verbatim 40 CFR 60.13(h)(2) review
(fetch currently blocked by network policy). These tests prove the routing
and the coverage gate without implementing any regulatory logic: a stub
raising NotImplementedError with the branch name IS the routing assertion.
"""
from datetime import datetime, timedelta, timezone

import pytest

from clerk.rules import HourContext, evaluate_hour, quadrants_operated
from clerk.schemas import CellValid

HOUR = datetime(2026, 2, 3, 9, 0, tzinfo=timezone.utc)


def _ctx(**kw):
    defaults = dict(
        analyzer="CEMS-001",
        hour_start=HOUR,
        seeq_covered=False,
        operating=[(HOUR, HOUR + timedelta(hours=1))],  # full-hour operating
    )
    defaults.update(kw)
    return HourContext(**defaults)


# ---------------------------------------------------------------------------
# The new coverage behavior (Ryan, 2026-07-02)
# ---------------------------------------------------------------------------

def test_uncovered_normal_hour_is_not_assessed():
    """SeeqCovered=false, unit operating, no manual/QA window → NOT-ASSESSED."""
    verdict, rule = evaluate_hour(_ctx(seeq_covered=False))
    assert verdict is CellValid.not_assessed
    assert rule == "not-assessed:no-seeq-coverage"


def test_covered_normal_hour_routes_to_branch_5():
    """Same hour with coverage → branch (i)/(ii); its verdict is stubbed
    pending verbatim review, and the stub firing proves the routing."""
    with pytest.raises(NotImplementedError, match=r"\(i\)/\(ii\)"):
        evaluate_hour(_ctx(seeq_covered=True))


def test_manual_window_fires_branch_4_regardless_of_coverage():
    """SeeqCovered=false + a manual entry covering the hour → branch (iii)(A)
    claims it; coverage is irrelevant to branches 2-4. The stub firing (rather
    than NOT-ASSESSED being returned) proves the gate does not shadow them."""
    ctx = _ctx(seeq_covered=False,
               manual_qa_windows=[(HOUR, HOUR + timedelta(hours=1))])
    with pytest.raises(NotImplementedError, match=r"\(iii\)\(A\)"):
        evaluate_hour(ctx)


def test_failed_cal_fires_branch_2_regardless_of_coverage():
    ctx = _ctx(seeq_covered=False, failed_cal_at=HOUR + timedelta(minutes=5))
    with pytest.raises(NotImplementedError, match=r"\(iv\)"):
        evaluate_hour(ctx)


def test_single_quadrant_fires_branch_3_regardless_of_coverage():
    ctx = _ctx(seeq_covered=False,
               operating=[(HOUR + timedelta(minutes=50),
                           HOUR + timedelta(minutes=58))])
    with pytest.raises(NotImplementedError, match=r"\(iii\)\(B\)"):
        evaluate_hour(ctx)


def test_not_operating_hour_needs_no_coverage_and_no_regulation():
    verdict, rule = evaluate_hour(_ctx(seeq_covered=False, operating=[]))
    assert verdict is CellValid.not_operating
    assert rule == "not-operating"


@pytest.mark.skip(reason="branch (iii)(A) verdict awaits verbatim 40 CFR "
                         "60.13(h)(2) text + Ryan's vector review (eCFR fetch "
                         "blocked by network policy)")
def test_manual_window_hour_verdict_under_iii_a():
    """Vector held for the review package: invalid :00-:20 in an otherwise
    clean operating hour → V spans :20-:60 = 40 min ≥ 15 → valid, coverage
    irrelevant. DO NOT enable until the pasted text confirms the vector."""
    ctx = _ctx(seeq_covered=False,
               manual_qa_windows=[(HOUR, HOUR + timedelta(minutes=20))])
    verdict, rule = evaluate_hour(ctx)
    assert verdict is CellValid.valid
    assert "iii" in rule


# ---------------------------------------------------------------------------
# Quadrant arithmetic (not regulatory — pure interval math)
# ---------------------------------------------------------------------------

def test_quadrants_full_hour():
    assert quadrants_operated(_ctx()) == {0, 1, 2, 3}


def test_quadrants_single():
    ctx = _ctx(operating=[(HOUR + timedelta(minutes=16),
                           HOUR + timedelta(minutes=29))])
    assert quadrants_operated(ctx) == {1}


def test_quadrants_boundary_touch_is_not_occupancy():
    """An interval ending exactly at a quadrant boundary does not occupy the
    next quadrant (zero-width overlap)."""
    ctx = _ctx(operating=[(HOUR, HOUR + timedelta(minutes=15))])
    assert quadrants_operated(ctx) == {0}
