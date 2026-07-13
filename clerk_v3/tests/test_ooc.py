"""
OOC (Appendix F §4.3.1) golden traps — the out-of-control mechanism.

Two layers under test:
  * clerk.ooc.compute_ooc_windows — entrance (asymmetric 2x/4x), exit, and
    the two flagged edge cases.
  * clerk.grid.build_grid ooc_windows path — wholesale-invalid interior,
    whole-hour QA rule at boundary hours, OOC paragraph, diluent propagation.
"""
from datetime import datetime, timedelta, timezone

import pytest

from clerk.grid import OOC_RULE, build_grid
from clerk.ooc import (FAIL_2X, FAIL_4X, PASS, ValidationCapsule,
                       compute_ooc_windows)
from clerk.schemas import (AnalyzerUnit, CellValid, Event, EventType,
                           OperatingWindow, SiteConfig)

D = datetime(2026, 4, 1, tzinfo=timezone.utc)


def _t(h, m=0):
    return D + timedelta(hours=h, minutes=m)


def _cap(analyzer, start, end, status):
    return ValidationCapsule(analyzer, start, end, status)


def _cfg():
    return SiteConfig(SiteTimeZoneIANA="America/New_York", LookbackMonths=8,
                      LateXThresholdDays=7, JitterToleranceMin=5,
                      PartialOperatingHourApplicability={},
                      ReasonParagraphMap={"MM-01": "(i)", "QA-01": "(iii)"})


# ---------------------------------------------------------------------------
# Window computation — entrance / exit
# ---------------------------------------------------------------------------

def test_five_consecutive_2x_enters_at_the_fifth_priors_valid():
    """Five consecutive 2x -> OOC starts at the 5th check's START. The four
    priors are NOT retroactively invalid (forward from the 5th only)."""
    caps = [_cap("A1", _t(1), _t(1, 15), FAIL_2X),
            _cap("A1", _t(2), _t(2, 15), FAIL_2X),
            _cap("A1", _t(3), _t(3, 15), FAIL_2X),
            _cap("A1", _t(4), _t(4, 15), FAIL_2X),
            _cap("A1", _t(5), _t(5, 15), FAIL_2X),   # the 5th
            _cap("A1", _t(9), _t(9, 15), PASS)]
    windows, flags = compute_ooc_windows(caps)
    assert flags == []
    assert windows == {"A1": [(_t(5), _t(9, 15))]}, \
        "entrance at the 5th capsule's start, exit at the Pass end"
    assert windows["A1"][0][0] == _t(5), "must NOT reach back to before the 5th"


def test_pass_mid_run_resets_the_2x_count():
    """Four 2x, a Pass, then more 2x -> no OOC until a NEW five-run."""
    caps = [_cap("A1", _t(1), _t(1, 15), FAIL_2X),
            _cap("A1", _t(2), _t(2, 15), FAIL_2X),
            _cap("A1", _t(3), _t(3, 15), FAIL_2X),
            _cap("A1", _t(4), _t(4, 15), FAIL_2X),
            _cap("A1", _t(5), _t(5, 15), PASS),       # resets
            _cap("A1", _t(6), _t(6, 15), FAIL_2X),
            _cap("A1", _t(7), _t(7, 15), FAIL_2X),
            _cap("A1", _t(8), _t(8, 15), FAIL_2X),
            _cap("A1", _t(9), _t(9, 15), FAIL_2X)]    # only 4 in the new run
    windows, flags = compute_ooc_windows(caps)
    assert windows == {} and flags == [], "4 + pass + 4 never reaches 5 consecutive"


def test_single_4x_enters_at_preceding_capsule():
    """A single 4x -> OOC starts at the START of the immediately preceding
    capsule (whatever its status). One 4x is enough."""
    caps = [_cap("A1", _t(1), _t(1, 15), PASS),
            _cap("A1", _t(2), _t(2, 15), FAIL_2X),   # the preceding capsule
            _cap("A1", _t(3), _t(3, 15), FAIL_4X),   # the 4x
            _cap("A1", _t(6), _t(6, 15), PASS)]
    windows, flags = compute_ooc_windows(caps)
    assert flags == []
    assert windows == {"A1": [(_t(2), _t(6, 15))]}, \
        "entrance at the preceding capsule's start, exit at the next Pass end"


def test_4x_mid_2x_run_uses_4x_rule():
    """A 4x mid-2x-run -> the 4x rule fires (entrance at the preceding
    capsule), not the 2x counting rule."""
    caps = [_cap("A1", _t(1), _t(1, 15), FAIL_2X),
            _cap("A1", _t(2), _t(2, 15), FAIL_2X),   # preceding the 4x
            _cap("A1", _t(3), _t(3, 15), FAIL_4X),
            _cap("A1", _t(7), _t(7, 15), PASS)]
    windows, flags = compute_ooc_windows(caps)
    assert windows == {"A1": [(_t(2), _t(7, 15))]}


def test_exit_is_the_next_pass_interior_spans_end_to_end():
    """Exit at the next Pass; the interior is one continuous window from
    entrance to that Pass end (fails in between do not fragment it)."""
    caps = [_cap("A1", _t(1), _t(1, 15), PASS),
            _cap("A1", _t(2), _t(2, 15), FAIL_4X),
            _cap("A1", _t(3), _t(3, 15), FAIL_2X),
            _cap("A1", _t(4), _t(4, 15), FAIL_2X),
            _cap("A1", _t(5), _t(5, 15), PASS)]
    windows, _ = compute_ooc_windows(caps)
    assert windows == {"A1": [(_t(1), _t(5, 15))]}, "one continuous window to the Pass end"


# ---------------------------------------------------------------------------
# Flagged edge cases — surfaced, not invented
# ---------------------------------------------------------------------------

def test_4x_on_first_validation_enters_at_record_start():
    """(1) DEFINED (confirmed): a 4x with no preceding capsule enters at the
    BEGINNING OF THE RECORD (this first capsule's start), forward to the
    closing Pass — no flag, no undefined entrance."""
    caps = [_cap("A1", _t(2), _t(2, 15), FAIL_4X),   # first validation is a 4x
            _cap("A1", _t(6), _t(6, 15), PASS)]
    windows, flags = compute_ooc_windows(caps)
    assert windows == {"A1": [(_t(2), _t(6, 15))]}, \
        "entrance at the record's first capsule start, exit at the Pass end"
    assert flags == [], "no longer an unresolved edge case"


def test_open_tail_stays_invalid_until_a_pass_closed_at_window_end():
    """(2) DEFINED (confirmed): a window open at end of stream STAYS open
    (invalid). open_tail_end closes it there so the grid can score it — the
    permanent rule. An informational flag records that no Pass closed it."""
    caps = [_cap("A1", _t(1), _t(1, 15), PASS),
            _cap("A1", _t(2), _t(2, 15), FAIL_4X)]     # opens, never closes
    w_closed, f_closed = compute_ooc_windows(caps, open_tail_end=_t(12))
    assert w_closed == {"A1": [(_t(1), _t(12))]}, \
        "held open (invalid) through the evaluation window end"
    assert any(f.kind == "open-tail-no-closing-pass" for f in f_closed), \
        "informational flag: still open, no corrective Pass yet"


# ---------------------------------------------------------------------------
# Grid — interior wholesale invalid, boundary whole-hour rule, OOC paragraph
# ---------------------------------------------------------------------------

def _grid(ooc, units=None, start=None, end=None):
    units = units or [AnalyzerUnit("A1", "U1", SeeqCovered=True, Obligation="NOx")]
    start = start or _t(0)
    end = end or _t(6)
    operating = [OperatingWindow(u, start, end) for u in {au.Unit for au in units}]
    return build_grid(events=[], capsules=[],
                      operating_windows=operating,
                      analyzer_units=units, qa_windows=[], config=_cfg(),
                      window_start=start, window_end=end, ooc_windows=ooc)


def _verdict(cells, analyzer, hour):
    for c in cells:
        if c.Analyzer == analyzer and c.HourStartUTC == hour:
            return c.Valid, c.RuleApplied
    raise KeyError((analyzer, hour))


def test_interior_hours_are_wholesale_invalid_under_ooc_paragraph():
    """An OOC window 01:00-04:00: hours fully inside are invalid, marked
    under the OOC (QA/QC) paragraph — never the fault paragraph."""
    cells = _grid({"A1": [(_t(1), _t(4))]})
    for h in (1, 2, 3):
        v, rule = _verdict(cells, "A1", _t(h))
        assert v is CellValid.invalid, f"hour {h} interior must be invalid"
        assert rule == OOC_RULE, "OOC paragraph, not fault"
    # outside the window, ordinary valid hours
    assert _verdict(cells, "A1", _t(0))[0] is CellValid.valid
    assert _verdict(cells, "A1", _t(4))[0] is CellValid.valid


def test_boundary_entrance_hour_stays_valid_with_residual_15min():
    """Entrance mid-hour at 01:40 -> the 01:00 hour has 01:00-01:40 (40 min)
    valid outside the OOC span -> two points >=15 min apart -> hour VALID,
    under the OOC paragraph."""
    cells = _grid({"A1": [(_t(1, 40), _t(4))]})
    v, rule = _verdict(cells, "A1", _t(1))
    assert (v, rule) == (CellValid.valid, OOC_RULE), \
        "boundary hour with >=15 min residual valid stays valid"


def test_boundary_entrance_hour_invalid_without_residual_15min():
    """Entrance at 01:50 -> only 01:00-01:50... wait: residual 01:00-01:50 is
    50 min. Use 01:05 span<15: entrance at 01:10 leaves 01:00-01:10 (10 min)
    -> under 15 -> hour INVALID."""
    cells = _grid({"A1": [(_t(1, 10), _t(4))]})
    v, rule = _verdict(cells, "A1", _t(1))
    assert (v, rule) == (CellValid.invalid, OOC_RULE), \
        "boundary hour with <15 min residual valid is invalid"


def test_boundary_exit_hour_valid_with_residual_15min():
    """Exit mid-hour at 03:20 -> the 03:00 hour has 03:20-04:00 (40 min)
    valid outside the OOC span -> hour VALID."""
    cells = _grid({"A1": [(_t(1), _t(3, 20))]})
    v, rule = _verdict(cells, "A1", _t(3))
    assert (v, rule) == (CellValid.valid, OOC_RULE)


def test_boundary_exit_hour_invalid_without_residual_15min():
    """Exit at 03:50 -> only 03:50-04:00 (10 min) valid -> hour INVALID."""
    cells = _grid({"A1": [(_t(1), _t(3, 50))]})
    v, rule = _verdict(cells, "A1", _t(3))
    assert (v, rule) == (CellValid.invalid, OOC_RULE)


def test_ooc_does_not_go_through_quadrant_scorer():
    """A full-hour OOC block would, under the quadrant scorer, be (i)/(iii)
    fault. It must instead be OOC. Confirm the rule label is OOC, proving
    the OOC path (not evaluate_hour) produced it."""
    cells = _grid({"A1": [(_t(1), _t(2))]})
    v, rule = _verdict(cells, "A1", _t(1))
    assert (v, rule) == (CellValid.invalid, OOC_RULE)
    assert rule != "(i)" and not rule.startswith("(iii)")


def test_diluent_ooc_propagates_to_dependents():
    """A diluent (O2) OOC window invalidates its dependents (NOx, CO) for the
    same hours, under the OOC paragraph — one-way (NOx OOC would not hit O2)."""
    units = [
        AnalyzerUnit("O2", "B15", SeeqCovered=True, Obligation="O2",
                     DiluentsRole="diluent", DiluentSpecies="O2"),
        AnalyzerUnit("NOx", "B15", SeeqCovered=True, Obligation="NOx",
                     DiluentsRole="diluent-corrected", DiluentSpecies="O2",
                     DiluentBasis="O2"),
        AnalyzerUnit("CO", "B15", SeeqCovered=True, Obligation="CO",
                     DiluentsRole="diluent-corrected", DiluentSpecies="O2",
                     DiluentBasis="O2"),
    ]
    cells = _grid({"O2": [(_t(1), _t(3))]}, units=units)
    for a in ("O2", "NOx", "CO"):
        for h in (1, 2):
            v, rule = _verdict(cells, a, _t(h))
            assert (v, rule) == (CellValid.invalid, OOC_RULE), \
                f"{a} hour {h} must be OOC-invalid (propagated for dependents)"


def test_ooc_over_non_operating_hour_stays_not_operating():
    """If the unit is offline that hour, OOC does not manufacture a down-hour
    — a not-operating hour is excluded regardless."""
    cells = build_grid(events=[], capsules=[],
                       operating_windows=[OperatingWindow("U1", _t(3), _t(6))],  # offline 0-3
                       analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True, Obligation="NOx")],
                       qa_windows=[], config=_cfg(), window_start=_t(0), window_end=_t(6),
                       ooc_windows={"A1": [(_t(1), _t(2))]})
    v, rule = _verdict(cells, "A1", _t(1))
    assert v is CellValid.not_operating, "OOC over an offline hour stays not-operating"


def test_no_ooc_windows_is_a_no_op():
    """Omitting ooc_windows leaves the grid exactly as before (regression)."""
    base = _grid({})
    assert all(c.RuleApplied != OOC_RULE for c in base)
    assert all(c.Valid is CellValid.valid for c in base), "clean operating hours"


# ---------------------------------------------------------------------------
# #1 (confirmed) — 4x on the first validation invalidates from the record start
# ---------------------------------------------------------------------------

def test_grid_4x_on_first_validation_invalid_from_record_start():
    """A 4x on the very first validation (first capsule 02:00, Pass 05:00)
    -> OOC window [02:00, 05:15]. Grid: hours 02,03,04 wholesale invalid
    under OOC from the record start; hour 05 is the exit boundary."""
    caps = [_cap("A1", _t(2), _t(2, 15), FAIL_4X),
            _cap("A1", _t(5), _t(5, 15), PASS)]
    windows, flags = compute_ooc_windows(caps)
    assert flags == [] and windows == {"A1": [(_t(2), _t(5, 15))]}
    cells = _grid(windows)
    for h in (2, 3, 4):
        v, rule = _verdict(cells, "A1", _t(h))
        assert (v, rule) == (CellValid.invalid, OOC_RULE), \
            f"hour {h} invalid from the record start"
    assert _verdict(cells, "A1", _t(1))[0] is CellValid.valid, "before the record: valid"
    assert _verdict(cells, "A1", _t(5)) == (CellValid.valid, OOC_RULE)


# ---------------------------------------------------------------------------
# #3 (confirmed) — OOC produces a List C record that matches the grid
# ---------------------------------------------------------------------------

def test_list_c_has_ooc_record_matching_the_grid():
    from clerk.listc import OOC_SOURCE, build_list_c
    units = [AnalyzerUnit("A1", "U1", SeeqCovered=True, Obligation="NOx")]
    windows = {"A1": [(_t(1), _t(4))]}
    cells = _grid(windows, units=units)
    records = build_list_c([], [], cells, analyzer_units=units)
    ooc = [r for r in records if r.SourceUsed == OOC_SOURCE]
    assert len(ooc) == 1
    r = ooc[0]
    assert r.DownHours == [_t(1), _t(2), _t(3)]
    assert r.ResolvedWindows == [(_t(1), _t(4))]
    assert r.GoverningParagraphs == [OOC_RULE]
    assert r.ContributingRecords == ["OOC:A1"]
    grid_ooc = {c.HourStartUTC for c in cells
                if c.Valid is CellValid.invalid and c.RuleApplied == OOC_RULE}
    covered = {h for rec in records for h in rec.DownHours}
    assert grid_ooc <= covered, "no OOC-down grid hour missing from List C"


def test_list_c_ooc_boundary_valid_hour_gets_no_record():
    from clerk.listc import OOC_SOURCE, build_list_c
    units = [AnalyzerUnit("A1", "U1", SeeqCovered=True, Obligation="NOx")]
    windows = {"A1": [(_t(1, 40), _t(4))]}
    cells = _grid(windows, units=units)
    records = build_list_c([], [], cells, analyzer_units=units)
    ooc = [r for r in records if r.SourceUsed == OOC_SOURCE]
    assert all(_t(1) not in r.DownHours for r in ooc)
    assert ooc and ooc[0].DownHours == [_t(2), _t(3)]


def test_list_c_ooc_propagates_to_dependent_records():
    from clerk.listc import OOC_SOURCE, build_list_c
    units = [
        AnalyzerUnit("O2", "B15", SeeqCovered=True, Obligation="O2",
                     DiluentsRole="diluent", DiluentSpecies="O2"),
        AnalyzerUnit("NOx", "B15", SeeqCovered=True, Obligation="NOx",
                     DiluentsRole="diluent-corrected", DiluentSpecies="O2",
                     DiluentBasis="O2"),
    ]
    cells = _grid({"O2": [(_t(1), _t(3))]}, units=units)
    records = build_list_c([], [], cells, analyzer_units=units)
    for a in ("O2", "NOx"):
        recs = [r for r in records if r.Analyzer == a and r.SourceUsed == OOC_SOURCE]
        assert recs and recs[0].DownHours == [_t(1), _t(2)]
