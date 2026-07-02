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
    build_grid,
    contributing_observation_windows,
)
from clerk.rules import HourContext, evaluate_hour
from clerk.schemas import (
    CellValid,
    Event,
    EventType,
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


def _obs(origin_id, status, start, end, analyzer="A1", signed=None):
    return Observation(
        origin_event_id=origin_id,
        status=status,
        extent_start_utc=start,
        extent_end_utc=end,
        signed_dismissal_extent=signed,
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
    """Fail-safe polarity: unadjudicated downtime counts immediately.
    Dismissal-pending has NOT been approved yet — it still counts."""
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

def _seeq_obs(origin_id, status, start, end, signed=None):
    return _obs(origin_id, status, start, end, analyzer="A1", signed=signed)


def test_dismissal_subtracted_when_capsule_still_matches():
    signed = (_utc(2026, 4, 1, 8, 0), _utc(2026, 4, 1, 12, 0))
    dismissed = _seeq_obs("E1", Status.dismissed, *signed, signed=signed)
    detected = {"A1": [signed]}
    origin_types = {"E1": EventType.SeeqDetection}
    capsules = {"A1": [signed]}  # exact match
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
        detected, [dismissed], origin_types, {"A1": [wide_capsule]}, jitter_minutes=5)
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
        detected, [dismissed], origin_types, {"A1": [near_capsule]}, jitter_minutes=5)
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
        detected, [tech_dismissed], origin_types, {"A1": [unrelated_capsule]}, jitter_minutes=5)
    assert out["A1"] == [unrelated_capsule], \
        "TechEntry-origin dismissal must not cancel unrelated detected time"


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
        _event("E1", EventType.SeeqDetection, None, hour, hour_end, "A1", hour),
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
        _event("E1", EventType.SeeqDetection, None, hour, signed_end, "A1", hour),
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
    assert len(cells) == 6 * 3  # 6 analyzers x 3 hours
    assert all(isinstance(c.Valid, CellValid) for c in cells)


def test_fixture_cems001_dismissed_matched_window_is_excused_from_detected_union():
    """CEMS-001's Jan 20 08:00-12:00 dismissal (E003/E004/E005) has an exact-
    matching capsule in capsules.csv — confirm the detected-window union no
    longer carries that interval after subtraction (regardless of what
    branch the uncovered analyzer ultimately routes through)."""
    from clerk.grid import (
        _capsules_by_analyzer,
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
        detected, observations, origin_types, capsules_by_analyzer, config.JitterToleranceMin)

    dismissed_window = (_utc(2026, 1, 20, 8, 0), _utc(2026, 1, 20, 12, 0))
    for s, e in detected.get("CEMS-001", []):
        assert not (s < dismissed_window[1] and dismissed_window[0] < e), \
            "the signed-dismissed, capsule-matched interval must be fully removed"
