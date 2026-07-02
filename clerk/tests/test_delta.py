"""
Step 3a delta writer — unit tests (synthetic) + the real-data drift-pair
integration test (night 1 pre-fix → night 2 post-fix).

The drift pair is the concrete test for withdrawal-window scoping (spec open
item 7): Withdrawn may only be emitted for episodes fully INSIDE the run's
pull window.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clerk.delta import (
    DeltaResult,
    Episode,
    episodes_from_detections,
    run_delta,
)
from clerk.schemas import Capsule, EventType, PullWindow, read_capsules, read_pull_windows

FIXTURES = Path(__file__).parent.parent / "fixtures"
REAL = FIXTURES / "real"

RUN_AT = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _cap(analyzer, start, end, cls="legacy-blended"):
    return Capsule(Analyzer=analyzer, DetectionClass=cls,
                   CapsuleStartUTC=start, CapsuleEndUTC=end)


def _ep(eid, analyzer, start, end, cls="legacy-blended", **kw):
    return Episode(EpisodeID=eid, Analyzer=analyzer, DetectionClass=cls,
                   StartUTC=start, EndUTC=end, **kw)


WIDE_WINDOW = PullWindow(Night="w", PullStartUTC=_utc(2026, 1, 1),
                         PullEndUTC=_utc(2026, 12, 31))


# ---------------------------------------------------------------------------
# Unit tests — synthetic
# ---------------------------------------------------------------------------

def test_unmatched_capsule_births_needs_review_detection():
    r = run_delta([], [_cap("A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5))],
                  WIDE_WINDOW, RUN_AT)
    assert len(r.new_events) == 1
    e = r.new_events[0]
    assert e.EventType is EventType.SeeqDetection
    assert "Needs review" in e.Reason, "never born-dismissed"
    assert e.Category == "legacy-blended", "Category carries DetectionClass"


def test_exact_match_emits_nothing():
    ep = _ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5))
    r = run_delta([ep], [_cap("A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5))],
                  WIDE_WINDOW, RUN_AT)
    assert r.new_events == []


def test_class_is_a_matching_key_not_a_hint():
    """Same analyzer, same extent, different class → no match: new detection
    plus a (window-scoped) withdrawal, never a silent cross-class merge."""
    ep = _ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5), cls="status-offline")
    r = run_delta([ep], [_cap("A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5),
                              cls="failed-daily-validation")],
                  WIDE_WINDOW, RUN_AT)
    types = sorted(e.EventType for e in r.new_events)
    assert types == [EventType.SeeqDetection, EventType.Withdrawn]


def test_material_boundary_move_emits_boundary_update():
    ep = _ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5))
    r = run_delta([ep], [_cap("A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 6))],
                  WIDE_WINDOW, RUN_AT)
    assert len(r.new_events) == 1
    e = r.new_events[0]
    assert e.EventType is EventType.BoundaryUpdate
    assert e.TargetEventID == "E1"
    assert "2026-06-01T05:00" in e.Reason, "old extent must ride along"


def test_sub_jitter_movement_writes_nothing():
    ep = _ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5))
    r = run_delta([ep], [_cap("A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5, 4))],
                  WIDE_WINDOW, RUN_AT, jitter_tolerance_min=5)
    assert r.new_events == []


def test_vanished_unconfirmed_episode_inside_window_withdraws():
    ep = _ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5))
    r = run_delta([ep], [], WIDE_WINDOW, RUN_AT)
    assert len(r.new_events) == 1
    assert r.new_events[0].EventType is EventType.Withdrawn
    assert r.new_events[0].TargetEventID == "E1"
    assert r.new_events[0].Actor == "clerk-delta", "machine-attributed"


def test_vanished_episode_outside_window_is_untouched():
    ep = _ep("E1", "A1", _utc(2026, 6, 20, 3), _utc(2026, 6, 20, 5))
    narrow = PullWindow(Night="n", PullStartUTC=_utc(2026, 6, 1),
                        PullEndUTC=_utc(2026, 6, 12))
    r = run_delta([ep], [], narrow, RUN_AT)
    assert r.new_events == [], "absence outside the pull window is not evidence"


def test_episode_straddling_window_edge_is_not_withdrawn():
    """Fail-safe: partial pull coverage cannot prove the episode vanished."""
    ep = _ep("E1", "A1", _utc(2026, 6, 11, 22), _utc(2026, 6, 12, 6))
    narrow = PullWindow(Night="n", PullStartUTC=_utc(2026, 6, 1),
                        PullEndUTC=_utc(2026, 6, 12))
    r = run_delta([ep], [], narrow, RUN_AT)
    assert r.new_events == []


def test_confirmed_episode_never_withdraws():
    ep = _ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5), Confirmed=True)
    r = run_delta([ep], [], WIDE_WINDOW, RUN_AT)
    assert r.new_events == []
    assert any("CONFLICT" in f for f in r.flags), "conflict goes to the digest"


def test_dismissal_pending_withdrawal_flags_cancel_approval():
    ep = _ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5),
             DismissalPending=True)
    r = run_delta([ep], [], WIDE_WINDOW, RUN_AT)
    assert any(e.EventType is EventType.Withdrawn for e in r.new_events)
    assert any("CANCEL-APPROVAL" in f for f in r.flags)


def test_abutting_capsules_stay_two_tickets_and_are_flagged():
    """Strict overlap = two tickets until Ryan rules otherwise — flag, don't decide."""
    caps = [
        _cap("A1", _utc(2026, 6, 1, 0), _utc(2026, 6, 1, 12)),
        _cap("A1", _utc(2026, 6, 1, 12), _utc(2026, 6, 1, 17)),
    ]
    r = run_delta([], caps, WIDE_WINDOW, RUN_AT)
    detections = [e for e in r.new_events if e.EventType is EventType.SeeqDetection]
    assert len(detections) == 2, "abutment must NOT merge into one ticket"
    assert any("ABUTMENT" in f and "Ryan to rule" in f for f in r.flags)


def test_identical_inputs_twice_identical_output():
    """G1 discipline at the delta layer: pure function, deterministic IDs."""
    eps = [_ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5))]
    caps = [_cap("A1", _utc(2026, 6, 2, 3), _utc(2026, 6, 2, 5))]
    r1 = run_delta(eps, caps, WIDE_WINDOW, RUN_AT)
    r2 = run_delta(eps, caps, WIDE_WINDOW, RUN_AT)
    assert r1 == r2


# ---------------------------------------------------------------------------
# Integration — the real-data drift pair (night 1 pre-fix → night 2 post-fix)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def drift():
    night1 = read_capsules(REAL / "capsules_lubeflare_drift_night1.csv")
    night2 = read_capsules(REAL / "capsules_lubeflare_drift_night2.csv")
    windows = {w.Night: w for w in read_pull_windows(REAL / "drift_pull_windows.csv")}
    return night1, night2, windows


@pytest.fixture(scope="module")
def night2_result(drift):
    night1, night2, windows = drift
    ingest = run_delta([], night1, windows["1"], RUN_AT, id_prefix="N1")
    episodes = episodes_from_detections(ingest.new_events)
    assert len(episodes) == 15, "night 1 ingest must birth one episode per capsule"
    return episodes, run_delta(episodes, night2, windows["2"], RUN_AT, id_prefix="N2")


def test_drift_night2_withdraws_exactly_the_phantom(night2_result):
    episodes, r = night2_result
    withdrawn = [e for e in r.new_events if e.EventType is EventType.Withdrawn]
    assert len(withdrawn) == 2, "exactly the Jun 8–9 phantom, on both H2S channels"
    assert {e.AnalyzerCEMIDs[0] for e in withdrawn} == \
        {"LUBEFLR-H2S-PCT", "LUBEFLR-H2S-PPM"}
    for e in withdrawn:
        assert e.ExtentStartUTC == _utc(2026, 6, 9, 0)
        assert e.ExtentEndUTC == _utc(2026, 6, 9, 12)
        assert e.Actor == "clerk-delta", "machine-attributed → diff-informational"
        assert e.TargetEventID, "withdrawal binds to its originating ticket"


def test_drift_night2_exact_matches_emit_zero_events(night2_result):
    _, r = night2_result
    non_withdrawn = [e for e in r.new_events if e.EventType is not EventType.Withdrawn]
    assert non_withdrawn == [], "every surviving capsule exact-matches: zero events"


def test_drift_jun16_episodes_outside_window_not_withdrawn(night2_result):
    episodes, r = night2_result
    jun16_ids = {e.EpisodeID for e in episodes if e.StartUTC.day == 16}
    assert len(jun16_ids) == 6, "sanity: Jun 16 has six episodes across the channels"
    withdrawn_targets = {e.TargetEventID for e in r.new_events
                         if e.EventType is EventType.Withdrawn}
    assert withdrawn_targets.isdisjoint(jun16_ids), \
        "outside night 2's pull window — must NOT be withdrawn"


def test_drift_abutment_flagged_not_decided(night2_result):
    """The phantom abuts the surviving 12:00–17:00 fragment at exactly 12:00Z.
    Strict overlap withdraws fragment 1 while fragment 2 exact-matches — and
    the abutment is flagged for Ryan's ruling."""
    _, r = night2_result
    assert any("ABUTMENT" in f and "Ryan to rule" in f for f in r.flags)


def test_drift_night2_rerun_is_idempotent(night2_result):
    episodes, r = night2_result
    windows = {w.Night: w for w in read_pull_windows(REAL / "drift_pull_windows.csv")}
    night2 = read_capsules(REAL / "capsules_lubeflare_drift_night2.csv")
    again = run_delta(episodes, night2, windows["2"], RUN_AT, id_prefix="N2")
    assert again == r
