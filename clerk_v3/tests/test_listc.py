"""
Enriched List C tests — the standalone compliance record (clerk/listc.py)
and the WindowPick fold behavior.

Includes THE reconciliation test (task #5): Seeq detects 08:00-09:00 and
09:15-10:00 (signal valid in the 09:00-09:15 gap); the tech logs one block
08:00-10:00. The record must preserve B's two intervals AND the gap,
surface the disagreement as pending, and only null the gap when a human
explicitly picks the tech's block.
"""
from datetime import datetime, timedelta, timezone

import pytest

from clerk.fold import fold
from clerk.grid import build_grid
from clerk.listc import build_list_c
from clerk.schemas import (AnalyzerUnit, Capsule, Event, EventType,
                           OperatingWindow, SiteConfig)


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


D = _utc(2026, 4, 1)


def _h(h, m=0):
    return D + timedelta(hours=h, minutes=m)


def _cfg():
    return SiteConfig(SiteTimeZoneIANA="America/New_York", LookbackMonths=8,
                      LateXThresholdDays=7, JitterToleranceMin=5,
                      PartialOperatingHourApplicability={},
                      ReasonParagraphMap={"MM-01": "(i)", "QA-01": "(iii)"})


def _log(eid, analyzer, start, end, reason="MM-01", note="", corrective=""):
    return Event(EventID=eid, EventType=EventType.TechEntry, TargetEventID=None,
                 ExtentStartUTC=start, ExtentEndUTC=end, AnalyzerCEMIDs=[analyzer],
                 Category="", ReasonCode=reason, Actor="tech", ActedAt=start,
                 Reason=note, CorrectiveAction=corrective, DetectionClass="")


def _pick(eid, target_id, analyzer, choice, approver, acted_at, start=None, end=None):
    return Event(EventID=eid, EventType=EventType.WindowPick, TargetEventID=target_id,
                 ExtentStartUTC=start, ExtentEndUTC=end, AnalyzerCEMIDs=[analyzer],
                 Category=choice, ReasonCode="", Actor=approver, ActedAt=acted_at,
                 Reason="synthetic approval stand-in", CorrectiveAction="",
                 DetectionClass="")


def _cap(analyzer, start, end):
    return Capsule(analyzer, "status-offline", start, end)


def _build(events, capsules, start=None, end=None):
    start, end = start or _h(7), end or _h(11)
    cells = build_grid(events=events, capsules=capsules,
                       operating_windows=[OperatingWindow("U1", start, end)],
                       analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
                       qa_windows=[], config=_cfg(),
                       window_start=start, window_end=end)
    return build_list_c(events, capsules, cells)


# ---------------------------------------------------------------------------
# Fold: WindowPick is read like dismissal — recorded on replay, extent untouched
# ---------------------------------------------------------------------------

def test_fold_window_pick_recorded_without_touching_extent():
    a1 = _log("A1E", "A1", _h(8), _h(10))
    pick = _pick("P1", "A1E", "A1", "use-B", "r.huddleston", _h(12))
    obs = fold([a1, pick])[0]
    assert obs.pick_choice == "use-B"
    assert obs.pick_approver == "r.huddleston"
    assert obs.pick_acted_at == _h(12)
    assert (obs.extent_start_utc, obs.extent_end_utc) == (_h(8), _h(10)), \
        "a pick resolves the record; it never alters the observation extent"
    assert "P1" in obs.contributing_event_ids


def test_fold_last_pick_wins_on_replay():
    a1 = _log("A1E", "A1", _h(8), _h(10))
    p1 = _pick("P1", "A1E", "A1", "use-B", "first", _h(12))
    p2 = _pick("P2", "A1E", "A1", "use-A", "second", _h(13))
    obs = fold([a1, p1, p2])[0]
    assert (obs.pick_choice, obs.pick_approver) == ("use-A", "second")


# ---------------------------------------------------------------------------
# Record enrichment basics
# ---------------------------------------------------------------------------

def test_concurrence_auto_approves_and_pulls_list_a_fields():
    events = [_log("A1E", "A1", _h(8), _h(10), reason="MM-01",
                   note="Blown fuse on sample pump", corrective="Fuse replaced 10:00")]
    records = _build(events, [_cap("A1", _h(8), _h(10))])
    assert len(records) == 1
    r = records[0]
    assert r.State == "auto-approved"
    assert r.SourceUsed == "concurrence (A=B)"
    assert r.ApproverName == "auto" and r.ApproverDecision == "concurrence"
    assert r.ResolvedWindows == [(_h(8), _h(10))]
    assert r.ResolvedMinutes == 120.0
    assert r.ReasonCode == "MM-01"
    assert r.Note == "Blown fuse on sample pump"
    assert r.CorrectiveAction == "Fuse replaced 10:00"
    assert r.Disagreement == ""
    assert r.DownHours == [_h(8), _h(9)]
    assert r.GoverningParagraphs == ["(i)"]
    assert "A1E" in r.ContributingRecords
    assert any(p.startswith("CAP:") for p in r.ContributingRecords), \
        "provenance keeps both the log entry and the detection"


def test_concurrence_within_jitter_still_concurs():
    events = [_log("A1E", "A1", _h(8), _h(10))]
    records = _build(events, [_cap("A1", _h(8, 3), _h(10, 4))])  # 3-4 min off
    assert records[0].State == "auto-approved"


def test_single_source_records():
    a_only = _build([_log("A1E", "A1", _h(8), _h(9))], [])
    assert (a_only[0].State, a_only[0].SourceUsed) == ("auto-approved", "A only")
    b_only = _build([], [_cap("A1", _h(8), _h(9))])
    assert (b_only[0].State, b_only[0].SourceUsed) == ("auto-approved", "B only")


def test_build_list_c_is_idempotent():
    events = [_log("A1E", "A1", _h(8), _h(10))]
    capsules = [_cap("A1", _h(8), _h(9)), _cap("A1", _h(9, 15), _h(10))]
    assert _build(events, capsules) == _build(events, capsules)


# ---------------------------------------------------------------------------
# Task #5 — the finer-grained disagreement (15-minute valid gap)
# ---------------------------------------------------------------------------

GAP_EVENTS = [_log("A1E", "A1", _h(8), _h(10), note="Analyzer down 8-10 (tech estimate)")]
GAP_CAPS = [_cap("A1", _h(8), _h(9)), _cap("A1", _h(9, 15), _h(10))]


def test_reconciliation_pending_preserves_both_claims_and_the_gap():
    """Seeq: 08:00-09:00 + 09:15-10:00 (valid 09:00-09:15). Tech: one block
    08:00-10:00. The record must NOT silently collapse to the tech's block:
    B's two intervals and the gap stay on the record, and the state is
    pending — no human has picked yet."""
    r = _build(GAP_EVENTS, GAP_CAPS)[0]
    assert r.State == "pending", "disagreement without a pick must surface as pending"
    assert r.WindowsB == [(_h(8), _h(9)), (_h(9, 15), _h(10))], \
        "Seeq's two separate intervals are preserved verbatim"
    assert r.WindowA == [(_h(8), _h(10))], "the tech's block is preserved verbatim"
    assert "09:00-09:15" in r.Disagreement and "VALID" in r.Disagreement, \
        "the valid gap is called out explicitly, not erased"
    assert r.ApproverName == "" and r.ApprovedAtUTC is None
    # FLAG (documented in listc.py): pending ResolvedWindows = the union the
    # hourly grid already counts (Guarantee A). The gap is NOT nulled from
    # the record — it lives in WindowsB + Disagreement until a pick lands.
    assert r.SourceUsed == "union (pending)"
    assert r.ResolvedWindows == [(_h(8), _h(10))]


def test_reconciliation_pick_seeq_restores_the_gap():
    """A human picks B: the resolved extent becomes Seeq's two intervals —
    the 09:00-09:15 valid window is honored (105 minutes, not 120)."""
    pick = _pick("P1", "A1E", "A1", "use-B", "r.huddleston", _h(12))
    r = _build(GAP_EVENTS + [pick], GAP_CAPS)[0]
    assert r.State == "approved"
    assert r.SourceUsed == "pick:use-B"
    assert r.ResolvedWindows == [(_h(8), _h(9)), (_h(9, 15), _h(10))]
    assert r.ResolvedMinutes == 105.0
    assert (r.ApproverName, r.ApproverDecision) == ("r.huddleston", "pick:use-B")
    assert r.ApprovedAtUTC == _h(12)
    assert r.WindowsB == [(_h(8), _h(9)), (_h(9, 15), _h(10))], "raw claims still visible"


def test_reconciliation_pick_tech_nulls_the_gap_explicitly():
    """Only an explicit human pick of the tech's block nulls the gap — and
    the record then shows who did it and when, with B's claim still on file."""
    pick = _pick("P1", "A1E", "A1", "use-A", "r.huddleston", _h(12))
    r = _build(GAP_EVENTS + [pick], GAP_CAPS)[0]
    assert r.State == "approved"
    assert r.ResolvedWindows == [(_h(8), _h(10))]
    assert r.ResolvedMinutes == 120.0
    assert r.ApproverName == "r.huddleston"
    assert r.WindowsB == [(_h(8), _h(9)), (_h(9, 15), _h(10))], \
        "picking A does not erase B's intervals from the record"
    assert "09:00-09:15" in r.Disagreement, "the overridden gap stays documented"


def test_reconciliation_pick_corrected_window():
    pick = _pick("P1", "A1E", "A1", "corrected", "r.huddleston", _h(12),
                 start=_h(8, 5), end=_h(9, 55))
    r = _build(GAP_EVENTS + [pick], GAP_CAPS)[0]
    assert r.SourceUsed == "pick:corrected"
    assert r.ResolvedWindows == [(_h(8, 5), _h(9, 55))]
    assert r.ApproverName == "r.huddleston"


def test_pick_never_changes_hourly_verdicts():
    """Guardrail: the pick resolves the RECORD; the hourly grid (validity
    math) is identical with and without it."""
    start, end = _h(7), _h(11)
    kw = dict(operating_windows=[OperatingWindow("U1", start, end)],
              analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
              qa_windows=[], config=_cfg(), window_start=start, window_end=end)
    pick = _pick("P1", "A1E", "A1", "use-B", "r.huddleston", _h(12))
    without = build_grid(events=GAP_EVENTS, capsules=GAP_CAPS, **kw)
    with_pick = build_grid(events=GAP_EVENTS + [pick], capsules=GAP_CAPS, **kw)
    assert [(c.Analyzer, c.HourStartUTC, c.Valid, c.RuleApplied) for c in without] == \
           [(c.Analyzer, c.HourStartUTC, c.Valid, c.RuleApplied) for c in with_pick]


# ---------------------------------------------------------------------------
# v3 Gap #2 — diluent-propagated downtime must reach List C
# ---------------------------------------------------------------------------

def _diluent_setup():
    """O2 diluent monitor down 09:00-11:00 (detected); SO2 corrected to O2
    with a clean own signal. In the grid SO2 is invalid 09/10 purely by
    propagation — it has no event or capsule of its own."""
    o2_det = Event(EventID="O2D", EventType=EventType.SeeqDetection,
                   TargetEventID=None, ExtentStartUTC=_h(9), ExtentEndUTC=_h(11),
                   AnalyzerCEMIDs=["O2"], Category="", ReasonCode="", Actor="seeq",
                   ActedAt=_h(9), Reason="", CorrectiveAction="",
                   DetectionClass="status-offline")
    o2_cf = Event(EventID="O2D-CF", EventType=EventType.Confirmation,
                  TargetEventID="O2D", ExtentStartUTC=_h(9), ExtentEndUTC=_h(11),
                  AnalyzerCEMIDs=["O2"], Category="", ReasonCode="MM-01", Actor="tech",
                  ActedAt=_h(9, 5), Reason="O2 analyzer offline", CorrectiveAction="",
                  DetectionClass="")
    events = [o2_det, o2_cf]
    capsules = [Capsule("O2", "status-offline", _h(9), _h(11))]
    units = [AnalyzerUnit("SO2", "SRU", SeeqCovered=True,
                          Obligation="SO2", DiluentsRole="diluent-corrected",
                          DiluentSpecies="O2", DiluentBasis="O2"),
             AnalyzerUnit("O2", "SRU", SeeqCovered=True, Obligation="O2",
                          DiluentsRole="diluent", DiluentSpecies="O2")]
    cells = build_grid(events=events, capsules=capsules,
                       operating_windows=[OperatingWindow("SRU", _h(8), _h(12))],
                       analyzer_units=units, qa_windows=[], config=_cfg(),
                       window_start=_h(8), window_end=_h(12))
    return events, capsules, cells, units


def test_gap2_without_units_dependent_has_no_record_pre_v3_behavior():
    """Omitting analyzer_units preserves pre-v3 behavior exactly: SO2 is
    invalid in the grid but carries NO List C record (the gap)."""
    events, capsules, cells, _units = _diluent_setup()
    from clerk.schemas import CellValid
    assert any(c.Analyzer == "SO2" and c.Valid is CellValid.invalid for c in cells), \
        "grid shows SO2 down by propagation"
    records = build_list_c(events, capsules, cells)          # no units
    assert not any(r.Analyzer == "SO2" for r in records), \
        "pre-v3: dependent SO2 has no record"


def test_gap2_with_units_dependent_carries_propagated_record():
    """v3 Gap #2: passing analyzer_units emits a propagated record for SO2
    covering exactly the grid-invalid hours, citing the O2 diluent."""
    events, capsules, cells, units = _diluent_setup()
    records = build_list_c(events, capsules, cells, analyzer_units=units)
    so2 = [r for r in records if r.Analyzer == "SO2"]
    assert len(so2) == 1, "SO2 now carries a List C record"
    r = so2[0]
    assert r.DownHours == [_h(9), _h(10)], "record matches the grid's invalid hours"
    assert r.ResolvedWindows == [(_h(9), _h(11))]
    assert r.SourceUsed == "diluent-propagated (from O2)"
    assert "O2" in r.Note and "diluent" in r.Note.lower()
    assert r.ReasonCode == "MM-01", "reason pulled through from the O2 outage"
    assert any(p.startswith("DILUENT:O2") for p in r.ContributingRecords)


def test_gap2_list_c_matches_grid_no_false_all_clear():
    """The core guarantee: no analyzer is invalid in the grid yet absent
    from (or clean in) List C. Every grid-down analyzer has a record whose
    DownHours cover its grid-invalid hours."""
    events, capsules, cells, units = _diluent_setup()
    from clerk.schemas import CellValid
    records = build_list_c(events, capsules, cells, analyzer_units=units)
    down_by_analyzer = {}
    for c in cells:
        if c.Valid is CellValid.invalid:
            down_by_analyzer.setdefault(c.Analyzer, set()).add(c.HourStartUTC)
    covered = {}
    for r in records:
        covered.setdefault(r.Analyzer, set()).update(r.DownHours)
    for analyzer, grid_hours in down_by_analyzer.items():
        assert grid_hours <= covered.get(analyzer, set()), \
            f"{analyzer}: grid-down hours {grid_hours} not all in List C {covered.get(analyzer)}"


def test_gap2_dependent_with_own_and_propagated_gets_both():
    """A pollutant analyzer with its OWN outage AND a later propagated
    outage carries two records: the own one and the propagated one."""
    from clerk.schemas import CellValid
    # NOx own outage 06-08, then O2 down 13-15 propagates to NOx.
    def det(analyzer, s, e):
        d = Event(EventID=f"{analyzer}{s.hour}", EventType=EventType.SeeqDetection,
                  TargetEventID=None, ExtentStartUTC=s, ExtentEndUTC=e,
                  AnalyzerCEMIDs=[analyzer], Category="", ReasonCode="", Actor="seeq",
                  ActedAt=s, Reason="", CorrectiveAction="", DetectionClass="status-offline")
        cf = Event(EventID=f"{analyzer}{s.hour}-CF", EventType=EventType.Confirmation,
                   TargetEventID=d.EventID, ExtentStartUTC=s, ExtentEndUTC=e,
                   AnalyzerCEMIDs=[analyzer], Category="", ReasonCode="MM-01", Actor="t",
                   ActedAt=s + timedelta(minutes=5), Reason="", CorrectiveAction="",
                   DetectionClass="")
        return [d, cf], Capsule(analyzer, "status-offline", s, e)
    nox_ev, nox_cap = det("NOx", _h(6), _h(8))
    o2_ev, o2_cap = det("O2", _h(13), _h(15))
    events = nox_ev + o2_ev
    capsules = [nox_cap, o2_cap]
    units = [AnalyzerUnit("NOx", "B15", SeeqCovered=True, Obligation="NOx",
                          DiluentsRole="diluent-corrected", DiluentSpecies="O2",
                          DiluentBasis="O2"),
             AnalyzerUnit("O2", "B15", SeeqCovered=True, Obligation="O2",
                          DiluentsRole="diluent", DiluentSpecies="O2")]
    cells = build_grid(events=events, capsules=capsules,
                       operating_windows=[OperatingWindow("B15", _h(5), _h(16))],
                       analyzer_units=units, qa_windows=[], config=_cfg(),
                       window_start=_h(5), window_end=_h(16))
    records = build_list_c(events, capsules, cells, analyzer_units=units)
    nox = sorted((r for r in records if r.Analyzer == "NOx"),
                 key=lambda r: r.ResolvedWindows[0][0])
    assert len(nox) == 2, "NOx has its own record AND a propagated record"
    assert nox[0].DownHours == [_h(6), _h(7)] and "propagated" not in nox[0].SourceUsed
    assert nox[1].DownHours == [_h(13), _h(14)]
    assert nox[1].SourceUsed == "diluent-propagated (from O2)"


def test_gap2_idempotent_with_units():
    events, capsules, cells, units = _diluent_setup()
    a = build_list_c(events, capsules, cells, analyzer_units=units)
    b = build_list_c(events, capsules, cells, analyzer_units=units)
    assert a == b
