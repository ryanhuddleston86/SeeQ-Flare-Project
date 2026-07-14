"""
Golden traps for the five-part Appendix-F extension:
  1. unit-offline -> not-assessed + operating-time denominator
  2. partial-operating per-quadrant validity + MQAQC cap
  3. OOC + unit-offline overlap (the "converter" scenario)
  4. run-window vs reporting-period
  5. §60.7(d) DAR roll-up
"""
from datetime import datetime, timedelta, timezone

import pytest

from clerk.dar import dar_rollup
from clerk.grid import (OOC_RULE, build_grid, detection_capsules,
                        operating_time_denominator, unit_offline_windows_from_capsules)
from clerk.ooc import (FAIL_2X, FAIL_4X, PASS, ValidationCapsule,
                       compute_ooc_windows)
from clerk.schemas import (AnalyzerUnit, Capsule, CellValid, GridCell,
                           OperatingWindow, SiteConfig)

D = datetime(2026, 4, 1, tzinfo=timezone.utc)


def _t(h, m=0):
    return D + timedelta(hours=h, minutes=m)


def _cfg():
    return SiteConfig(SiteTimeZoneIANA="America/New_York", LookbackMonths=8,
                      LateXThresholdDays=7, JitterToleranceMin=5,
                      PartialOperatingHourApplicability={},
                      ReasonParagraphMap={"MM-01": "(i)", "QA-01": "(iii)"})


def _units(analyzer="NOx", unit="U1", **kw):
    return [AnalyzerUnit(analyzer, unit, SeeqCovered=True, Obligation="NOx", **kw)]


def _grid(units, start, end, **kw):
    op = [OperatingWindow(u, start, end) for u in {a.Unit for a in units}]
    return build_grid(events=[], capsules=[], operating_windows=op,
                      analyzer_units=units, qa_windows=[], config=_cfg(),
                      window_start=start, window_end=end, **kw)


def _v(cells, analyzer, hour):
    for c in cells:
        if c.Analyzer == analyzer and c.HourStartUTC == hour:
            return c.Valid, c.RuleApplied
    raise KeyError((analyzer, hour))


# ---------------------------------------------------------------------------
# Item 1 — unit-offline: full-hour-offline = not-assessed vs partial = assessed
# ---------------------------------------------------------------------------

def test_full_offline_hour_not_assessed_partial_hour_assessed():
    """An hour the unit is offline the ENTIRE clock hour -> not-operating
    (excluded from the denominator). An hour with even one operating minute
    -> assessed and counts."""
    units = _units()
    # offline 01:00-02:00 (full hour) and 03:00-03:50 (partial: 03:50-04:00 operates)
    offline = {"U1": [(_t(1), _t(2)), (_t(3), _t(3, 50))]}
    cells = _grid(units, _t(0), _t(5), unit_offline_windows=offline)
    assert _v(cells, "NOx", _t(1))[0] is CellValid.not_operating, "full-hour offline"
    # hour 03 operated 03:50-04:00 (10 min, 1 quadrant) -> assessed, scored
    assert _v(cells, "NOx", _t(3))[0] is not CellValid.not_operating, \
        "one operating minute makes the hour assessed"
    denom = operating_time_denominator(cells)
    # 5 calendar hours (00..04); hour 01 is fully offline -> denominator 4
    assert denom["NOx"] == 4, "denominator = calendar hours minus full not-assessed hours"


# ---------------------------------------------------------------------------
# Item 2 — partial-operating per-quadrant validity + MQAQC cap
# ---------------------------------------------------------------------------

def test_partial_operating_needs_a_point_in_each_operated_quadrant():
    """Operated 3 quadrants (00:00-00:45), no MQAQC: needs a valid point in
    all 3. A detected invalid window killing one quadrant -> INVALID (ii)."""
    units = _units()
    # operate only 00:00-00:45 (Q0,Q1,Q2 = 3 quadrants); offline the rest
    offline = {"U1": [(_t(0, 45), _t(1))]}
    # kill Q1 (00:15-00:30) with a detection capsule
    caps = [Capsule("NOx", "status-offline", _t(0, 15), _t(0, 30))]
    op = [OperatingWindow("U1", _t(0), _t(1))]
    cells = build_grid(events=[], capsules=caps, operating_windows=op,
                       analyzer_units=units, qa_windows=[], config=_cfg(),
                       window_start=_t(0), window_end=_t(1),
                       unit_offline_windows=offline)
    v, rule = _v(cells, "NOx", _t(0))
    assert (v, rule) == (CellValid.invalid, "(ii)"), "missing a point in an operated quadrant"


def test_partial_operating_mqaqc_caps_requirement_at_two():
    """Operated 3 quadrants, but a validation ran (MQAQC): the requirement is
    capped at 2. Points in 2 of the 3 operated quadrants -> VALID even though
    the third has none."""
    units = _units()
    op = [OperatingWindow("U1", _t(0), _t(1))]
    offline = {"U1": [(_t(0, 45), _t(1))]}         # operate Q0,Q1,Q2
    caps = [Capsule("NOx", "status-offline", _t(0, 30), _t(0, 45))]  # kill Q2 only
    mqaqc = {"NOx": [(_t(0, 5), _t(0, 20))]}       # a validation ran in the hour
    cells = build_grid(events=[], capsules=caps, operating_windows=op,
                       analyzer_units=units, qa_windows=[], config=_cfg(),
                       window_start=_t(0), window_end=_t(1),
                       unit_offline_windows=offline, mqaqc_windows=mqaqc)
    v, rule = _v(cells, "NOx", _t(0))
    # operated 3 quads, MQAQC cap=2, valid points in Q0 and Q1 -> 2 >= 2 -> valid
    assert (v, rule) == (CellValid.valid, "(ii)"), "MQAQC caps the requirement at 2"


# ---------------------------------------------------------------------------
# Passing daily validations must NOT create downtime (regression trap)
# ---------------------------------------------------------------------------

def test_passing_daily_validations_produce_zero_downtime():
    """Regression: a run of N days of PASSING daily validations (each a
    ~20-min cal-gas check) and NO other events must produce ZERO downtime for
    every analyzer — DAR downtime% == 0. A passing validation is normal QA
    activity (§60.13(h)(2)(iii)), not a monitor outage. Guards against the
    bug where the cal-gas window killed a quadrant of the full hour and the
    (i) rule scored the hour DOWN.

    Two analyzers so the "every analyzer" clause is exercised."""
    units = [AnalyzerUnit("NOx", "U1", SeeqCovered=True, Obligation="NOx"),
             AnalyzerUnit("SO2", "U1", SeeqCovered=True, Obligation="SO2")]
    days = 21                       # a full 3-week run, the real-data horizon
    start, end = _t(0), _t(24 * days)

    # One passing validation per analyzer per day at 06:40-07:00 (cal gas).
    val = []
    for a in ("NOx", "SO2"):
        for d in range(days):
            base = 24 * d
            val.append(ValidationCapsule(a, _t(base + 6, 40), _t(base + 7), PASS))
    ooc, flags = compute_ooc_windows(val, open_tail_end=end)
    assert ooc == {}, "passing validations create no OOC window"
    assert flags == []

    mqaqc = {}
    for v in val:
        mqaqc.setdefault(v.Analyzer, []).append((v.StartUTC, v.EndUTC))

    # The cal-gas offline ALSO surfaced as a Seeq status-offline detection —
    # the exact real-run shape that previously scored the hour DOWN under (i).
    caps = [Capsule(v.Analyzer, "status-offline", v.StartUTC, v.EndUTC) for v in val]

    cells = build_grid(events=[], capsules=caps,
                       operating_windows=[OperatingWindow("U1", start, end)],
                       analyzer_units=units, qa_windows=[], config=_cfg(),
                       window_start=start, window_end=end,
                       ooc_windows=ooc, mqaqc_windows=mqaqc)

    # Not one invalid hour anywhere in the grid.
    down = [c for c in cells if c.Valid is CellValid.invalid]
    assert down == [], f"passing validations produced {len(down)} phantom down-hours"

    # Each validation hour is judged as QA (iii)(A), never (i).
    for a in ("NOx", "SO2"):
        for d in range(days):
            v, rule = _v(cells, a, _t(24 * d + 6))
            assert (v, rule) == (CellValid.valid, "(iii)(A)"), \
                f"{a} day {d}: passing-validation hour must be valid via (iii)(A)"

    # DAR downtime% == 0 for EVERY analyzer.
    rows = dar_rollup(cells, [], units, start, end)
    assert {r.Analyzer for r in rows} == {"NOx", "SO2"}
    for r in rows:
        assert r.DowntimeHours == 0, f"{r.Analyzer}: {r.DowntimeHours} down hours"
        assert r.DowntimePct == 0.0, f"{r.Analyzer}: downtime {r.DowntimePct}%"
        assert r.Flag5pctDowntime is False


# ---------------------------------------------------------------------------
# Item 3 (+1,+2) — the "converter" scenario
# ---------------------------------------------------------------------------

def test_converter_scenario_ooc_offline_repair_without_validation():
    """OOC 4x entrance at the prior validation -> unit taken offline mid-OOC
    -> repair WITHOUT validation (does not close OOC) -> return + a passing
    validation closes OOC at its completion. Verify each stage."""
    units = _units()
    start, end = _t(0), _t(12)

    # Validation stream: prior 2x @02:00, 4x @03:00 (entrance = 02:00),
    # passing validation 09:00-09:45 (closes OOC at 09:45, completion mid-hour).
    val = [ValidationCapsule("NOx", _t(2), _t(2, 15), FAIL_2X),
           ValidationCapsule("NOx", _t(3), _t(3, 15), FAIL_4X),
           ValidationCapsule("NOx", _t(9), _t(9, 45), PASS)]
    ooc, flags = compute_ooc_windows(val, open_tail_end=end)
    assert ooc == {"NOx": [(_t(2), _t(9, 45))]}, "OOC opens at the prior validation, closes at the pass"

    # Unit offline: 04:00-06:30 (hrs 04,05 full; hr06 partial 2-quadrant),
    # and 07:00-07:45 (hr07 partial 1-quadrant). Repair leaves no validation.
    offline = {"U1": [(_t(4), _t(6, 30)), (_t(7), _t(7, 45))]}
    mqaqc = {"NOx": [(_t(2), _t(2, 15)), (_t(3), _t(3, 15)), (_t(9), _t(9, 45))]}

    cells = build_grid(events=[], capsules=[], operating_windows=[OperatingWindow("U1", start, end)],
                       analyzer_units=units, qa_windows=[], config=_cfg(),
                       window_start=start, window_end=end,
                       ooc_windows=ooc, unit_offline_windows=offline, mqaqc_windows=mqaqc)

    # Before OOC: valid.
    assert _v(cells, "NOx", _t(0))[0] is CellValid.valid
    assert _v(cells, "NOx", _t(1))[0] is CellValid.valid
    # OOC interior (operating, fully inside): down under OOC.
    assert _v(cells, "NOx", _t(2)) == (CellValid.invalid, OOC_RULE)
    assert _v(cells, "NOx", _t(3)) == (CellValid.invalid, OOC_RULE)
    # NOT-ASSESSED middle: unit fully offline -> not-operating (masks OOC).
    assert _v(cells, "NOx", _t(4))[0] is CellValid.not_operating
    assert _v(cells, "NOx", _t(5))[0] is CellValid.not_operating
    # Assessed 2-quadrant boundary hour (operated 06:30-07:00), under OOC -> down.
    assert _v(cells, "NOx", _t(6)) == (CellValid.invalid, OOC_RULE)
    # Assessed 1-quadrant boundary hour (operated 07:45-08:00), under OOC -> down.
    assert _v(cells, "NOx", _t(7)) == (CellValid.invalid, OOC_RULE)
    # Still OOC (repair without validation did NOT close it).
    assert _v(cells, "NOx", _t(8)) == (CellValid.invalid, OOC_RULE)
    # MQAQC-cap CLOSING hour: OOC ends 09:45, residual 09:45-10:00 is one
    # quadrant; MQAQC cap needs 2 -> still DOWN.
    assert _v(cells, "NOx", _t(9)) == (CellValid.invalid, OOC_RULE)
    # Validity resumes at the pass completion -> next full hour valid.
    assert _v(cells, "NOx", _t(10))[0] is CellValid.valid
    assert _v(cells, "NOx", _t(11))[0] is CellValid.valid


# ---------------------------------------------------------------------------
# OOC 3-consecutive-2x (no OOC) vs 5-consecutive-2x (OOC)
# ---------------------------------------------------------------------------

def test_three_consecutive_2x_no_ooc_vs_five_consecutive_2x_ooc():
    three = [ValidationCapsule("A1", _t(h), _t(h, 15), FAIL_2X) for h in (1, 2, 3)]
    w3, f3 = compute_ooc_windows(three)
    assert w3 == {} and f3 == [], "3 consecutive 2x -> no OOC"

    five = [ValidationCapsule("A1", _t(h), _t(h, 15), FAIL_2X) for h in (1, 2, 3, 4, 5)]
    five.append(ValidationCapsule("A1", _t(9), _t(9, 15), PASS))
    w5, f5 = compute_ooc_windows(five)
    assert w5 == {"A1": [(_t(5), _t(9, 15))]}, "5th consecutive 2x -> OOC from the 5th"


# ---------------------------------------------------------------------------
# Item 5 — DAR roll-up where one analyzer crosses 5% and others don't
# ---------------------------------------------------------------------------

def _cell(analyzer, hour, valid, rule="(i)"):
    return GridCell(Analyzer=analyzer, HourStartUTC=hour, HourLocalLabel="",
                    OperatingFraction=1.0, Valid=valid, RuleApplied=rule,
                    ContributingEventIDs=[])


def test_dar_rollup_one_analyzer_crosses_5pct():
    """Two analyzers over a 20-hour reporting period. HIGH has 2 down hours
    (10% >= 5% -> full report). LOW has 0 down (0% -> summary only)."""
    units = [AnalyzerUnit("HIGH", "U1", SeeqCovered=True, Obligation="NOx"),
             AnalyzerUnit("LOW", "U1", SeeqCovered=True, Obligation="SO2")]
    cells = []
    for h in range(20):
        cells.append(_cell("HIGH", _t(h), CellValid.invalid if h in (5, 6) else CellValid.valid,
                           rule="(i)"))
        cells.append(_cell("LOW", _t(h), CellValid.valid))
    rows = {r.Analyzer: r for r in dar_rollup(cells, [], units, _t(0), _t(20))}
    assert rows["HIGH"].OperatingHours == 20 and rows["HIGH"].DowntimeHours == 2
    assert rows["HIGH"].DowntimePct == 10.0
    assert rows["HIGH"].Flag5pctDowntime is True
    assert rows["HIGH"].ReportRequired == "full excess-emission report"
    assert rows["LOW"].DowntimeHours == 0 and rows["LOW"].Flag5pctDowntime is False
    assert rows["LOW"].ReportRequired == "summary only"


def test_dar_downtime_breakdown_by_reason_and_reporting_period_filter():
    """Down hours attribute to reason categories via List C records, and the
    roll-up filters to the reporting period (Item 4)."""
    from clerk.listc import ListCRecord
    units = [AnalyzerUnit("NOx", "U1", SeeqCovered=True, Obligation="NOx")]
    # 10-hour fold window; report only hours 2..8.
    cells = [_cell("NOx", _t(h), CellValid.invalid if h in (1, 3, 4) else CellValid.valid)
             for h in range(10)]
    records = [
        ListCRecord("NOx", "auto-approved", [(_t(3), _t(5))], 120.0, "A only", "",
                    [], [], "MM-01", "", "", "auto", "single-source", None,
                    ["(i)"], [_t(3), _t(4)], []),
    ]
    rows = {r.Analyzer: r for r in dar_rollup(cells, records, units, _t(2), _t(9))}
    r = rows["NOx"]
    # hour 1 down is OUTSIDE the reporting period; only hours 3,4 count.
    assert r.DowntimeHours == 2, "reporting-period filter excludes the hour-1 down"
    assert r.DowntimeByReason["MM"] == 2, "both attributed to MM-01"
    assert r.OperatingHours == 7  # hours 2..8 inclusive


def test_dar_excess_data_not_fabricated_when_absent():
    """Excess-emission duration is 0 and flagged not-provided when no excess
    data is supplied — never inferred from validity."""
    units = [AnalyzerUnit("NOx", "U1", SeeqCovered=True, Obligation="NOx")]
    cells = [_cell("NOx", _t(h), CellValid.valid) for h in range(10)]
    r = dar_rollup(cells, [], units, _t(0), _t(10))[0]
    assert r.ExcessHours == 0 and r.ExcessDataProvided is False


# ---------------------------------------------------------------------------
# Unit-offline capsule identity resolution (production 'Unit - UNIT' form)
# ---------------------------------------------------------------------------

def test_unit_offline_identity_forms_resolve_to_unit():
    from clerk.grid import (resolve_offline_unit, unresolved_offline_ids,
                            unit_offline_windows_from_capsules)
    roster = [AnalyzerUnit("Boiler_15 - NOx", "Boiler_15", SeeqCovered=True, Obligation="NOx"),
              AnalyzerUnit("SRU - O2", "SRU", SeeqCovered=True, Obligation="O2")]
    # all three accepted identity forms
    assert resolve_offline_unit("Boiler_15 - UNIT", roster) == "Boiler_15"   # <Unit> - suffix
    assert resolve_offline_unit("Boiler_15", roster) == "Boiler_15"          # bare unit
    assert resolve_offline_unit("Boiler_15 - NOx", roster) == "Boiler_15"    # real analyzer id
    assert resolve_offline_unit("Nope - UNIT", roster) is None               # unknown unit
    caps = [Capsule("Boiler_15 - UNIT", "unit-offline", _t(2), _t(4)),
            Capsule("Bogus - UNIT", "unit-offline", _t(1), _t(2))]
    assert unresolved_offline_ids(caps, roster) == ["Bogus - UNIT"], "fail-loud list"
    windows = unit_offline_windows_from_capsules(caps, roster)
    assert windows == {"Boiler_15": [(_t(2), _t(4))]}, "keyed by resolved unit"
