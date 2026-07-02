"""
Step 2 fold.py tests. list_c_recalc.py's own 8 tests are untouched and run
separately (test_list_c_recalc.py at repo root) — different schema, no
overlap; this file only exercises the Events-ledger fold.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clerk.fold import Observation, Status, fold
from clerk.schemas import Event, EventType, read_events

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def _event(id, event_type, target, start, end, analyzers, acted_at,
           corrective_action="", detection_class=""):
    return Event(
        EventID=id,
        EventType=event_type,
        TargetEventID=target,
        ExtentStartUTC=start,
        ExtentEndUTC=end,
        AnalyzerCEMIDs=analyzers if isinstance(analyzers, list) else [analyzers],
        Category="",
        ReasonCode="",
        Actor="test",
        ActedAt=acted_at,
        Reason="",
        CorrectiveAction=corrective_action,
        DetectionClass=detection_class,
    )


def _one(events):
    result = fold(events)
    assert len(result) == 1
    return result[0]


ORIGIN_START = _utc(2026, 3, 1, 8, 0)
ORIGIN_END = _utc(2026, 3, 1, 12, 0)


# ---------------------------------------------------------------------------
# Origin events
# ---------------------------------------------------------------------------

def test_seeq_detection_origin_is_needs_review():
    obs = _one([_event("E1", EventType.SeeqDetection, None,
                       ORIGIN_START, ORIGIN_END, "A1", _utc(2026, 3, 1, 8, 5))])
    assert obs.status is Status.needs_review
    assert obs.origin_event_id == "E1"
    assert (obs.extent_start_utc, obs.extent_end_utc) == (ORIGIN_START, ORIGIN_END)


def test_seeq_detection_origin_needs_review_even_with_corrective_action():
    """Unlike TechEntry, SeeqDetection's initial status is fixed — no
    corrective-action-based guess applies to it."""
    obs = _one([_event("E1", EventType.SeeqDetection, None,
                       ORIGIN_START, ORIGIN_END, "A1", _utc(2026, 3, 1, 8, 5),
                       corrective_action="somehow already present")])
    assert obs.status is Status.needs_review
    assert obs.has_corrective_action is True


def test_detection_class_sourced_from_origin_seeq_detection_event():
    """Added 2026-07-02 so grid.py can match dismissals by analyzer+class,
    not analyzer alone."""
    obs = _one([_event("E1", EventType.SeeqDetection, None,
                       ORIGIN_START, ORIGIN_END, "A1", _utc(2026, 3, 1, 8, 5),
                       detection_class="failed-daily-validation")])
    assert obs.detection_class == "failed-daily-validation"


def test_detection_class_is_none_for_tech_entry_origin():
    """Nullable — blank on TechEntry origins, since DetectionClass is only
    ever populated on machine-authored events."""
    obs = _one([_event("E1", EventType.TechEntry, None,
                       ORIGIN_START, ORIGIN_END, "A1", _utc(2026, 3, 1, 8, 30))])
    assert obs.detection_class is None


def test_detection_class_blank_string_becomes_none():
    """Blank -> None generally, not just for TechEntry — a SeeqDetection
    event with an empty DetectionClass column also yields None."""
    obs = _one([_event("E1", EventType.SeeqDetection, None,
                       ORIGIN_START, ORIGIN_END, "A1", _utc(2026, 3, 1, 8, 5),
                       detection_class="")])
    assert obs.detection_class is None


def test_detection_class_not_affected_by_later_events():
    """Only the ORIGIN event's DetectionClass is consulted — a later
    BoundaryUpdate (which also carries a DetectionClass per the
    machine-authored-events convention) does not override it."""
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
              "A1", _utc(2026, 3, 1, 8, 5), detection_class="status-offline"),
        _event("E2", EventType.BoundaryUpdate, "E1", ORIGIN_START,
              _utc(2026, 3, 1, 13, 0), "A1", _utc(2026, 3, 1, 9, 0),
              detection_class="status-offline"),
    ]
    obs = _one(events)
    assert obs.detection_class == "status-offline"


def test_tech_entry_blank_corrective_action_is_needs_review():
    """FLAGGED guess: single-form submit with no corrective action yet."""
    obs = _one([_event("E1", EventType.TechEntry, None,
                       ORIGIN_START, ORIGIN_END, "A1", _utc(2026, 3, 1, 8, 30))])
    assert obs.status is Status.needs_review
    assert obs.has_corrective_action is False


def test_tech_entry_with_corrective_action_is_confirmed():
    """FLAGGED guess: corrective action present at submission time."""
    obs = _one([_event("E1", EventType.TechEntry, None,
                       ORIGIN_START, ORIGIN_END, "A1", _utc(2026, 3, 1, 8, 30),
                       corrective_action="Repaired on site")])
    assert obs.status is Status.confirmed
    assert obs.has_corrective_action is True


# ---------------------------------------------------------------------------
# Explicit-effect event types
# ---------------------------------------------------------------------------

def test_confirmation_sets_confirmed_and_leaves_extent():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Confirmation, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.confirmed
    assert (obs.extent_start_utc, obs.extent_end_utc) == (ORIGIN_START, ORIGIN_END)


def test_dismissal_proposed_sets_dismissal_pending():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.DismissalProposed, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.dismissal_pending


def test_approval_sets_dismissed_and_records_its_own_extent_not_current():
    """ledger 17: the approval's stated extent, never the observation's
    current extent — even when the two differ (a BoundaryUpdate widened the
    extent AFTER the extent the approval is actually signing off)."""
    approved_extent_start = _utc(2026, 3, 1, 8, 0)
    approved_extent_end = _utc(2026, 3, 1, 12, 0)
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.DismissalProposed, "E1",
               approved_extent_start, approved_extent_end,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.BoundaryUpdate, "E1",
               ORIGIN_START, _utc(2026, 3, 1, 13, 0),
               "A1", _utc(2026, 3, 1, 10, 0)),
        _event("E4", EventType.Approval, "E1",
               approved_extent_start, approved_extent_end,
               "A1", _utc(2026, 3, 1, 11, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.dismissed
    assert obs.extent_end_utc == _utc(2026, 3, 1, 13, 0), \
        "current extent still reflects the BoundaryUpdate"
    assert obs.signed_dismissal_extent == (approved_extent_start, approved_extent_end), \
        "signed extent is the approval's own, not the widened current extent"


def test_dismissal_rejected_sets_needs_review():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.DismissalProposed, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.DismissalRejected, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 10, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.needs_review


def test_reopen_sets_needs_review():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Withdrawn, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.Reopen, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 2, 9, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.needs_review


def test_boundary_update_changes_extent_and_leaves_status():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.DismissalProposed, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.BoundaryUpdate, "E1",
               ORIGIN_START, _utc(2026, 3, 1, 14, 0),
               "A1", _utc(2026, 3, 1, 20, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.dismissal_pending, "BoundaryUpdate must not touch status"
    assert obs.extent_end_utc == _utc(2026, 3, 1, 14, 0)


def test_withdrawn_sets_withdrawn():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Withdrawn, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 2, 23, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.withdrawn


def test_superseded_sets_superseded():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Superseded, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 2, 23, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.superseded


def test_correction_changes_extent_and_leaves_status():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Confirmation, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.Correction, "E1",
               ORIGIN_START, _utc(2026, 3, 1, 11, 0),
               "A1", _utc(2026, 3, 1, 12, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.confirmed, "Correction must not touch status"
    assert obs.extent_end_utc == _utc(2026, 3, 1, 11, 0)


def test_correction_shrinks_extent_without_gating():
    """fold.py does NOT gate or classify reductive Corrections — it folds
    whatever's asserted, full stop. The unapproved-flip safety check lives
    in diff.py (Step 7), not here."""
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Correction, "E1",
               ORIGIN_START, _utc(2026, 3, 1, 9, 0),  # shortened from 12:00
               "A1", _utc(2026, 3, 1, 10, 0)),
    ]
    obs = _one(events)
    assert obs.extent_end_utc == _utc(2026, 3, 1, 9, 0)
    assert obs.status is Status.needs_review, "no gating, no status side-effect"


# ---------------------------------------------------------------------------
# Principles: replay honestly; status never filters output
# ---------------------------------------------------------------------------

def test_replay_honestly_withdrawn_after_confirmation_is_not_rejected():
    """Principle 1: fold does not re-validate upstream business rules.
    Whether a confirmed episode should withdraw is delta.py's job to
    enforce on write; fold just replays whatever the stream asserts."""
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Confirmation, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.Withdrawn, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 2, 23, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.withdrawn


def test_fold_returns_every_observation_regardless_of_status():
    """Principle 2 / Guarantee A: fold must never filter observations by
    status. Withdrawn/Dismissed/Superseded observations still come back —
    it's grid.py's job (Step 4) to decide what counts, not fold's to hide
    them. This guards against an 'only Confirmed counts' filter creeping in."""
    dismissed = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.DismissalProposed, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.Approval, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 10, 0)),
    ]
    withdrawn = [
        _event("E4", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A2", _utc(2026, 3, 1, 8, 5)),
        _event("E5", EventType.Withdrawn, "E4", ORIGIN_START, ORIGIN_END,
               "A2", _utc(2026, 3, 2, 23, 0)),
    ]
    superseded = [
        _event("E6", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A3", _utc(2026, 3, 1, 8, 5)),
        _event("E7", EventType.Superseded, "E6", ORIGIN_START, ORIGIN_END,
               "A3", _utc(2026, 3, 2, 23, 0)),
    ]
    result = fold(dismissed + withdrawn + superseded)
    statuses = {obs.origin_event_id: obs.status for obs in result}
    assert statuses == {
        "E1": Status.dismissed,
        "E4": Status.withdrawn,
        "E6": Status.superseded,
    }, "every observation must be present in the output, whatever its status"


def test_has_corrective_action_decoupled_from_status():
    """A Correction can carry CorrectiveAction text without becoming a
    status-changing event — has_corrective_action and status track
    independently."""
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Correction, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0),
               corrective_action="Root cause found; part replaced"),
    ]
    obs = _one(events)
    assert obs.has_corrective_action is True
    assert obs.status is Status.needs_review, "Correction still leaves status untouched"


# ---------------------------------------------------------------------------
# Flat grouping: TargetEventID always the origin, never a chain
# ---------------------------------------------------------------------------

def test_flat_targeting_through_multiple_dismissal_cycles():
    """Propose, reject, propose again, approve — every downstream event
    targets the ORIGIN directly (flat), never the specific prior
    DismissalProposed it conceptually responds to. All fold into ONE
    observation via flat grouping, not split by a target chain."""
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.DismissalProposed, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.DismissalRejected, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 10, 0)),
        _event("E4", EventType.DismissalProposed, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 11, 0)),
        _event("E5", EventType.Approval, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 12, 0)),
    ]
    obs = _one(events)
    assert obs.status is Status.dismissed
    assert obs.contributing_event_ids == ["E1", "E2", "E3", "E4", "E5"]


# ---------------------------------------------------------------------------
# Determinism: tiebreak, input-order independence, multi-observation isolation
# ---------------------------------------------------------------------------

def test_deterministic_tiebreak_same_acted_at_orders_by_event_id():
    """Two events sharing an ActedAt must apply in EventID order — E2
    (Confirmation) before E3 (Withdrawn) purely by ID, since ActedAt ties."""
    same_time = _utc(2026, 3, 1, 9, 0)
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E3", EventType.Withdrawn, "E1", ORIGIN_START, ORIGIN_END,
               "A1", same_time),
        _event("E2", EventType.Confirmation, "E1", ORIGIN_START, ORIGIN_END,
               "A1", same_time),
    ]
    obs = _one(events)
    # E2 < E3 lexicographically, so Confirmation applies before Withdrawn.
    assert obs.status is Status.withdrawn
    assert obs.contributing_event_ids == ["E1", "E2", "E3"]


def test_input_order_does_not_affect_result():
    """Late-arriving events simply change the fold: feeding the same event
    set in a shuffled order produces an identical result, since only
    (ActedAt, EventID) — not list position — determines replay order."""
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.DismissalProposed, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.Approval, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 10, 0)),
    ]
    forward = fold(events)
    reversed_ = fold(list(reversed(events)))
    shuffled = fold([events[1], events[2], events[0]])
    assert forward == reversed_ == shuffled


def test_multiple_observations_do_not_interfere():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Confirmation, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.TechEntry, None, ORIGIN_START, ORIGIN_END,
               "A2", _utc(2026, 3, 1, 10, 0)),
    ]
    result = {obs.origin_event_id: obs for obs in fold(events)}
    assert len(result) == 2
    assert result["E1"].status is Status.confirmed
    assert result["E3"].status is Status.needs_review
    assert result["E1"].analyzers == ["A1"]
    assert result["E3"].analyzers == ["A2"]


def test_contributing_event_ids_and_latest_acted_at():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.DismissalProposed, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 9, 0)),
        _event("E3", EventType.Approval, "E1", ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 10, 30)),
    ]
    obs = _one(events)
    assert obs.contributing_event_ids == ["E1", "E2", "E3"]
    assert obs.latest_acted_at == _utc(2026, 3, 1, 10, 30)


def test_analyzers_come_from_origin_only_not_widened_by_later_events():
    events = [
        _event("E1", EventType.SeeqDetection, None, ORIGIN_START, ORIGIN_END,
               "A1", _utc(2026, 3, 1, 8, 5)),
        _event("E2", EventType.Correction, "E1", ORIGIN_START, ORIGIN_END,
               ["A1", "A2"], _utc(2026, 3, 1, 9, 0)),
    ]
    obs = _one(events)
    assert obs.analyzers == ["A1"], \
        "Correction's broader CEMID list does not widen the observation's analyzers"


# ---------------------------------------------------------------------------
# Integration: the real events.csv fixture
# ---------------------------------------------------------------------------

def test_fixture_folds_without_error_and_covers_every_status():
    events = read_events(FIXTURES / "events.csv")
    result = fold(events)
    statuses = {obs.status for obs in result}
    assert statuses == {
        Status.confirmed, Status.dismissed, Status.withdrawn,
        Status.superseded, Status.needs_review,
    }, "the fixture must exercise every reachable status"


def test_fixture_e001_e002_confirmed():
    events = read_events(FIXTURES / "events.csv")
    obs = {o.origin_event_id: o for o in fold(events)}["E001"]
    assert obs.status is Status.confirmed


def test_fixture_e003_dismissed_with_own_signed_extent():
    """E005's Approval targets E003 (origin) directly, per the flat
    convention — this is the fixture correctness fix this ruling required."""
    events = read_events(FIXTURES / "events.csv")
    obs = {o.origin_event_id: o for o in fold(events)}["E003"]
    assert obs.status is Status.dismissed
    assert obs.signed_dismissal_extent == (
        _utc(2026, 1, 20, 8, 0), _utc(2026, 1, 20, 12, 0))
    assert obs.detection_class == "failed-daily-validation", \
        "fixture correctness fix (2026-07-02): E003 previously said " \
        "status-offline, mismatched against capsules.csv's actual " \
        "failed-daily-validation entry for the same window"


def test_fixture_e006_tech_entry_with_corrective_action_confirmed():
    events = read_events(FIXTURES / "events.csv")
    obs = {o.origin_event_id: o for o in fold(events)}["E006"]
    assert obs.status is Status.confirmed
    assert obs.has_corrective_action is True


def test_fixture_e010_rejected_then_boundary_updated():
    events = read_events(FIXTURES / "events.csv")
    obs = {o.origin_event_id: o for o in fold(events)}["E010"]
    assert obs.status is Status.needs_review, "DismissalRejected wins over the earlier propose"
    assert obs.extent_end_utc == _utc(2026, 2, 10, 10, 0), "BoundaryUpdate applied last"


def test_fixture_e014_withdrawn():
    events = read_events(FIXTURES / "events.csv")
    obs = {o.origin_event_id: o for o in fold(events)}["E014"]
    assert obs.status is Status.withdrawn


def test_fixture_e016_superseded():
    events = read_events(FIXTURES / "events.csv")
    obs = {o.origin_event_id: o for o in fold(events)}["E016"]
    assert obs.status is Status.superseded
