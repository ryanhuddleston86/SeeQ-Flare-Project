"""
F8 — Golden validation set: Doc 50 "Clerk v2 Golden Traps — Answer Key".

Doc 50 supplied by Ryan 2026-07-11 (in-session; the document itself is not
in the repo). Each trap below encodes Doc 50's hand-derived Expected line
verbatim; the clerk's output is diffed against it. Where clerk and Doc 50
disagreed during encoding, the CLERK was fixed (T2: reason→paragraph
resolution was unwired in build_grid; T8: start-only entries crashed the
grid) — no expected value was bent.

Conventions per Doc 50: all times UTC, dense sampling assumed, quadrants
Q1=:00–:15 … Q4=:45–:00. "Down" = CellValid.invalid. "Dropped" =
CellValid.not_operating (out of numerator AND denominator, still recorded).

Synthetic plant (Doc 50):
  B15: NOx (diluent-corrected→O2), O2 (diluent), CO (diluent-corrected→O2),
       TEMP-degF (not-diluent-corrected)
  FCC: NOx-P permanent, NOx-T temp  (O2-F omitted from the rollup roster —
       see the encoding note on T5 in the fix-pass report: the rollup is
       per-obligation, and Doc 50's expected values consider only the NOx
       obligation's monitors)
  SRU: SO2 (diluent-corrected→CO2), CO2 (diluent)

Global invariant (Doc 50): NO not_assessed hour anywhere in the set —
asserted inside _build for every scenario.
"""
from datetime import datetime, timedelta, timezone

import pytest

from clerk.fold import fold
from clerk.grid import build_grid, source_down_hours
from clerk.run import _adjudicated_condition_rows
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
from list_c_recalc import DowntimeRecord, compute_modified_calc


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# The real reason vocabulary, mapped as seeded in config (F7):
_REASON_MAP = {"MM-01": "(i)", "NM-01": "(i)", "QA-01": "(iii)",
               "OK-01": "(i)", "UK-01": "(i)"}


def _cfg():
    return SiteConfig(
        SiteTimeZoneIANA="America/New_York", LookbackMonths=8,
        LateXThresholdDays=7, JitterToleranceMin=5,
        PartialOperatingHourApplicability={},
        ReasonParagraphMap=dict(_REASON_MAP),
    )


_ids = iter(range(1, 1000))


def _seeq(analyzer, start, end, cls="status-offline"):
    eid = f"GS{next(_ids)}"
    return Event(EventID=eid, EventType=EventType.SeeqDetection,
                 TargetEventID=None, ExtentStartUTC=start, ExtentEndUTC=end,
                 AnalyzerCEMIDs=[analyzer], Category="", ReasonCode="",
                 Actor="seeq-auto", ActedAt=start, Reason="", CorrectiveAction="",
                 DetectionClass=cls)


def _confirm(target, analyzer, start, end, reason):
    eid = f"GC{next(_ids)}"
    return Event(EventID=eid, EventType=EventType.Confirmation,
                 TargetEventID=target.EventID, ExtentStartUTC=start,
                 ExtentEndUTC=end, AnalyzerCEMIDs=[analyzer], Category="",
                 ReasonCode=reason, Actor="tech", ActedAt=start + timedelta(minutes=1),
                 Reason="", CorrectiveAction="", DetectionClass="")


def _outage(analyzer, start, end, reason):
    """A confirmed detected outage carrying a reason code — one observation."""
    det = _seeq(analyzer, start, end)
    return [det, _confirm(det, analyzer, start, end, reason)]


def _tech(analyzer, start, end, reason):
    eid = f"GT{next(_ids)}"
    return Event(EventID=eid, EventType=EventType.TechEntry, TargetEventID=None,
                 ExtentStartUTC=start, ExtentEndUTC=end, AnalyzerCEMIDs=[analyzer],
                 Category="", ReasonCode=reason, Actor="tech", ActedAt=start,
                 Reason="", CorrectiveAction="", DetectionClass="")


def _build(events=(), capsules=(), operating=(), units=(), start=None, end=None,
           validations=()):
    cells = build_grid(
        events=list(events), capsules=list(capsules),
        operating_windows=list(operating), analyzer_units=list(units),
        qa_windows=[], config=_cfg(), window_start=start, window_end=end,
        validations=list(validations),
    )
    # Doc 50 global invariant: no not_assessed hour anywhere in the set.
    assert not any(c.Valid is CellValid.not_assessed for c in cells)
    return cells


def _down(cells, analyzer):
    return sorted(c.HourStartUTC for c in cells
                  if c.Analyzer == analyzer and c.Valid is CellValid.invalid)


def _dropped(cells, analyzer):
    return sorted(c.HourStartUTC for c in cells
                  if c.Analyzer == analyzer and c.Valid is CellValid.not_operating)


D = _utc(2026, 4, 1)  # trap day


def _h(hour, minute=0):
    return D + timedelta(hours=hour, minutes=minute)


# ---------------------------------------------------------------------------
# T1 — (iii)(A) is 15-minute separation (kills the ≥30-min bug)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case,valid_end_min,expect_down", [
    ("T1a", 18, True),    # 13-min span -> DOWN
    ("T1b", 22, False),   # 17-min span -> NOT DOWN (30-min-bug discriminator)
    ("T1c", 19, True),    # 14-min span -> DOWN
    ("T1d", 20, False),   # exactly 15  -> NOT DOWN (30-min-bug discriminator)
])
def test_t1_iii_a_fifteen_minute_separation(case, valid_end_min, expect_down):
    """Hour 09:00, reason QA-01 -> (iii), all four quadrants operating.
    Valid only 09:05–09:xx, remainder invalid."""
    events = (_outage("NOx", _h(9), _h(9, 5), "QA-01")
              + _outage("NOx", _h(9, valid_end_min), _h(10), "QA-01"))
    cells = _build(
        events=events,
        operating=[OperatingWindow("B15", _h(9), _h(10))],
        units=[AnalyzerUnit("NOx", "B15", SeeqCovered=True)],
        start=_h(9), end=_h(10),
    )
    assert _down(cells, "NOx") == ([_h(9)] if expect_down else []), \
        f"{case}: span {valid_end_min - 5} min vs the >=15 test"
    assert cells[0].RuleApplied.startswith("(iii)"), \
        "QA-01 must route the hour through the (iii) family"


# ---------------------------------------------------------------------------
# T2 — Paragraph selection by reason (same window, two reasons)
# ---------------------------------------------------------------------------

def _t2_cells(reason):
    return _build(
        events=_outage("NOx", _h(8, 22), _h(10, 40), reason),
        operating=[OperatingWindow("B15", _h(8), _h(11))],
        units=[AnalyzerUnit("NOx", "B15", SeeqCovered=True)],
        start=_h(8), end=_h(11),
    )


def test_t2a_qa_reason_selects_iii_one_down_hour():
    """QA-01 -> (iii): hr8 keeps 08:00–08:22 (22 min >= 15) -> valid; hr9 all
    invalid -> down; hr10 keeps 10:40–11:00 (20 min) -> valid.
    Expected down = {09:00} (1 hour)."""
    assert _down(_t2_cells("QA-01"), "NOx") == [_h(9)]


def test_t2b_mm_reason_selects_i_three_down_hours():
    """MM-01 -> (i): hr8 Q3+Q4 consumed -> down; hr9 down; hr10 Q1+Q2
    consumed -> down. Expected down = {08:00, 09:00, 10:00} (3 hours).
    Discriminator vs T2a: a build with one fixed test gives the same count
    for both reasons; correct is 1 vs 3."""
    assert _down(_t2_cells("MM-01"), "NOx") == [_h(8), _h(9), _h(10)]


# ---------------------------------------------------------------------------
# T3 — Unit-offline gate
# ---------------------------------------------------------------------------

def test_t3a_full_offline_hour_dropped_not_down():
    """Unit fully offline 09:00–10:00, online 10:00–11:00; NOx invalid
    09:00–11:00 (MM-01). Expected: hr9 DROPPED (recorded, out of num+denom);
    hr10 DOWN."""
    cells = _build(
        events=_outage("NOx", _h(9), _h(11), "MM-01"),
        operating=[OperatingWindow("B15", _h(8), _h(9)),
                   OperatingWindow("B15", _h(10), _h(11))],
        units=[AnalyzerUnit("NOx", "B15", SeeqCovered=True)],
        start=_h(8), end=_h(11),
    )
    assert _down(cells, "NOx") == [_h(10)]
    assert _dropped(cells, "NOx") == [_h(9)]


def test_t3b_partial_offline_hour_is_evaluated_not_dropped():
    """Unit offline only 10:00–10:30 (Q1,Q2 of hr10); NOx invalid all hr10.
    Expected: mask Q1,Q2 and evaluate the operating remainder -> hr10 DOWN,
    dropped {}. A build dropping the partial-offline hour loses a real
    down hour."""
    cells = _build(
        events=_outage("NOx", _h(10), _h(11), "MM-01"),
        operating=[OperatingWindow("B15", _h(8), _h(10)),
                   OperatingWindow("B15", _h(10, 30), _h(11))],
        units=[AnalyzerUnit("NOx", "B15", SeeqCovered=True)],
        start=_h(8), end=_h(11),
    )
    assert _down(cells, "NOx") == [_h(10)]
    assert _dropped(cells, "NOx") == []


# ---------------------------------------------------------------------------
# T4 — Diluent propagation (O2), one-way
# ---------------------------------------------------------------------------

def test_t4_o2_diluent_propagates_one_way():
    """O2 invalid 09:00–11:00 (MM-01); NOx and CO have valid own signal all
    day; NOx has its OWN outage 13:00–14:00; TEMP-degF not corrected.
    Expected: O2 {09,10}; NOx {09,10,13}; CO {09,10}; TEMP {}; and O2 NOT
    down at 13:00 (one-way)."""
    units = [
        AnalyzerUnit("NOx", "B15", SeeqCovered=True,
                     DiluentsRole="diluent-corrected", DiluentSpecies="O2",
                     DiluentBasis="O2"),
        AnalyzerUnit("O2", "B15", SeeqCovered=True,
                     DiluentsRole="diluent", DiluentSpecies="O2"),
        AnalyzerUnit("CO", "B15", SeeqCovered=True,
                     DiluentsRole="diluent-corrected", DiluentSpecies="O2",
                     DiluentBasis="O2"),
        AnalyzerUnit("TEMP-degF", "B15", SeeqCovered=True),
    ]
    events = (_outage("O2", _h(9), _h(11), "MM-01")
              + _outage("NOx", _h(13), _h(14), "MM-01"))
    cells = _build(
        events=events,
        operating=[OperatingWindow("B15", _h(8), _h(15))],
        units=units, start=_h(8), end=_h(15),
    )
    assert _down(cells, "O2") == [_h(9), _h(10)]
    assert _down(cells, "NOx") == [_h(9), _h(10), _h(13)], \
        "09–10 propagated from O2 despite valid own signal; 13 is NOx's own"
    assert _down(cells, "CO") == [_h(9), _h(10)], "propagated"
    assert _down(cells, "TEMP-degF") == [], "not diluent-corrected — unaffected"
    assert _h(13) not in _down(cells, "O2"), \
        "one-way: NOx's own outage must not propagate back to O2"


# ---------------------------------------------------------------------------
# T5 — Redundant temp coverage + coverage-window gating
# ---------------------------------------------------------------------------

def _t5_cells(hour, perm_invalid, temp_invalid):
    def cell(analyzer, invalid):
        return GridCell(Analyzer=analyzer, HourStartUTC=hour, HourLocalLabel="",
                        OperatingFraction=1.0,
                        Valid=CellValid.invalid if invalid else CellValid.valid,
                        RuleApplied="(i)", ContributingEventIDs=[])
    return [cell("NOx-P", perm_invalid), cell("NOx-T", temp_invalid)]


def _t5_grid():
    cells = []
    for h in range(10, 16):
        cells.extend(_t5_cells(_h(h), perm_invalid=h in (10, 11, 12, 13),
                               temp_invalid=h in (12, 13, 14, 15)))
    return cells


def test_t5a_full_coverage_source_down_is_intersection():
    """Both in coverage all window. Monitor-level ALWAYS reported (global
    invariant); source-down = {12:00, 13:00} only. The 10:00–11:00
    report-vs-emissions divergence (source covered while NOx-P's own data
    is invalid) is expected, not a bug."""
    units = [AnalyzerUnit("NOx-P", "FCC", SeeqCovered=True, Obligation="NOx"),
             AnalyzerUnit("NOx-T", "FCC", SeeqCovered=True, Obligation="NOx")]
    cells = _t5_grid()
    assert [c.HourStartUTC for c in cells
            if c.Analyzer == "NOx-P" and c.Valid is CellValid.invalid] == \
        [_h(h) for h in (10, 11, 12, 13)], "monitor-level downtime reported"
    assert [c.HourStartUTC for c in cells
            if c.Analyzer == "NOx-T" and c.Valid is CellValid.invalid] == \
        [_h(h) for h in (12, 13, 14, 15)], "monitor-level downtime reported"
    # v3: keyed by (unit, obligation); the two redundant NOx monitors share
    # the NOx obligation and still intersect together.
    assert source_down_hours(units, cells) == {("FCC", "NOx"): [_h(12), _h(13)]}


def test_t5b_coverage_gating_in_service_at_noon():
    """NOx-T InServiceDate = 12:00: at 10–11 only NOx-P is in coverage and
    it is down -> those hours are source-down. Expected {10,11,12,13}.
    Discriminator: without gating the not-yet-deployed temp's 'valid' cells
    yield {12,13} — the temp-CEMS bug."""
    units = [AnalyzerUnit("NOx-P", "FCC", SeeqCovered=True, Obligation="NOx"),
             AnalyzerUnit("NOx-T", "FCC", SeeqCovered=True, Obligation="NOx",
                          InServiceDateUTC=_h(12))]
    assert source_down_hours(units, _t5_grid()) == \
        {("FCC", "NOx"): [_h(h) for h in (10, 11, 12, 13)]}


def test_t5c_manual_only_temp_same_result():
    """Identical to T5b but NOx-T's downtime comes from a manual (List A)
    TechEntry with no capsule. Same expected: {10,11,12,13}."""
    units = [AnalyzerUnit("NOx-P", "FCC", SeeqCovered=True, Obligation="NOx"),
             AnalyzerUnit("NOx-T", "FCC", SeeqCovered=True, Obligation="NOx",
                          InServiceDateUTC=_h(12))]
    events = [_tech("NOx-T", _h(12), _h(16), "MM-01")]
    capsules = [Capsule("NOx-P", "status-offline", _h(10), _h(14))]
    cells = _build(
        events=events, capsules=capsules,
        operating=[OperatingWindow("FCC", _h(10), _h(16))],
        units=units, start=_h(10), end=_h(16),
    )
    assert source_down_hours(units, cells) == \
        {("FCC", "NOx"): [_h(h) for h in (10, 11, 12, 13)]}


def test_t5d_v3_per_obligation_a_valid_pollutant_never_masks_another():
    """v3 Gap #1 (T5a intent, the multi-obligation discriminator): a unit
    with a NOx obligation and a separate O2 obligation. NOx is down 06-08;
    O2 is valid throughout. The per-UNIT intersection (pre-v3) would report
    the unit NOT down at 06/07 because the valid O2 monitor masks it. The
    per-OBLIGATION rollup must report the NOx obligation down at 06/07 while
    O2 stays covered — a valid monitor for one pollutant never covers
    another pollutant's outage (D8)."""
    units = [AnalyzerUnit("B15-NOx", "Boiler_15", SeeqCovered=True, Obligation="NOx"),
             AnalyzerUnit("B15-O2", "Boiler_15", SeeqCovered=True, Obligation="O2",
                          DiluentsRole="diluent", DiluentSpecies="O2")]
    cells = []
    for h in range(5, 9):
        cells.append(GridCell("B15-NOx", _h(h), "", 1.0,
                              CellValid.invalid if h in (6, 7) else CellValid.valid,
                              "(i)", []))
        cells.append(GridCell("B15-O2", _h(h), "", 1.0, CellValid.valid, "(i)", []))
    rollup = source_down_hours(units, cells)
    assert rollup == {("Boiler_15", "NOx"): [_h(6), _h(7)]}, \
        "NOx obligation down 06/07; O2 obligation covered; no cross-masking"
    assert ("Boiler_15", "O2") not in rollup


# ---------------------------------------------------------------------------
# T6 — Approved-counts-only
# ---------------------------------------------------------------------------

def test_t6_only_approved_extents_count():
    """E1 Approved 2 h, E2 Pending 3 h, E3 Rejected 1 h. Expected counted
    downtime = 2 hours. A status-blind sum returns 6."""
    records = [
        DowntimeRecord("E1", _h(10), _h(12), "Approved", _h(12)),
        DowntimeRecord("E2", _h(13), _h(16), "Pending", _h(16)),
        DowntimeRecord("E3", _h(20), _h(21), "Rejected", _h(21)),
    ]
    assert compute_modified_calc(records) == {(2026, 4): pytest.approx(2.0)}


# ---------------------------------------------------------------------------
# T7 — Backdate to last passing validation event
# ---------------------------------------------------------------------------

def test_t7_backdate_anchor_is_the_passing_validation_event():
    """Validation passes Day1 06:00; data reads valid through Day2 06:00;
    validation fails Day2 06:00. Expected: all hours Day1 06:00 -> Day2
    06:00 invalid; anchor = Day1 06:00. (The failure hour itself is also
    invalid per (iv) — no in-hour recovery.) A build anchoring on the last
    valid data cell invalidates almost nothing."""
    day1, day2 = _utc(2026, 4, 1, 6), _utc(2026, 4, 2, 6)
    start, end = _utc(2026, 4, 1, 4), _utc(2026, 4, 2, 8)
    cells = _build(
        operating=[OperatingWindow("B15", start, end)],
        units=[AnalyzerUnit("NOx", "B15", SeeqCovered=True)],
        start=start, end=end,
        validations=[ValidationEvent("NOx", day1, True),
                     ValidationEvent("NOx", day2, False)],
    )
    expected_invalid = []
    hour = day1
    while hour <= day2:
        expected_invalid.append(hour)
        hour += timedelta(hours=1)
    assert _down(cells, "NOx") == expected_invalid
    assert _dropped(cells, "NOx") == []


# ---------------------------------------------------------------------------
# T8 — Start-only entry
# ---------------------------------------------------------------------------

def test_t8_start_only_entry_recorded_but_no_window():
    """A 'filter change' logged at 09:15, start only, no end. Expected: the
    entry is recorded; it produces ZERO down-hours by itself. A build
    treating it as open-ended downtime marks every subsequent hour down —
    and this build CRASHED on it before the fix."""
    marker = _tech("NOx", _h(9, 15), None, "QA-01")
    cells = _build(
        events=[marker],
        operating=[OperatingWindow("B15", _h(8), _h(12))],
        units=[AnalyzerUnit("NOx", "B15", SeeqCovered=True)],
        start=_h(8), end=_h(12),
    )
    assert _down(cells, "NOx") == [], "a marker is not a window"
    # Recorded: the fold keeps the observation, and the adjudicated output
    # carries the row (blank end), so nothing is silently lost.
    observations = fold([marker])
    assert len(observations) == 1
    rows = _adjudicated_condition_rows(observations,
                                       {marker.EventID: marker}, cells)
    assert len(rows) == 1
    assert rows[0].ConditionEndUTC is None


# ---------------------------------------------------------------------------
# T9 — Whole-hour counting, not minutes
# ---------------------------------------------------------------------------

def test_t9_ten_invalid_minutes_do_not_down_the_hour():
    """NOx invalid 09:05–09:10 and 09:35–09:40 (10 invalid minutes total),
    QA-01 -> (iii). Two valid points >= 15 min apart exist. Expected: hour
    NOT DOWN. A build summing invalid minutes or failing on any gap marks
    it down; the reg counts whole hours."""
    events = (_outage("NOx", _h(9, 5), _h(9, 10), "QA-01")
              + _outage("NOx", _h(9, 35), _h(9, 40), "QA-01"))
    cells = _build(
        events=events,
        operating=[OperatingWindow("B15", _h(9), _h(10))],
        units=[AnalyzerUnit("NOx", "B15", SeeqCovered=True)],
        start=_h(9), end=_h(10),
    )
    assert _down(cells, "NOx") == []


# ---------------------------------------------------------------------------
# T10 — CO2 diluent path
# ---------------------------------------------------------------------------

def test_t10_co2_diluent_propagates_to_so2():
    """SRU: CO2 invalid 09:00–10:00 (MM-01); SO2 has valid own signal,
    corrected to CO2. Expected: SO2 down {09:00}; CO2 down {09:00}.
    Propagation follows the CO2 basis — a build hardwired to O2 misses it."""
    units = [
        AnalyzerUnit("SO2", "SRU", SeeqCovered=True,
                     DiluentsRole="diluent-corrected", DiluentSpecies="CO2",
                     DiluentBasis="CO2"),
        AnalyzerUnit("CO2", "SRU", SeeqCovered=True,
                     DiluentsRole="diluent", DiluentSpecies="CO2"),
    ]
    cells = _build(
        events=_outage("CO2", _h(9), _h(10), "MM-01"),
        operating=[OperatingWindow("SRU", _h(8), _h(11))],
        units=units, start=_h(8), end=_h(11),
    )
    assert _down(cells, "CO2") == [_h(9)]
    assert _down(cells, "SO2") == [_h(9)], "propagated via the CO2 basis"
