"""
Steps 4-5 grid.py tests.

Section A is the REQUIRED test closing the fold.py handoff (Ryan,
2026-07-02): a Needs-review observation and a Confirmed observation with
identical extents must contribute IDENTICALLY to the union (Guarantee A —
no "only Confirmed counts" filter). A Dismissed observation with the same
extent contributes NOTHING. Same for Withdrawn and Superseded.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clerk.fold import Observation, Status
from clerk.grid import (
    apply_dismissal_subtraction,
    backdate_to_last_passing,
    build_grid,
    capsule_provenance_id,
    contributing_observation_windows,
    is_down_hour,
    source_down_hours,
)
from clerk.rules import HourContext, evaluate_hour
from clerk.schemas import (
    CellValid,
    Event,
    EventType,
    GridCell,
    read_analyzer_units,
    read_capsules,
    read_config,
    read_events,
    read_operating,
    read_qa_windows,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def _obs(origin_id, status, start, end, analyzer="A1", signed=None, detection_class=None):
    return Observation(
        origin_event_id=origin_id,
        status=status,
        extent_start_utc=start,
        extent_end_utc=end,
        signed_dismissal_extent=signed,
        detection_class=detection_class,
        has_corrective_action=False,
        analyzers=[analyzer],
        latest_acted_at=start,
        contributing_event_ids=[origin_id],
    )


# ---------------------------------------------------------------------------
# Section A — REQUIRED: Guarantee A union-contribution test
# ---------------------------------------------------------------------------

EXTENT = (_utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 10, 0))


def test_needs_review_and_confirmed_contribute_identically():
    needs_review = _obs("E1", Status.needs_review, *EXTENT)
    confirmed = _obs("E2", Status.confirmed, *EXTENT)
    result = contributing_observation_windows([needs_review, confirmed])
    assert result == {"A1": [EXTENT, EXTENT]}, \
        "Needs review must count exactly like Confirmed — identical contribution"


def test_dismissed_contributes_nothing():
    dismissed = _obs("E3", Status.dismissed, *EXTENT)
    result = contributing_observation_windows([dismissed])
    assert "A1" not in result, "Dismissed must contribute NOTHING to the union"


def test_withdrawn_contributes_nothing():
    withdrawn = _obs("E4", Status.withdrawn, *EXTENT)
    result = contributing_observation_windows([withdrawn])
    assert "A1" not in result, "Withdrawn must contribute NOTHING to the union"


def test_superseded_contributes_nothing():
    superseded = _obs("E5", Status.superseded, *EXTENT)
    result = contributing_observation_windows([superseded])
    assert "A1" not in result, "Superseded must contribute NOTHING to the union"


def test_dismissal_pending_still_contributes():
    """Guarantee A: dismissal-pending has NOT been approved — it still counts
    toward the union until a Dismissed or Approval event closes it."""
    pending = _obs("E6", Status.dismissal_pending, *EXTENT)
    result = contributing_observation_windows([pending])
    assert result == {"A1": [EXTENT]}


def test_mixed_set_only_excluded_statuses_are_dropped():
    """One test file, all five statuses together — the union contains
    exactly the two contributing intervals, nothing from the other three."""
    observations = [
        _obs("E1", Status.needs_review, *EXTENT),
        _obs("E2", Status.confirmed, *EXTENT),
        _obs("E3", Status.dismissed, *EXTENT),
        _obs("E4", Status.withdrawn, *EXTENT),
        _obs("E5", Status.superseded, *EXTENT),
    ]
    result = contributing_observation_windows(observations)
    assert result == {"A1": [EXTENT, EXTENT]}


# ---------------------------------------------------------------------------
# Section B — signed-dismissal subtraction
# ---------------------------------------------------------------------------

def _seeq_obs(origin_id, status, start, end, signed=None, detection_class="status-offline"):
    return _obs(origin_id, status, start, end, analyzer="A1", signed=signed,
               detection_class=detection_class)


def test_dismissal_subtracted_when_capsule_still_matches():
    signed = (_utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 12, 0))
    dismissed = _seeq_obs("E1", Status.dismissed, *signed, signed=signed)
    detected = {"A1": [signed]}
    origin_types = {"E1": EventType.SeeqDetection}
    capsules = {("A1", "status-offline"): [signed]}  # exact match, same class
    out = apply_dismissal_subtraction(detected, [dismissed], origin_types, capsules, jitter_minutes=5)
    assert out["A1"] == []


def test_dismissal_not_subtracted_without_a_live_capsule():
    signed = (_utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 12, 0))
    dismissed = _seeq_obs("E1", Status.dismissed, *signed, signed=signed)
    detected = {"A1": [signed]}
    origin_types = {"E1": EventType.SeeqDetection}
    out = apply_dismissal_subtraction(detected, [dismissed], origin_types, {}, jitter_minutes=5)
    assert out["A1"] == [signed], "no live capsule corroborates it — nothing subtracted"


def test_excess_beyond_signed_extent_stays_invalid():
    """The live capsule is WIDER than the signed extent — only the signed
    portion is removed; the excess on either side stays invalid."""
    signed = (_utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 12, 0))
    wide_capsule = (_utc(2026, 4, 1, 7, 0), _utc(2026, 4, 1, 13, 0))
    dismissed = _seeq_obs("E1", Status.dismissed, *signed, signed=signed)
    detected = {"A1": [wide_capsule]}
    origin_types = {"E1": EventType.SeeqDetection}
    out = apply_dismissal_subtraction(
        detected, [dismissed], origin_types, {("A1", "status-offline"): [wide_capsule]}, jitter_minutes=5)
    assert out["A1"] == [
        (_utc(2026, 4, 1, 7, 0), _utc(2026, 4, 1, 8, 0)),
        (_utc(2026, 4, 1, 12, 0), _utc(2026, 4, 1, 13, 0)),
    ]


def test_near_touch_within_jitter_still_matches():
    signed = (_utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 12, 0))
    near_capsule = (_utc(2026, 4, 1, 12, 3), _utc(2026, 4, 1, 14, 0))  # 3 min gap
    dismissed = _seeq_obs("E1", Status.dismissed, *signed, signed=signed)
    detected = {"A1": [signed, near_capsule]}
    origin_types = {"E1": EventType.SeeqDetection}
    out = apply_dismissal_subtraction(
        detected, [dismissed], origin_types, {("A1", "status-offline"): [near_capsule]}, jitter_minutes=5)
    assert out["A1"] == [near_capsule]


def test_tech_entry_origin_dismissal_never_subtracts():
    """A TechEntry-origin observation has no capsule counterpart. Even if
    dismissed, it must be excluded from the subtraction list entirely — it
    must not cancel an unrelated SeeqDetection ticket's capsule time at the
    same analyzer."""
    signed = (_utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 12, 0))
    tech_dismissed = _seeq_obs("E1", Status.dismissed, *signed, signed=signed)
    unrelated_capsule = (_utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 12, 0))
    detected = {"A1": [unrelated_capsule]}
    origin_types = {"E1": EventType.TechEntry}
    out = apply_dismissal_subtraction(
        detected, [tech_dismissed], origin_types,
        {("A1", "status-offline"): [unrelated_capsule]}, jitter_minutes=5)
    assert out["A1"] == [unrelated_capsule], \
        "TechEntry-origin dismissal must not cancel unrelated detected time"


def test_dismissal_does_not_cross_detection_classes():
    """Extends test_tech_entry_origin_dismissal_never_subtracts (Ryan,
    2026-07-02): two SeeqDetection observations, same analyzer, DIFFERENT
    DetectionClass, overlapping windows — a dismissal signed against one
    class must not subtract from the other class's live capsule, even
    though analyzer-only matching would have wrongly allowed it."""
    from clerk.grid import _manual_and_detected_windows

    window = (_utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 12, 0))
    dismissed_offline = _seeq_obs("E1", Status.dismissed, *window, signed=window,
                                  detection_class="status-offline")
    live_validation = _seeq_obs("E2", Status.needs_review, *window,
                                detection_class="failed-daily-validation")
    origin_types = {"E1": EventType.SeeqDetection, "E2": EventType.SeeqDetection}

    _manual, detected = _manual_and_detected_windows(
        [dismissed_offline, live_validation], origin_types)
    assert detected["A1"] == [window], \
        "E1 is Dismissed and already excluded per Guarantee A — only E2's window remains"

    # Only the OTHER class has a live capsule; the dismissed class has none.
    capsules_by_class = {("A1", "failed-daily-validation"): [window]}
    out = apply_dismissal_subtraction(
        detected, [dismissed_offline, live_validation], origin_types,
        capsules_by_class, jitter_minutes=5)
    assert out["A1"] == [window], \
        "a status-offline dismissal must not be corroborated by a failed-daily-validation capsule"


# ---------------------------------------------------------------------------
# Section C — build_grid end-to-end scenarios (constructed, not the fixture)
# ---------------------------------------------------------------------------

def _config():
    return read_config(FIXTURES / "config.csv")


def _event(id, event_type, target, start, end, analyzer, acted_at, detection_class=""):
    return Event(
        EventID=id, EventType=event_type, TargetEventID=target,
        ExtentStartUTC=start, ExtentEndUTC=end, AnalyzerCEMIDs=[analyzer],
        Category="", ReasonCode="", Actor="test", ActedAt=acted_at,
        Reason="", CorrectiveAction="", DetectionClass=detection_class,
    )


from clerk.schemas import AnalyzerUnit, OperatingWindow, QAWindow, Capsule


def test_clean_covered_full_hour_is_valid():
    hour = _utc(2026, 4, 1, 8, 0)
    cells = build_grid(
        events=[], capsules=[],
        operating_windows=[OperatingWindow("U1", hour, hour + timedelta(hours=2))],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour + timedelta(hours=1),
    )
    assert len(cells) == 1
    assert cells[0].Valid is CellValid.valid
    assert cells[0].RuleApplied == "(i)"
    assert cells[0].OperatingFraction == 1.0


def test_not_operating_hour():
    hour = _utc(2026, 4, 1, 8, 0)
    cells = build_grid(
        events=[], capsules=[],
        operating_windows=[OperatingWindow("U1", hour + timedelta(hours=5), hour + timedelta(hours=6))],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour + timedelta(hours=1),
    )
    assert cells[0].Valid is CellValid.not_operating
    assert cells[0].OperatingFraction == 0.0


def test_uncovered_analyzer_clean_hour_is_not_assessed():
    hour = _utc(2026, 4, 1, 8, 0)
    cells = build_grid(
        events=[], capsules=[],
        operating_windows=[OperatingWindow("U1", hour, hour + timedelta(hours=1))],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=False)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour + timedelta(hours=1),
    )
    assert cells[0].Valid is CellValid.not_assessed


def test_tech_entry_window_triggers_iii_a_regardless_of_coverage():
    hour = _utc(2026, 4, 1, 8, 0)
    events = [_event("E1", EventType.TechEntry, None, hour, hour + timedelta(hours=1),
                     "A1", hour)]
    cells = build_grid(
        events=events, capsules=[],
        operating_windows=[OperatingWindow("U1", hour, hour + timedelta(hours=1))],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=False)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour + timedelta(hours=1),
    )
    assert cells[0].RuleApplied == "(iii)(A)"
    assert cells[0].Valid is CellValid.invalid  # fully consumed
    assert cells[0].ContributingEventIDs == ["E1"]


def test_tech_entry_branch_4_eligibility_is_status_independent():
    """Guarantee A's status-independence principle (proven for union-
    contribution) must carry over to branch selection identically: branch 4
    eligibility depends ONLY on origin type (TechEntry), never on status.
    Two TechEntry-origin observations, identical extent, one Needs review
    and one Confirmed, on an uncovered analyzer — both must select branch 4
    and produce the SAME Valid verdict; neither may reach NOT-ASSESSED.
    (SeeqCovered is irrelevant here: branch 4 evaluates purely against V,
    which a TechEntry-origin observation populates on its own — it never
    needs Seeq's quadrant signal.)"""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    # Partial window so the verdict itself (not just RuleApplied) is a
    # meaningful check: V = :20-:60 = 40 min >= 15 -> valid under (iii)(A).
    needs_review = _event("E1", EventType.TechEntry, None, hour, hour + timedelta(minutes=20),
                          "A1", hour, detection_class="")

    def _confirmed_via_corrective_action(id, analyzer):
        return Event(
            EventID=id, EventType=EventType.TechEntry, TargetEventID=None,
            ExtentStartUTC=hour, ExtentEndUTC=hour + timedelta(minutes=20),
            AnalyzerCEMIDs=[analyzer], Category="", ReasonCode="", Actor="tech",
            ActedAt=hour, Reason="", CorrectiveAction="fixed on site", DetectionClass="",
        )

    events = [needs_review, _confirmed_via_corrective_action("E2", "A2")]
    cells = build_grid(
        events=events, capsules=[],
        operating_windows=[OperatingWindow("U1", hour, hour_end),
                           OperatingWindow("U2", hour, hour_end)],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=False),
                        AnalyzerUnit("A2", "U2", SeeqCovered=False)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    by_analyzer = {c.Analyzer: c for c in cells}
    assert by_analyzer["A1"].RuleApplied == "(iii)(A)"
    assert by_analyzer["A2"].RuleApplied == "(iii)(A)"
    assert by_analyzer["A1"].Valid == by_analyzer["A2"].Valid == CellValid.valid
    assert CellValid.not_assessed not in (by_analyzer["A1"].Valid, by_analyzer["A2"].Valid)


def test_seeq_detection_window_does_not_trigger_iii_a_routes_to_normal():
    """A detected (not manual) invalid window doesn't trigger branch 4 — it
    subtracts from V in the normal branch instead."""
    hour = _utc(2026, 4, 1, 8, 0)
    events = [_event("E1", EventType.SeeqDetection, None, hour, hour + timedelta(minutes=20),
                     "A1", hour)]
    cells = build_grid(
        events=events, capsules=[],
        operating_windows=[OperatingWindow("U1", hour, hour + timedelta(hours=1))],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour + timedelta(hours=1),
    )
    assert cells[0].RuleApplied == "(i)"
    assert cells[0].Valid is CellValid.invalid  # Q1 consumed
    assert cells[0].ContributingEventIDs == ["E1"]


def test_qa_window_triggers_iii_a():
    hour = _utc(2026, 4, 1, 8, 0)
    cells = build_grid(
        events=[], capsules=[],
        operating_windows=[OperatingWindow("U1", hour, hour + timedelta(hours=1))],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[QAWindow("A1", hour, hour + timedelta(minutes=20), "CGA-TEST")],
        config=_config(),
        window_start=hour, window_end=hour + timedelta(hours=1),
    )
    assert cells[0].RuleApplied == "(iii)(A)"
    assert cells[0].Valid is CellValid.valid  # V spans 40 min


def test_dismissal_cancels_detected_window_and_restores_valid():
    """End-to-end: a SeeqDetection ticket approved with a signed extent, a
    still-matching capsule -> the detected window is cancelled, the hour is
    valid under the normal branch (no manual/QA window ever existed)."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    events = [
        _event("E1", EventType.SeeqDetection, None, hour, hour_end, "A1", hour,
              detection_class="status-offline"),
        _event("E2", EventType.DismissalProposed, "E1", hour, hour_end, "A1",
               hour + timedelta(minutes=10)),
        _event("E3", EventType.Approval, "E1", hour, hour_end, "A1",
               hour + timedelta(minutes=20)),
    ]
    capsules = [Capsule("A1", "status-offline", hour, hour_end)]
    cells = build_grid(
        events=events, capsules=capsules,
        operating_windows=[OperatingWindow("U1", hour, hour_end)],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    assert cells[0].Valid is CellValid.valid
    assert cells[0].RuleApplied == "(i)"


def test_dismissal_with_no_live_capsule_at_all_leaves_nothing_to_excuse():
    """No capsule anywhere: the Dismissed ticket already contributes nothing
    via the folded-window path (Guarantee A exclusion), and there is no
    capsule to seed the detected-window union either. Nothing invalid was
    ever asserted for this hour, so it is legitimately valid — the
    subtraction step has nothing to do, and correctly does nothing."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    events = [
        _event("E1", EventType.SeeqDetection, None, hour, hour_end, "A1", hour),
        _event("E2", EventType.DismissalProposed, "E1", hour, hour_end, "A1",
               hour + timedelta(minutes=10)),
        _event("E3", EventType.Approval, "E1", hour, hour_end, "A1",
               hour + timedelta(minutes=20)),
    ]
    cells = build_grid(
        events=events, capsules=[],  # no live capsule at all
        operating_windows=[OperatingWindow("U1", hour, hour_end)],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    assert cells[0].Valid is CellValid.valid


def test_dismissal_with_unrelated_non_matching_capsule_stays_invalid():
    """The signed dismissal covers 8:00-8:30. A DIFFERENT, unrelated live
    capsule sits at 8:45-9:00 — too far away to jitter-match (15 min gap
    against a 5 min tolerance) — so it is never subtracted and correctly
    leaves the hour invalid, via Q4 being fully consumed."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    signed_end = hour + timedelta(minutes=30)
    events = [
        _event("E1", EventType.SeeqDetection, None, hour, signed_end, "A1", hour,
              detection_class="status-offline"),
        _event("E2", EventType.DismissalProposed, "E1", hour, signed_end, "A1",
               hour + timedelta(minutes=10)),
        _event("E3", EventType.Approval, "E1", hour, signed_end, "A1",
               hour + timedelta(minutes=20)),
    ]
    unrelated_capsule = Capsule("A1", "status-offline",
                                hour + timedelta(minutes=45), hour_end)
    cells = build_grid(
        events=events, capsules=[unrelated_capsule],
        operating_windows=[OperatingWindow("U1", hour, hour_end)],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    assert cells[0].Valid is CellValid.invalid
    assert cells[0].RuleApplied == "(i)"


def test_multiple_hours_and_analyzers_iterate_independently():
    hour = _utc(2026, 4, 1, 8, 0)
    cells = build_grid(
        events=[], capsules=[],
        operating_windows=[
            OperatingWindow("U1", hour, hour + timedelta(hours=3)),
            OperatingWindow("U2", hour, hour + timedelta(hours=3)),
        ],
        analyzer_units=[
            AnalyzerUnit("A1", "U1", SeeqCovered=True),
            AnalyzerUnit("A2", "U2", SeeqCovered=False),
        ],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour + timedelta(hours=3),
    )
    assert len(cells) == 6  # 2 analyzers x 3 hours
    a1_valids = {c.Valid for c in cells if c.Analyzer == "A1"}
    a2_valids = {c.Valid for c in cells if c.Analyzer == "A2"}
    assert a1_valids == {CellValid.valid}
    assert a2_valids == {CellValid.not_assessed}


def test_local_label_reflects_site_timezone():
    hour = _utc(2026, 4, 1, 12, 0)  # 08:00 EDT (spring, UTC-4)
    cells = build_grid(
        events=[], capsules=[],
        operating_windows=[OperatingWindow("U1", hour, hour + timedelta(hours=1))],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour + timedelta(hours=1),
    )
    assert "08:00" in cells[0].HourLocalLabel
    assert "EDT" in cells[0].HourLocalLabel


# ---------------------------------------------------------------------------
# Capsule provenance (Ryan, 2026-07-02 — closes diff.py's flagged gap)
# ---------------------------------------------------------------------------

def test_capsule_provenance_id_is_deterministic_and_identity_based():
    c1 = Capsule("A1", "status-offline", _utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 9, 0))
    c2 = Capsule("A1", "status-offline", _utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 9, 0))
    moved = Capsule("A1", "status-offline", _utc(2026, 4, 1, 8, 5), _utc(2026, 4, 1, 9, 0))
    other_class = Capsule("A1", "failed-daily-validation",
                          _utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 9, 0))
    assert capsule_provenance_id(c1) == capsule_provenance_id(c2)
    assert capsule_provenance_id(c1) != capsule_provenance_id(moved)
    assert capsule_provenance_id(c1) != capsule_provenance_id(other_class)
    assert capsule_provenance_id(c1).startswith("CAP:")


def test_unticketed_capsule_appears_in_contributing_event_ids():
    """An hour invalid purely from a live capsule (no Events at all) still
    carries provenance — the whole point of the capsule-id extension."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    capsule = Capsule("A1", "status-offline", hour, hour_end)
    cells = build_grid(
        events=[], capsules=[capsule],
        operating_windows=[OperatingWindow("U1", hour, hour_end)],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    assert cells[0].Valid is CellValid.invalid
    assert cells[0].ContributingEventIDs == [capsule_provenance_id(capsule)]


def test_ticketed_capsule_shows_both_ticket_and_capsule_provenance():
    """When a ticket AND its originating capsule both cover the hour,
    ContributingEventIDs cites both — more transparency, not deduped."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    events = [_event("E1", EventType.SeeqDetection, None, hour, hour_end, "A1", hour,
                     detection_class="status-offline")]
    capsule = Capsule("A1", "status-offline", hour, hour_end)
    cells = build_grid(
        events=events, capsules=[capsule],
        operating_windows=[OperatingWindow("U1", hour, hour_end)],
        analyzer_units=[AnalyzerUnit("A1", "U1", SeeqCovered=True)],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    assert set(cells[0].ContributingEventIDs) == {"E1", capsule_provenance_id(capsule)}


# ---------------------------------------------------------------------------
# Section D — integration smoke test against the real fixture set
# ---------------------------------------------------------------------------

def test_fixture_set_builds_without_error():
    events = read_events(FIXTURES / "events.csv")
    capsules = read_capsules(FIXTURES / "capsules.csv")
    operating = read_operating(FIXTURES / "operating.csv")
    analyzer_units = read_analyzer_units(FIXTURES / "analyzer_units.csv")
    qa_windows = read_qa_windows(FIXTURES / "qa_windows.csv")
    config = read_config(FIXTURES / "config.csv")

    window_start = _utc(2026, 1, 1, 0, 0)
    window_end = _utc(2026, 1, 1, 3, 0)
    cells = build_grid(events, capsules, operating, analyzer_units, qa_windows,
                       config, window_start, window_end)
    assert len(cells) == 8 * 3  # 8 analyzers (F5 added the O2 pair) x 3 hours
    assert all(isinstance(c.Valid, CellValid) for c in cells)


def test_fixture_cems001_dismissed_matched_window_is_excused_from_detected_union():
    """CEMS-001's Jan 20 08:00-12:00 dismissal (E003/E004/E005) has an exact-
    matching capsule in capsules.csv — confirm the detected-window union no
    longer carries that interval after subtraction (regardless of what
    branch the uncovered analyzer ultimately routes through)."""
    from clerk.grid import (
        _capsules_by_analyzer,
        _capsules_by_analyzer_class,
        _manual_and_detected_windows,
        _origin_types,
    )
    events = read_events(FIXTURES / "events.csv")
    capsules = read_capsules(FIXTURES / "capsules.csv")
    config = read_config(FIXTURES / "config.csv")

    from clerk.fold import fold as fold_fn
    observations = fold_fn(events)
    origin_types = _origin_types(events)
    _manual, detected = _manual_and_detected_windows(observations, origin_types)
    capsules_by_analyzer = _capsules_by_analyzer(capsules)
    for analyzer, intervals in capsules_by_analyzer.items():
        detected.setdefault(analyzer, []).extend(intervals)
    detected = apply_dismissal_subtraction(
        detected, observations, origin_types, _capsules_by_analyzer_class(capsules),
        config.JitterToleranceMin)

    dismissed_window = (_utc(2026, 1, 20, 8, 0), _utc(2026, 1, 20, 12, 0))
    for s, e in detected.get("CEMS-001", []):
        assert not (s < dismissed_window[1] and dismissed_window[0] < e), \
            "the signed-dismissed, capsule-matched interval must be fully removed"


# ---------------------------------------------------------------------------
# W7 — is_down_hour: only CellValid.invalid counts as a compliance down-hour
# ---------------------------------------------------------------------------

def _make_cell(valid: CellValid) -> GridCell:
    return GridCell(
        Analyzer="A1",
        HourStartUTC=_utc(2026, 1, 15, 8),
        HourLocalLabel="",
        OperatingFraction=1.0,
        Valid=valid,
        RuleApplied="(i)",
        ContributingEventIDs=[],
    )


def test_invalid_cell_is_down_hour():
    assert is_down_hour(_make_cell(CellValid.invalid)) is True


def test_valid_cell_is_not_down_hour():
    assert is_down_hour(_make_cell(CellValid.valid)) is False


def test_not_operating_cell_is_not_down_hour():
    """A unit not running is excluded from the DAR denominator — not a deficiency."""
    assert is_down_hour(_make_cell(CellValid.not_operating)) is False


def test_not_assessed_cell_is_not_down_hour():
    """Detection coverage absent — an open question, not a confirmed down-hour."""
    assert is_down_hour(_make_cell(CellValid.not_assessed)) is False


# ---------------------------------------------------------------------------
# W6 — backdate_to_last_passing (pure function, synthetic grid)
# ---------------------------------------------------------------------------

def _cell(analyzer, hour_offset_h, valid: CellValid) -> GridCell:
    base = _utc(2026, 1, 15, 0)
    hour = base + timedelta(hours=hour_offset_h)
    return GridCell(
        Analyzer=analyzer,
        HourStartUTC=hour,
        HourLocalLabel="",
        OperatingFraction=1.0,
        Valid=valid,
        RuleApplied="(i)",
        ContributingEventIDs=[],
    )


def test_backdate_returns_most_recent_valid_hour():
    grid = [
        _cell("A1", 0, CellValid.valid),
        _cell("A1", 1, CellValid.valid),
        _cell("A1", 2, CellValid.invalid),
        _cell("A1", 3, CellValid.invalid),
    ]
    result = backdate_to_last_passing("A1", grid)
    assert result == _utc(2026, 1, 15, 1), "must return the latest valid hour, not the first"


def test_backdate_returns_none_when_no_valid_cell():
    grid = [
        _cell("A1", 0, CellValid.invalid),
        _cell("A1", 1, CellValid.not_operating),
    ]
    assert backdate_to_last_passing("A1", grid) is None


def test_backdate_returns_none_for_unknown_analyzer():
    grid = [_cell("A1", 0, CellValid.valid)]
    assert backdate_to_last_passing("A2", grid) is None


def test_backdate_skips_other_analyzers():
    grid = [
        _cell("A1", 0, CellValid.valid),
        _cell("A1", 1, CellValid.valid),
        _cell("A2", 5, CellValid.valid),   # later hour, different analyzer
    ]
    result = backdate_to_last_passing("A1", grid)
    assert result == _utc(2026, 1, 15, 1), "must not include cells from other analyzers"


def test_backdate_not_operating_does_not_count_as_passing():
    grid = [
        _cell("A1", 0, CellValid.valid),
        _cell("A1", 1, CellValid.not_operating),
        _cell("A1", 2, CellValid.not_assessed),
    ]
    result = backdate_to_last_passing("A1", grid)
    assert result == _utc(2026, 1, 15, 0), "only CellValid.valid counts as passing"


def test_backdate_empty_grid_returns_none():
    assert backdate_to_last_passing("A1", []) is None


# ---------------------------------------------------------------------------
# W9 — diluent propagation: detected-invalid on diluent propagates to dependents
# ---------------------------------------------------------------------------

def test_diluent_downtime_propagates_to_dependent_analyzer():
    """When a diluent monitor (CO2) has a detected-invalid capsule, its
    dependent pollutant analyzer must also read invalid — even though the
    pollutant analyzer itself has no capsule or event for that hour."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    diluent_capsule = Capsule("CO2MON", "status-offline", hour, hour_end)
    cells = build_grid(
        events=[], capsules=[diluent_capsule],
        operating_windows=[
            OperatingWindow("U1", hour, hour_end),
            OperatingWindow("U2", hour, hour_end),
        ],
        analyzer_units=[
            AnalyzerUnit("CO2MON", "U2", SeeqCovered=True, DiluentsRole="diluent", DiluentSpecies="CO2", DiluentBasis=""),
            AnalyzerUnit("SO2MON", "U1", SeeqCovered=True, DiluentsRole="diluent-corrected", DiluentSpecies="CO2", DiluentBasis="CO2MON"),
        ],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    by_analyzer = {c.Analyzer: c for c in cells}
    assert by_analyzer["CO2MON"].Valid is CellValid.invalid, "diluent monitor itself is invalid"
    assert by_analyzer["SO2MON"].Valid is CellValid.invalid, "propagated diluent outage makes dependent invalid"


def test_clean_diluent_does_not_affect_dependent():
    """A diluent monitor with no capsule/events (clean) must not flip the
    dependent analyzer — clean diluent means no propagation."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    cells = build_grid(
        events=[], capsules=[],  # no diluent downtime
        operating_windows=[
            OperatingWindow("U1", hour, hour_end),
            OperatingWindow("U2", hour, hour_end),
        ],
        analyzer_units=[
            AnalyzerUnit("CO2MON", "U2", SeeqCovered=True, DiluentsRole="diluent", DiluentSpecies="CO2", DiluentBasis=""),
            AnalyzerUnit("SO2MON", "U1", SeeqCovered=True, DiluentsRole="diluent-corrected", DiluentSpecies="CO2", DiluentBasis="CO2MON"),
        ],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    by_analyzer = {c.Analyzer: c for c in cells}
    assert by_analyzer["CO2MON"].Valid is CellValid.valid
    assert by_analyzer["SO2MON"].Valid is CellValid.valid, "no diluent outage → dependent stays valid"


def test_analyzer_without_diluent_basis_unaffected_by_diluent_downtime():
    """An analyzer with no DiluentBasis must not pick up a diluent monitor's
    downtime — propagation is opt-in via DiluentBasis field only."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    diluent_capsule = Capsule("CO2MON", "status-offline", hour, hour_end)
    cells = build_grid(
        events=[], capsules=[diluent_capsule],
        operating_windows=[
            OperatingWindow("U1", hour, hour_end),
            OperatingWindow("U2", hour, hour_end),
        ],
        analyzer_units=[
            AnalyzerUnit("CO2MON", "U2", SeeqCovered=True,  DiluentsRole="diluent", DiluentSpecies="CO2", DiluentBasis=""),
            AnalyzerUnit("NOXMON", "U1", SeeqCovered=True,  DiluentsRole="not-diluent-corrected", DiluentBasis=""),
        ],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    by_analyzer = {c.Analyzer: c for c in cells}
    assert by_analyzer["NOXMON"].Valid is CellValid.valid, \
        "NOXMON has no DiluentBasis — must not be affected by CO2MON downtime"


def test_diluent_and_own_downtime_are_unioned():
    """An analyzer with both its own capsule downtime AND a diluent outage
    must be invalid for the union of both windows. This test uses a partial-
    hour diluent capsule to confirm the union semantics (not just override)."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    # Diluent down for first 20 min; pollutant down for last 20 min.
    # Together they consume 40 min of Q1+Q4, which under (i) leaves ≤20 min valid.
    diluent_capsule = Capsule("CO2MON", "status-offline",
                              hour, hour + timedelta(minutes=20))
    own_capsule     = Capsule("SO2MON", "status-offline",
                              hour + timedelta(minutes=40), hour_end)
    cells = build_grid(
        events=[], capsules=[diluent_capsule, own_capsule],
        operating_windows=[
            OperatingWindow("U1", hour, hour_end),
            OperatingWindow("U2", hour, hour_end),
        ],
        analyzer_units=[
            AnalyzerUnit("CO2MON", "U2", SeeqCovered=True, DiluentsRole="diluent", DiluentSpecies="CO2", DiluentBasis=""),
            AnalyzerUnit("SO2MON", "U1", SeeqCovered=True, DiluentsRole="diluent-corrected", DiluentSpecies="CO2", DiluentBasis="CO2MON"),
        ],
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    by_analyzer = {c.Analyzer: c for c in cells}
    # SO2MON: Q1 (0-15) consumed by diluent outage, Q4 (45-60) consumed by own
    # outage -> neither Q1 nor Q4 has valid data -> invalid under branch (i)
    assert by_analyzer["SO2MON"].Valid is CellValid.invalid


# ---------------------------------------------------------------------------
# W10 — source_down_hours: intersection (AND) of all analyzer downtimes per unit
# ---------------------------------------------------------------------------

def _make_cell_full(analyzer, hour, valid: CellValid) -> GridCell:
    return GridCell(
        Analyzer=analyzer,
        HourStartUTC=hour,
        HourLocalLabel="",
        OperatingFraction=1.0,
        Valid=valid,
        RuleApplied="(i)",
        ContributingEventIDs=[],
    )


def test_source_down_only_when_all_analyzers_invalid():
    """Source is down only when ALL its analyzers are simultaneously invalid."""
    hour = _utc(2026, 4, 1, 8, 0)
    units = [
        AnalyzerUnit("A1", "U1", SeeqCovered=True),
        AnalyzerUnit("A2", "U1", SeeqCovered=True),
    ]
    cells = [
        _make_cell_full("A1", hour, CellValid.invalid),
        _make_cell_full("A2", hour, CellValid.invalid),  # both down
    ]
    result = source_down_hours(units, cells)
    assert result == {"U1": [hour]}


def test_source_not_down_when_only_one_analyzer_invalid():
    """One analyzer invalid, one valid → NOT a source-down hour (AND gate)."""
    hour = _utc(2026, 4, 1, 8, 0)
    units = [
        AnalyzerUnit("A1", "U1", SeeqCovered=True),
        AnalyzerUnit("A2", "U1", SeeqCovered=True),
    ]
    cells = [
        _make_cell_full("A1", hour, CellValid.invalid),
        _make_cell_full("A2", hour, CellValid.valid),   # A2 still valid
    ]
    result = source_down_hours(units, cells)
    assert "U1" not in result, "partial downtime is not a source-down hour"


def test_not_operating_hours_excluded_from_source_down():
    """not_operating hours are outside the coverage gate — not source-down."""
    hour = _utc(2026, 4, 1, 8, 0)
    units = [
        AnalyzerUnit("A1", "U1", SeeqCovered=True),
        AnalyzerUnit("A2", "U1", SeeqCovered=True),
    ]
    cells = [
        _make_cell_full("A1", hour, CellValid.not_operating),
        _make_cell_full("A2", hour, CellValid.not_operating),
    ]
    result = source_down_hours(units, cells)
    assert result == {}, "unit not running — not a source-down hour"


def test_not_assessed_hours_excluded_from_source_down():
    """not_assessed on any analyzer: insufficient coverage → not source-down."""
    hour = _utc(2026, 4, 1, 8, 0)
    units = [
        AnalyzerUnit("A1", "U1", SeeqCovered=True),
        AnalyzerUnit("A2", "U1", SeeqCovered=False),
    ]
    cells = [
        _make_cell_full("A1", hour, CellValid.invalid),
        _make_cell_full("A2", hour, CellValid.not_assessed),
    ]
    result = source_down_hours(units, cells)
    assert result == {}, "not_assessed blocks source-down verdict"


def test_source_down_hours_multi_hour_intersection():
    """Only the hour where ALL analyzers are simultaneously invalid appears."""
    base = _utc(2026, 4, 1, 8, 0)
    h0, h1, h2 = base, base + timedelta(hours=1), base + timedelta(hours=2)
    units = [
        AnalyzerUnit("A1", "U1", SeeqCovered=True),
        AnalyzerUnit("A2", "U1", SeeqCovered=True),
    ]
    cells = [
        # h0: A1 invalid, A2 valid → NOT source-down
        _make_cell_full("A1", h0, CellValid.invalid),
        _make_cell_full("A2", h0, CellValid.valid),
        # h1: both invalid → source-down
        _make_cell_full("A1", h1, CellValid.invalid),
        _make_cell_full("A2", h1, CellValid.invalid),
        # h2: both valid → NOT source-down
        _make_cell_full("A1", h2, CellValid.valid),
        _make_cell_full("A2", h2, CellValid.valid),
    ]
    result = source_down_hours(units, cells)
    assert result == {"U1": [h1]}


def test_source_down_hours_multiple_units_independent():
    """Each unit's source-down hours are computed independently."""
    hour = _utc(2026, 4, 1, 8, 0)
    units = [
        AnalyzerUnit("A1", "U1", SeeqCovered=True),
        AnalyzerUnit("B1", "U2", SeeqCovered=True),
    ]
    cells = [
        _make_cell_full("A1", hour, CellValid.invalid),  # U1 only has one analyzer → down
        _make_cell_full("B1", hour, CellValid.valid),    # U2 only has one analyzer → valid
    ]
    result = source_down_hours(units, cells)
    assert "U1" in result and hour in result["U1"]
    assert "U2" not in result


def test_source_down_hours_returns_empty_when_no_downtime():
    hour = _utc(2026, 4, 1, 8, 0)
    units = [AnalyzerUnit("A1", "U1", SeeqCovered=True)]
    cells = [_make_cell_full("A1", hour, CellValid.valid)]
    assert source_down_hours(units, cells) == {}


# ---------------------------------------------------------------------------
# F2 — coverage-window gating on the source rollup
# ---------------------------------------------------------------------------

def _hourly_cells(analyzer, day_hours, invalid_hours):
    """Cells for the given clock hours on 2026-04-01; hours listed in
    invalid_hours are CellValid.invalid, the rest valid."""
    return [
        _make_cell_full(analyzer, _utc(2026, 4, 1, h),
                        CellValid.invalid if h in invalid_hours else CellValid.valid)
        for h in day_hours
    ]


def test_f2_fcc_full_coverage_intersection():
    """FCC full-coverage case: permanent down 10:00–14:00, temp down
    12:00–16:00, both in coverage across the whole window → source down is
    the intersection only: {12:00, 13:00}."""
    window = range(10, 16)
    units = [
        AnalyzerUnit("PERM", "FCC", SeeqCovered=True),
        AnalyzerUnit("TEMP", "FCC", SeeqCovered=True),
    ]
    cells = (_hourly_cells("PERM", window, invalid_hours={10, 11, 12, 13})
             + _hourly_cells("TEMP", window, invalid_hours={12, 13, 14, 15}))
    result = source_down_hours(units, cells)
    assert result == {"FCC": [_utc(2026, 4, 1, 12), _utc(2026, 4, 1, 13)]}


def test_f2_coverage_gating_temp_not_yet_in_service():
    """Coverage-gating case: same outages, but the temp's InServiceDate is
    12:00 — at 10:00–11:00 only the permanent is in coverage and it is down,
    so those hours ARE source-down. Result: {10:00, 11:00, 12:00, 13:00}."""
    window = range(10, 16)
    units = [
        AnalyzerUnit("PERM", "FCC", SeeqCovered=True),
        AnalyzerUnit("TEMP", "FCC", SeeqCovered=True,
                     InServiceDateUTC=_utc(2026, 4, 1, 12)),
    ]
    cells = (_hourly_cells("PERM", window, invalid_hours={10, 11, 12, 13})
             + _hourly_cells("TEMP", window, invalid_hours={12, 13, 14, 15}))
    result = source_down_hours(units, cells)
    assert result == {"FCC": [_utc(2026, 4, 1, h) for h in (10, 11, 12, 13)]}


def test_f2_oos_monitor_excluded_from_intersection():
    """The mirror of in-service gating: a temp already pulled (hour >=
    OOSDate) is absent — its stale 'valid' cells cannot veto source-down."""
    window = range(10, 14)
    units = [
        AnalyzerUnit("PERM", "FCC", SeeqCovered=True),
        AnalyzerUnit("TEMP", "FCC", SeeqCovered=True,
                     OOSDateUTC=_utc(2026, 4, 1, 12)),
    ]
    # PERM down 12-13; TEMP reads valid there but is out of coverage.
    cells = (_hourly_cells("PERM", window, invalid_hours={12, 13})
             + _hourly_cells("TEMP", window, invalid_hours=set()))
    result = source_down_hours(units, cells)
    assert result == {"FCC": [_utc(2026, 4, 1, 12), _utc(2026, 4, 1, 13)]}


def test_f2_no_in_coverage_monitor_means_no_source_down_claim():
    """At least one in-coverage monitor is required — an hour where every
    roster entry is out of coverage asserts nothing."""
    units = [
        AnalyzerUnit("TEMP", "FCC", SeeqCovered=True,
                     InServiceDateUTC=_utc(2026, 4, 1, 12)),
    ]
    cells = _hourly_cells("TEMP", range(10, 12), invalid_hours={10, 11})
    assert source_down_hours(units, cells) == {}


def test_f2_manual_only_temp_participates_in_rollup():
    """Manual-only temp: the temp's down window comes from a manual (List A)
    TechEntry with no capsule anywhere — it must still participate in the
    intersection exactly like a capsule-detected outage. Both monitors down
    the same hour → source-down."""
    hour = _utc(2026, 4, 1, 8, 0)
    hour_end = hour + timedelta(hours=1)
    events = [_event("E1", EventType.TechEntry, None, hour, hour_end, "TEMP", hour)]
    capsules = [Capsule("PERM", "status-offline", hour, hour_end)]
    units = [
        AnalyzerUnit("PERM", "FCC-U", SeeqCovered=True),
        AnalyzerUnit("TEMP", "FCC-U", SeeqCovered=True),
    ]
    cells = build_grid(
        events=events, capsules=capsules,
        operating_windows=[OperatingWindow("FCC-U", hour, hour_end)],
        analyzer_units=units,
        qa_windows=[], config=_config(),
        window_start=hour, window_end=hour_end,
    )
    by_analyzer = {c.Analyzer: c for c in cells}
    assert by_analyzer["TEMP"].Valid is CellValid.invalid, \
        "manual full-hour window -> (iii)(A) with empty V -> invalid"
    assert by_analyzer["PERM"].Valid is CellValid.invalid
    assert source_down_hours(units, cells) == {"FCC-U": [hour]}
