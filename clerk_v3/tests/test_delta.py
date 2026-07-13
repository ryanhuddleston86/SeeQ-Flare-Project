"""
Step 3a delta writer — unit tests (synthetic).

Covers: episode matching, withdrawal-window scoping, capsule coalescing,
confirmed-episode conflict handling, and determinism.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clerk.delta import (
    DeltaResult,
    Episode,
    coalesce_capsules,
    episodes_from_detections,
    run_delta,
)
from clerk.schemas import Capsule, EventType, PullWindow

FIXTURES = Path(__file__).parent.parent / "fixtures"

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
    assert e.DetectionClass == "legacy-blended", "dedicated column carries the class"
    assert e.Category == "", "Category is unused — not repurposed"


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
    """Partial pull coverage cannot prove the episode vanished."""
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


def test_abutting_capsules_coalesce_into_one_candidate():
    """Ryan's ruling 2026-07-02: zero-gap same-analyzer+class capsules merge
    at ingestion, before matching — one candidate episode for tonight's pull."""
    caps = [
        _cap("A1", _utc(2026, 6, 1, 0), _utc(2026, 6, 1, 12)),
        _cap("A1", _utc(2026, 6, 1, 12), _utc(2026, 6, 1, 17)),
    ]
    r = run_delta([], caps, WIDE_WINDOW, RUN_AT)
    detections = [e for e in r.new_events if e.EventType is EventType.SeeqDetection]
    assert len(detections) == 1
    assert detections[0].ExtentStartUTC == _utc(2026, 6, 1, 0)
    assert detections[0].ExtentEndUTC == _utc(2026, 6, 1, 17)


def test_coalesce_chain_gap_class_and_analyzer_boundaries():
    caps = [
        # three-link abutting chain → one candidate
        _cap("A1", _utc(2026, 6, 1, 0), _utc(2026, 6, 1, 2)),
        _cap("A1", _utc(2026, 6, 1, 2), _utc(2026, 6, 1, 4)),
        _cap("A1", _utc(2026, 6, 1, 4), _utc(2026, 6, 1, 6)),
        # any gap stays split
        _cap("A1", _utc(2026, 6, 1, 7), _utc(2026, 6, 1, 8)),
        # abutting but different class: never merged
        _cap("A1", _utc(2026, 6, 1, 8), _utc(2026, 6, 1, 9), cls="status-offline"),
        # abutting but different analyzer: never merged
        _cap("A2", _utc(2026, 6, 1, 9), _utc(2026, 6, 1, 10)),
    ]
    out = coalesce_capsules(caps)
    spans = [(c.Analyzer, c.DetectionClass, c.CapsuleStartUTC.hour, c.CapsuleEndUTC.hour)
             for c in out]
    assert spans == [
        ("A1", "legacy-blended", 0, 6),
        ("A1", "legacy-blended", 7, 8),
        ("A1", "status-offline", 8, 9),
        ("A2", "legacy-blended", 9, 10),
    ]


def test_coalesce_does_not_mutate_input():
    caps = [
        _cap("A1", _utc(2026, 6, 1, 0), _utc(2026, 6, 1, 2)),
        _cap("A1", _utc(2026, 6, 1, 2), _utc(2026, 6, 1, 4)),
    ]
    coalesce_capsules(caps)
    assert caps[0].CapsuleEndUTC == _utc(2026, 6, 1, 2)


def test_episode_capsule_abutment_across_history_still_flagged():
    """The ruling covers ingestion only. A tonight-capsule abutting a KNOWN
    episode remains strict no-match — flagged for Ryan, not decided."""
    ep = _ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5))
    r = run_delta([ep], [_cap("A1", _utc(2026, 6, 1, 5), _utc(2026, 6, 1, 7))],
                  WIDE_WINDOW, RUN_AT)
    types = sorted(e.EventType for e in r.new_events)
    assert types == [EventType.SeeqDetection, EventType.Withdrawn]
    assert any("ABUTMENT" in f and "Ryan to rule" in f for f in r.flags)


def test_identical_inputs_twice_identical_output():
    """G1 discipline at the delta layer: pure function, deterministic IDs."""
    eps = [_ep("E1", "A1", _utc(2026, 6, 1, 3), _utc(2026, 6, 1, 5))]
    caps = [_cap("A1", _utc(2026, 6, 2, 3), _utc(2026, 6, 2, 5))]
    r1 = run_delta(eps, caps, WIDE_WINDOW, RUN_AT)
    r2 = run_delta(eps, caps, WIDE_WINDOW, RUN_AT)
    assert r1 == r2


# ---------------------------------------------------------------------------
# F6 — synthetic re-cover of the removed real-data drift pair's COMPOSITION
#
# W2 removed six drift tests because they read fixtures/real/*.csv. Their
# individual behaviors (coalescing, boundary update, window scoping, exact
# match, idempotence) were already covered synthetically above — but the
# two-night COMPOSITION they proved end-to-end was not: night 1's abutting
# pair coalesces into ONE episode, then night 2's partial-overlap capsule
# trims it via a material BoundaryUpdate rather than withdrawing it, and
# the whole thing reruns idempotently. This test restores that composition
# with synthetic data.
# ---------------------------------------------------------------------------

def test_two_night_drift_composition_coalesce_then_trim_not_withdraw():
    # Night 1: two abutting capsules (end == next start, the upstream norm)
    # ingest as ONE continuous 00:00–17:00 episode.
    night1 = [
        _cap("A1", _utc(2026, 6, 9, 0), _utc(2026, 6, 9, 12)),
        _cap("A1", _utc(2026, 6, 9, 12), _utc(2026, 6, 9, 17)),
    ]
    w1 = PullWindow(Night="1", PullStartUTC=_utc(2026, 6, 8, 18),
                    PullEndUTC=_utc(2026, 6, 9, 18))
    ingest = run_delta([], night1, w1, RUN_AT, id_prefix="N1")
    episodes = episodes_from_detections(ingest.new_events)
    assert len(episodes) == 1, "abutting pair must coalesce, not birth two tickets"
    assert episodes[0].StartUTC == _utc(2026, 6, 9, 0)
    assert episodes[0].EndUTC == _utc(2026, 6, 9, 17)

    # Night 2: re-pull shows only 12:00–17:00 — the morning was a phantom.
    # The capsule OVERLAPS the coalesced episode, so the writer must emit a
    # material BoundaryUpdate (new extent, old extent riding along in
    # Reason), and NO Withdrawn anywhere.
    night2 = [_cap("A1", _utc(2026, 6, 9, 12), _utc(2026, 6, 9, 17))]
    w2 = PullWindow(Night="2", PullStartUTC=_utc(2026, 6, 8, 18),
                    PullEndUTC=_utc(2026, 6, 9, 18))
    r = run_delta(episodes, night2, w2, RUN_AT, id_prefix="N2")
    updates = [e for e in r.new_events if e.EventType is EventType.BoundaryUpdate]
    assert len(updates) == 1
    assert updates[0].ExtentStartUTC == _utc(2026, 6, 9, 12), "trimmed extent"
    assert updates[0].ExtentEndUTC == _utc(2026, 6, 9, 17)
    assert updates[0].TargetEventID, "binds to the surviving ticket"
    assert updates[0].Actor == "clerk-delta", "machine-attributed"
    assert "2026-06-09T00:00" in updates[0].Reason, "old extent rides along"
    assert [e for e in r.new_events if e.EventType is EventType.Withdrawn] == [], \
        "overlap means trim, never withdraw"
    assert [e for e in r.new_events if e.EventType is not EventType.BoundaryUpdate] \
        == [], "nothing but the single BoundaryUpdate"

    # And the night-2 pass is idempotent.
    assert run_delta(episodes, night2, w2, RUN_AT, id_prefix="N2") == r

