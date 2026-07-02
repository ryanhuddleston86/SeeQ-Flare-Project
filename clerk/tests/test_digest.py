"""
Step 7 digest.py tests — section presence, order, and content.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from clerk.delta import DeltaResult
from clerk.diff import CellFlip, DiffResult, FlipOutcome, LateArrival, NewInvalidCell
from clerk.digest import render_digest
from clerk.schemas import Event, EventType

RUN_AT = datetime(2026, 4, 2, 6, 0, tzinfo=timezone.utc)
RUN_DATE = date(2026, 4, 2)
HOUR = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)


def _event(id, event_type, target, start, end, analyzer, acted_at,
           actor="test", reason=""):
    return Event(
        EventID=id, EventType=event_type, TargetEventID=target,
        ExtentStartUTC=start, ExtentEndUTC=end, AnalyzerCEMIDs=[analyzer],
        Category="", ReasonCode="", Actor=actor, ActedAt=acted_at,
        Reason=reason, CorrectiveAction="", DetectionClass="",
    )


def _empty_diff(is_first_run=False, new_cell_count=0):
    return DiffResult(is_first_run=is_first_run, new_cell_count=new_cell_count)


def _empty_delta():
    return DeltaResult()


# ---------------------------------------------------------------------------
# Section order and presence
# ---------------------------------------------------------------------------

def test_sections_appear_in_spec_order():
    text = render_digest(RUN_AT, [], _empty_diff(), _empty_delta(), RUN_DATE)
    order = [
        "LastSuccessfulRun",
        "## Integrity Alerts",
        "## New Downtime Since Yesterday",
        "## Late-Arriving Invalid Hours",
        "## Pending Dismissals",
        "## Withdrawals & Conflicts",
        "## Summary",
    ]
    positions = [text.index(marker) for marker in order]
    assert positions == sorted(positions), "sections must appear in spec order"


def test_heartbeat_is_the_first_line():
    text = render_digest(RUN_AT, [], _empty_diff(), _empty_delta(), RUN_DATE)
    first_line = text.splitlines()[0]
    assert first_line == f"# Clerk run {RUN_AT.isoformat()} — LastSuccessfulRun"


def test_all_empty_sections_still_render_none():
    text = render_digest(RUN_AT, [], _empty_diff(), _empty_delta(), RUN_DATE)
    assert text.count("None.") >= 4  # alerts, new downtime, late arrivals, pending, w&c


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------

def test_first_run_collapses_flip_sections_to_a_note():
    diff = _empty_diff(is_first_run=True, new_cell_count=42)
    text = render_digest(RUN_AT, [], diff, _empty_delta(), RUN_DATE)
    assert "First run" in text
    assert "42 cells reported as new" in text


# ---------------------------------------------------------------------------
# Integrity alerts / new downtime / late arrivals — rendered from DiffResult
# ---------------------------------------------------------------------------

def test_integrity_alert_rendered():
    diff = _empty_diff()
    diff.integrity_alerts.append(
        CellFlip("A1", HOUR, FlipOutcome.integrity_alert, "no explanation found", ["E1"]))
    text = render_digest(RUN_AT, [], diff, _empty_delta(), RUN_DATE)
    assert "A1" in text and "no explanation found" in text


def test_new_downtime_rendered():
    diff = _empty_diff()
    diff.new_invalid_cells.append(NewInvalidCell("A1", HOUR))
    text = render_digest(RUN_AT, [], diff, _empty_delta(), RUN_DATE)
    section = text.split("## New Downtime Since Yesterday")[1].split("##")[0]
    assert "A1" in section
    assert HOUR.isoformat() in section


def test_late_arrival_rendered_with_age():
    diff = _empty_diff()
    diff.late_arrivals.append(LateArrival("A1", HOUR, 12))
    text = render_digest(RUN_AT, [], diff, _empty_delta(), RUN_DATE)
    section = text.split("## Late-Arriving Invalid Hours")[1].split("##")[0]
    assert "12 days old" in section


# ---------------------------------------------------------------------------
# Pending dismissals + age + cancel-approval flags
# ---------------------------------------------------------------------------

def test_pending_dismissal_shows_age_from_proposal():
    hour_end = HOUR + timedelta(hours=1)
    proposed_at = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)  # after HOUR's ActedAt
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("E2", EventType.DismissalProposed, "E1", HOUR, hour_end, "A1", proposed_at),
    ]
    text = render_digest(RUN_AT, events, _empty_diff(), _empty_delta(), RUN_DATE)
    section = text.split("## Pending Dismissals")[1].split("##")[0]
    assert "E1" in section
    expected_age = (RUN_DATE - proposed_at.date()).days
    assert f"{expected_age} days old" in section


def test_pending_dismissal_with_cancel_approval_flag():
    hour_end = HOUR + timedelta(hours=1)
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("E2", EventType.DismissalProposed, "E1", HOUR, hour_end, "A1",
              HOUR + timedelta(minutes=10)),
    ]
    delta = DeltaResult(new_events=[], flags=[
        "CANCEL-APPROVAL withdrawn episode E1 has a dismissal in flight — "
        "cancel the approval card; digest",
    ])
    text = render_digest(RUN_AT, events, _empty_diff(), delta, RUN_DATE)
    section = text.split("## Pending Dismissals")[1].split("##")[0]
    assert "CANCEL-APPROVAL" in section
    assert "E1" in section


def test_confirmed_observation_is_not_a_pending_dismissal():
    hour_end = HOUR + timedelta(hours=1)
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("E2", EventType.Confirmation, "E1", HOUR, hour_end, "A1",
              HOUR + timedelta(minutes=10)),
    ]
    text = render_digest(RUN_AT, events, _empty_diff(), _empty_delta(), RUN_DATE)
    section = text.split("## Pending Dismissals")[1].split("##")[0]
    assert "None." in section


# ---------------------------------------------------------------------------
# Withdrawals & conflicts
# ---------------------------------------------------------------------------

def test_withdrawal_rendered_from_delta_new_events():
    withdrawn = _event("N1", EventType.Withdrawn, "E1", HOUR, HOUR + timedelta(hours=1),
                       "A1", RUN_AT, actor="clerk-delta",
                       reason="no matching capsule in re-detection window")
    delta = DeltaResult(new_events=[withdrawn], flags=[])
    text = render_digest(RUN_AT, [], _empty_diff(), delta, RUN_DATE)
    section = text.split("## Withdrawals & Conflicts")[1].split("##")[0]
    assert "E1" in section
    assert "no matching capsule" in section


def test_conflict_rendered_from_delta_flags():
    delta = DeltaResult(new_events=[], flags=[
        "CONFLICT confirmed episode E9 (A1) has no matching capsule — "
        "human assertion stands, no Withdrawn; digest",
    ])
    text = render_digest(RUN_AT, [], _empty_diff(), delta, RUN_DATE)
    section = text.split("## Withdrawals & Conflicts")[1].split("##")[0]
    assert "CONFLICT" in section
    assert "E9" in section


def test_non_withdrawn_new_events_and_non_conflict_flags_excluded():
    """Only Withdrawn events and CONFLICT-prefixed flags belong here — a
    BoundaryUpdate or an ABUTMENT flag must not leak into this section."""
    boundary_update = _event("N1", EventType.BoundaryUpdate, "E1", HOUR,
                             HOUR + timedelta(hours=1), "A1", RUN_AT, actor="clerk-delta")
    delta = DeltaResult(new_events=[boundary_update], flags=[
        "ABUTMENT capsules A1 08:00-09:00 and 09:00-10:00 — Ryan to rule",
    ])
    text = render_digest(RUN_AT, [], _empty_diff(), delta, RUN_DATE)
    section = text.split("## Withdrawals & Conflicts")[1].split("##")[0]
    assert "None." in section


# ---------------------------------------------------------------------------
# Summary counts
# ---------------------------------------------------------------------------

def test_summary_counts_match_inputs():
    diff = _empty_diff()
    diff.integrity_alerts.append(
        CellFlip("A1", HOUR, FlipOutcome.integrity_alert, "x", ["E1"]))
    diff.silent_passes.append(
        CellFlip("A2", HOUR, FlipOutcome.silent_pass, "x", ["E2"]))
    diff.late_arrivals.append(LateArrival("A1", HOUR, 10))
    diff.new_invalid_cells.append(NewInvalidCell("A1", HOUR))

    withdrawn = _event("N1", EventType.Withdrawn, "E3", HOUR, HOUR + timedelta(hours=1),
                       "A1", RUN_AT, actor="clerk-delta")
    delta = DeltaResult(new_events=[withdrawn], flags=["CONFLICT x"])

    text = render_digest(RUN_AT, [], diff, delta, RUN_DATE)
    section = text.split("## Summary")[1]
    assert "Integrity alerts: 1" in section
    assert "Silent passes: 1" in section
    assert "Late-arriving invalid cells: 1" in section
    assert "New invalid cells: 1" in section
    assert "Withdrawals tonight: 1" in section
    assert "Conflicts: 1" in section


def test_first_run_summary_uses_new_cell_count():
    diff = _empty_diff(is_first_run=True, new_cell_count=99)
    text = render_digest(RUN_AT, [], diff, _empty_delta(), RUN_DATE)
    section = text.split("## Summary")[1]
    assert "New invalid cells: 99" in section
