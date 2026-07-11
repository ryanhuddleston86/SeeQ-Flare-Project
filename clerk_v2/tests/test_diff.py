"""
Step 7 diff.py tests (gate G5).

Every ✗→✓ flip must land in exactly one of three buckets — silent_passes,
machine_informational, or integrity_alerts — never a fourth outcome.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from clerk.diff import FlipOutcome, diff_grids
from clerk.grid import capsule_provenance_id
from clerk.schemas import Capsule, CellValid, Event, EventType, GridCell

HOUR = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)
RUN_DATE = date(2026, 4, 1)
TZ = "America/New_York"


def _event(id, event_type, target, start, end, analyzer, acted_at,
           actor="test", detection_class=""):
    return Event(
        EventID=id, EventType=event_type, TargetEventID=target,
        ExtentStartUTC=start, ExtentEndUTC=end, AnalyzerCEMIDs=[analyzer],
        Category="", ReasonCode="", Actor=actor, ActedAt=acted_at,
        Reason="", CorrectiveAction="", DetectionClass=detection_class,
    )


def _cell(analyzer, hour, valid, contributing=()):
    return GridCell(
        Analyzer=analyzer, HourStartUTC=hour, HourLocalLabel="x",
        OperatingFraction=1.0, Valid=valid, RuleApplied="(i)",
        ContributingEventIDs=list(contributing),
    )


def _diff(prior, current, events, late_x=7, run_date=RUN_DATE):
    return diff_grids(prior, current, events, run_date, late_x, TZ)


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------

def test_first_run_no_prior_cells_reports_everything_new():
    current = [_cell("A1", HOUR, CellValid.invalid)]
    result = _diff([], current, [])
    assert result.is_first_run is True
    assert result.new_cell_count == 1
    assert result.silent_passes == result.machine_informational == result.integrity_alerts == []


# ---------------------------------------------------------------------------
# Non-flips: nothing traced, nothing pinged
# ---------------------------------------------------------------------------

def test_valid_to_invalid_is_not_a_flip_no_trace_attempted():
    prior = [_cell("A1", HOUR, CellValid.valid)]
    current = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    result = _diff(prior, current, [])
    assert result.silent_passes == result.machine_informational == []
    # it's a NEW invalid cell -> only the late-arrival check applies to it


def test_unchanged_invalid_is_not_a_flip():
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    current = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    result = _diff(prior, current, [])
    assert result.silent_passes == result.machine_informational == result.integrity_alerts == []
    assert result.late_arrivals == []  # not NEW invalid


def test_unchanged_valid_is_a_no_op():
    prior = [_cell("A1", HOUR, CellValid.valid)]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, [])
    assert result.silent_passes == result.machine_informational == result.integrity_alerts == []


# ---------------------------------------------------------------------------
# Silent pass — Approval covering the extent
# ---------------------------------------------------------------------------

def test_flip_traces_to_approval_silent_pass():
    hour_end = HOUR + timedelta(hours=1)
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("E2", EventType.DismissalProposed, "E1", HOUR, hour_end, "A1",
              HOUR + timedelta(minutes=10)),
        _event("E3", EventType.Approval, "E1", HOUR, hour_end, "A1",
              HOUR + timedelta(minutes=20), actor="supervisor"),
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.silent_passes) == 1
    assert result.silent_passes[0].origin_event_ids == ["E1"]
    assert result.machine_informational == result.integrity_alerts == []


def test_approval_not_covering_the_hour_is_integrity_alert():
    """Dismissed, but the signed extent is for a DIFFERENT hour — this flip
    cannot trace to it."""
    other_hour_end = HOUR + timedelta(hours=3)
    other_hour_start = HOUR + timedelta(hours=2)
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, HOUR + timedelta(hours=3),
              "A1", HOUR),
        _event("E2", EventType.DismissalProposed, "E1", other_hour_start, other_hour_end,
              "A1", HOUR + timedelta(minutes=10)),
        _event("E3", EventType.Approval, "E1", other_hour_start, other_hour_end,
              "A1", HOUR + timedelta(minutes=20), actor="supervisor"),
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.integrity_alerts) == 1
    assert result.silent_passes == []


# ---------------------------------------------------------------------------
# Machine-informational — Withdrawn / BoundaryUpdate
# ---------------------------------------------------------------------------

def test_flip_traces_to_machine_withdrawn_informational():
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, HOUR + timedelta(hours=1),
              "A1", HOUR),
        _event("E2", EventType.Withdrawn, "E1", HOUR, HOUR + timedelta(hours=1),
              "A1", HOUR + timedelta(days=1), actor="clerk-delta"),
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.machine_informational) == 1
    assert result.silent_passes == result.integrity_alerts == []


def test_withdrawn_but_not_machine_attributed_is_integrity_alert():
    """Structurally possible per fold.py (no origin-type/actor restriction
    on event effects) — must not be silently trusted just because the
    status says Withdrawn."""
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, HOUR + timedelta(hours=1),
              "A1", HOUR),
        _event("E2", EventType.Withdrawn, "E1", HOUR, HOUR + timedelta(hours=1),
              "A1", HOUR + timedelta(days=1), actor="jsmith"),  # human, not clerk-delta
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.integrity_alerts) == 1
    assert result.machine_informational == []


def test_flip_traces_to_machine_boundary_update_informational():
    """The ticket's extent moved off this hour via a machine BoundaryUpdate
    — status is unaffected (still Needs review), only the extent moved."""
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, HOUR + timedelta(hours=1),
              "A1", HOUR),
        _event("E2", EventType.BoundaryUpdate, "E1",
              HOUR + timedelta(hours=2), HOUR + timedelta(hours=3),
              "A1", HOUR + timedelta(days=1), actor="clerk-delta"),
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.machine_informational) == 1
    assert result.silent_passes == result.integrity_alerts == []


# ---------------------------------------------------------------------------
# Integrity alert — the unapproved-flip safety net (Correction, human path)
# ---------------------------------------------------------------------------

def test_correction_moving_boundary_off_hour_is_integrity_alert():
    """The safety check fold.py's docstring deferred to diff.py: a
    reductive Correction (human path) moving the boundary such that an
    hour flips valid, WITHOUT a machine BoundaryUpdate, must NOT silently
    pass — no fourth outcome."""
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, HOUR + timedelta(hours=1),
              "A1", HOUR),
        _event("E2", EventType.Correction, "E1",
              HOUR + timedelta(hours=2), HOUR + timedelta(hours=3),
              "A1", HOUR + timedelta(days=1), actor="jsmith"),
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.integrity_alerts) == 1
    assert result.silent_passes == result.machine_informational == []


def test_no_explanation_at_all_is_integrity_alert():
    """Ticket still covers the hour, still Needs review — nothing in its
    history explains why the cell would have flipped. Untraceable."""
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, HOUR + timedelta(hours=1),
              "A1", HOUR),
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.integrity_alerts) == 1


def test_missing_observation_is_integrity_alert():
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E-GONE"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, [])
    assert len(result.integrity_alerts) == 1
    assert "no longer found" in result.integrity_alerts[0].explanation


def test_empty_contributing_event_ids_is_integrity_alert():
    """No provenance recorded at all (neither ticket nor capsule id) —
    conservatively alerts rather than silently passing."""
    prior = [_cell("A1", HOUR, CellValid.invalid, [])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, [])
    assert len(result.integrity_alerts) == 1
    assert "no contributing ticket or capsule" in result.integrity_alerts[0].explanation


# ---------------------------------------------------------------------------
# Unticketed capsule provenance (Ryan, 2026-07-02 — closes the flagged gap
# for the stable-detection case)
# ---------------------------------------------------------------------------

def test_unticketed_capsule_unchanged_two_nights_running_is_silent():
    """REQUIRED: an hour invalid purely from a live capsule two nights
    running, no Events rows at all -> silent, not INTEGRITY ALERT. The raw
    detection is identical (same analyzer+class+start+end) on both pulls —
    nothing new to approve."""
    capsule = Capsule("A1", "status-offline", HOUR, HOUR + timedelta(hours=1))
    cap_id = capsule_provenance_id(capsule)
    prior = [_cell("A1", HOUR, CellValid.invalid, [cap_id])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = diff_grids(prior, current, [], RUN_DATE, 7, TZ, current_capsules=[capsule])
    assert len(result.silent_passes) == 1
    assert result.integrity_alerts == result.machine_informational == []


def test_unticketed_capsule_gone_still_integrity_alert():
    """A capsule that's gone (or moved — same effect, different id) still
    has no Observation to check: falls to INTEGRITY ALERT exactly as
    before the capsule-provenance extension."""
    capsule = Capsule("A1", "status-offline", HOUR, HOUR + timedelta(hours=1))
    cap_id = capsule_provenance_id(capsule)
    prior = [_cell("A1", HOUR, CellValid.invalid, [cap_id])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = diff_grids(prior, current, [], RUN_DATE, 7, TZ, current_capsules=[])
    assert len(result.integrity_alerts) == 1
    assert result.silent_passes == []


def test_unticketed_capsule_moved_gets_a_different_id_and_still_alerts():
    capsule = Capsule("A1", "status-offline", HOUR, HOUR + timedelta(hours=1))
    cap_id = capsule_provenance_id(capsule)
    moved_capsule = Capsule("A1", "status-offline",
                            HOUR + timedelta(minutes=10), HOUR + timedelta(hours=1))
    prior = [_cell("A1", HOUR, CellValid.invalid, [cap_id])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = diff_grids(prior, current, [], RUN_DATE, 7, TZ, current_capsules=[moved_capsule])
    assert len(result.integrity_alerts) == 1


def test_mixed_capsule_and_ticket_contributors_both_must_resolve():
    """One contributor is a stable unticketed capsule (silent), the other
    is a properly approved ticket (silent) — the whole flip stays silent."""
    hour_end = HOUR + timedelta(hours=1)
    capsule = Capsule("A1", "status-offline", HOUR, hour_end)
    cap_id = capsule_provenance_id(capsule)
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("E2", EventType.Approval, "E1", HOUR, hour_end, "A1",
              HOUR + timedelta(minutes=10), actor="supervisor"),
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, [cap_id, "E1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = diff_grids(prior, current, events, RUN_DATE, 7, TZ, current_capsules=[capsule])
    assert len(result.silent_passes) == 1
    assert result.integrity_alerts == result.machine_informational == []


# ---------------------------------------------------------------------------
# Multiple contributing tickets on one flipped cell
# ---------------------------------------------------------------------------

def test_all_approved_tickets_is_silent_pass():
    hour_end = HOUR + timedelta(hours=1)
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("E2", EventType.Approval, "E1", HOUR, hour_end, "A1",
              HOUR + timedelta(minutes=10), actor="supervisor"),
        _event("F1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("F2", EventType.Approval, "F1", HOUR, hour_end, "A1",
              HOUR + timedelta(minutes=10), actor="supervisor"),
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1", "F1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.silent_passes) == 1
    assert result.machine_informational == result.integrity_alerts == []


def test_mixed_approval_and_machine_withdrawn_is_informational_not_silent():
    """One traces to Approval, the other to machine Withdrawn — the mix
    should surface in the digest (informational), not go fully silent."""
    hour_end = HOUR + timedelta(hours=1)
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("E2", EventType.Approval, "E1", HOUR, hour_end, "A1",
              HOUR + timedelta(minutes=10), actor="supervisor"),
        _event("F1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("F2", EventType.Withdrawn, "F1", HOUR, hour_end, "A1",
              HOUR + timedelta(days=1), actor="clerk-delta"),
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1", "F1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.machine_informational) == 1
    assert result.silent_passes == []


def test_one_unexplained_ticket_taints_the_whole_flip_to_alert():
    """Even if ONE of two contributing tickets is fully approved, an
    unexplained SECOND ticket means the overall flip is untraceable."""
    hour_end = HOUR + timedelta(hours=1)
    events = [
        _event("E1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        _event("E2", EventType.Approval, "E1", HOUR, hour_end, "A1",
              HOUR + timedelta(minutes=10), actor="supervisor"),
        _event("F1", EventType.SeeqDetection, None, HOUR, hour_end, "A1", HOUR),
        # F1 has no explaining event at all
    ]
    prior = [_cell("A1", HOUR, CellValid.invalid, ["E1", "F1"])]
    current = [_cell("A1", HOUR, CellValid.valid)]
    result = _diff(prior, current, events)
    assert len(result.integrity_alerts) == 1
    assert result.silent_passes == []


# ---------------------------------------------------------------------------
# Late arrival
# ---------------------------------------------------------------------------

def test_new_invalid_cell_older_than_threshold_pings():
    old_hour = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)  # 31 days before run_date
    prior = []
    current = [_cell("A1", old_hour, CellValid.invalid, ["E1"])]
    result = diff_grids([_cell("A1", old_hour, CellValid.valid)], current, [],
                        RUN_DATE, late_x_threshold_days=7, site_timezone=TZ)
    assert len(result.late_arrivals) == 1
    assert result.late_arrivals[0].days_old > 7


def test_new_invalid_cell_within_threshold_does_not_ping():
    recent_hour = RUN_DATE - timedelta(days=2)
    recent_dt = datetime(recent_hour.year, recent_hour.month, recent_hour.day, 8, 0,
                        tzinfo=timezone.utc)
    prior = [_cell("A1", recent_dt, CellValid.valid)]
    current = [_cell("A1", recent_dt, CellValid.invalid, ["E1"])]
    result = _diff(prior, current, [], late_x=7)
    assert result.late_arrivals == []


def test_late_arrival_independent_of_flip_tracing():
    """A late-arriving invalid cell doesn't need any flip explanation —
    it's simply new invalid time, no approval/withdrawal involved."""
    old_hour = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    prior = [_cell("A1", old_hour, CellValid.not_operating)]
    current = [_cell("A1", old_hour, CellValid.invalid, ["E1"])]
    result = _diff(prior, current, [], late_x=7)
    assert len(result.late_arrivals) == 1
    assert result.silent_passes == result.machine_informational == result.integrity_alerts == []


def test_late_arrival_exactly_at_threshold_does_not_ping():
    """LateXThresholdDays default 7 — the boundary itself is not late."""
    exact_hour = datetime(2026, 3, 25, 8, 0, tzinfo=timezone.utc)  # exactly 7 days before RUN_DATE
    prior = [_cell("A1", exact_hour, CellValid.valid)]
    current = [_cell("A1", exact_hour, CellValid.invalid, ["E1"])]
    result = _diff(prior, current, [], late_x=7)
    assert result.late_arrivals == []
