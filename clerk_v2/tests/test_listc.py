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
