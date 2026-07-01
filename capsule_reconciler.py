"""
capsule_reconciler.py — Spike: temporal-overlap capsule ID reconciliation.

Validates the S-4 reconciliation logic before building the real Seeq merge
phase. Not production code — run it, inspect it, throw it away.

Judgment calls recorded here:
  JC1 (merge): B — structured merged_from list preserving source IDs and all
    metadata fields; annotation text deduplicated by exact stripped match so a
    split-then-merge round-trip doesn't double the note. Near-exact/similarity
    dedup deferred to production.
  JC2 (split): A — full metadata copied to every fragment; split_warning=True
    and a note_origin marker on every fragment (symmetric — field always present):
      note_origin="original"            → fragment keeping the pre-split UUID
      note_origin="split_from:<uuid>"   → fragment(s) receiving a copy
    A user edit to a fragment should clear note_origin; the note then becomes
    original to that fragment. This spike simulates edits via direct mutation.
  JC3 (archive): Option B — reconcile() archives capsules with no incoming overlap
    instead of dropping them. Archive is a preservation path: metadata survives
    intact; bookkeeping (archived_reason, archived_at) lives on ArchivedCapsule,
    not inside capsule.metadata, so the capsule's own record is not polluted.
    Reanimation is caller-driven: reconcile() has no awareness of the archive store.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Capsule:
    """Stored capsule with a stable identity that survives Seeq recomputes."""
    id: str
    start: datetime
    end: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)


@dataclass
class Window:
    """Incoming Seeq-detected time window — no identity yet."""
    start: datetime
    end: datetime


@dataclass
class ArchivedCapsule:
    """
    A capsule that had no overlapping window in the latest Seeq pull.

    The original capsule is preserved exactly as it was — id, start, end,
    metadata (annotation, note_origin, etc.) all intact. Archive bookkeeping
    lives here, not in capsule.metadata, so the capsule's own record is not
    polluted by structural event markers.
    """
    capsule: Capsule
    archived_reason: str   # "no_overlap" — had no matching incoming window this run
    archived_at: datetime  # timestamp of the reconcile run that archived it


@dataclass
class ReconcileResult:
    """
    Return type for reconcile(). Both lists must be handled by the caller —
    ignoring archived means accepting the silent-drop bug at one layer up.
    """
    reconciled: list[Capsule]
    archived: list[ArchivedCapsule]


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _overlaps(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    """True iff [s1, e1) and [s2, e2) share interior time. Touching endpoints are not overlap."""
    return s1 < e2 and s2 < e1


def _overlap_seconds(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> float:
    lo = max(s1, s2)
    hi = min(e1, e2)
    return (hi - lo).total_seconds() if hi > lo else 0.0


def _find_components(
    existing: list[Capsule], incoming: list[Window]
) -> list[tuple[list[int], list[int]]]:
    """
    Build a bipartite overlap graph and return connected components as
    (existing_indices, incoming_indices) pairs.

    Components drive reconcile() — each component is one merge/split/drift/new
    decision, made independently.
    """
    e_to_i: dict[int, list[int]] = defaultdict(list)
    i_to_e: dict[int, list[int]] = defaultdict(list)

    for i_idx, win in enumerate(incoming):
        for e_idx, cap in enumerate(existing):
            if _overlaps(cap.start, cap.end, win.start, win.end):
                e_to_i[e_idx].append(i_idx)
                i_to_e[i_idx].append(e_idx)

    visited_e: set[int] = set()
    visited_i: set[int] = set()
    components: list[tuple[list[int], list[int]]] = []

    for start_e in range(len(existing)):
        if start_e in visited_e:
            continue
        comp_e: list[int] = []
        comp_i: list[int] = []
        queue: list[tuple[str, int]] = [("e", start_e)]
        while queue:
            kind, idx = queue.pop()
            if kind == "e":
                if idx in visited_e:
                    continue
                visited_e.add(idx)
                comp_e.append(idx)
                for j in e_to_i[idx]:
                    if j not in visited_i:
                        queue.append(("i", j))
            else:
                if idx in visited_i:
                    continue
                visited_i.add(idx)
                comp_i.append(idx)
                for j in i_to_e[idx]:
                    if j not in visited_e:
                        queue.append(("e", j))
        components.append((comp_e, comp_i))

    # Incoming windows with no existing overlap become singleton "new" components.
    for i_idx in range(len(incoming)):
        if i_idx not in visited_i:
            components.append(([], [i_idx]))

    return components


def _build_merged_from(caps: list[Capsule]) -> list[dict[str, Any]]:
    """
    Build the merged_from list for a merge event.

    Deduplicates by annotation text (exact stripped match) so a split-then-merge
    round-trip doesn't produce two identical sentences in the combined record.
    Near-exact / similarity matching is deferred to production.
    """
    seen_annotations: set[str] = set()
    entries: list[dict[str, Any]] = []
    for cap in caps:
        text = (cap.metadata.get("annotation") or "").strip()
        if text and text in seen_annotations:
            continue
        if text:
            seen_annotations.add(text)
        entries.append({"id": cap.id, **cap.metadata})
    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reconcile(
    existing: list[Capsule],
    incoming: list[Window],
    run_at: datetime,
) -> ReconcileResult:
    """
    Reconcile incoming Seeq windows against stored capsules by temporal overlap.

    Per-component rules:
      0 existing, 1 incoming  → new UUID, empty metadata
      1 existing, 1 incoming  → preserve UUID + metadata, update bounds (drift)
      N existing, 1 incoming  → merge: earliest-created UUID survives;
                                 metadata = {"merged_from": [{id, ...fields}, ...]}
                                 sorted by capsule start, annotation text
                                 deduplicated (JC1=B)
      1 existing, N incoming  → split: original UUID on largest-overlap fragment;
                                 full metadata copied to every fragment;
                                 note_origin="original" on primary,
                                 note_origin="split_from:<id>" on secondaries;
                                 split_warning=True on all (JC2=A)
      M existing, N incoming  → complex merge+split: merge logic for anchor,
                                 split logic for fragments, complex_warning=True
      existing with no incoming overlap → archived (JC3): full capsule preserved
                                 in ReconcileResult.archived with archived_reason
                                 and archived_at; NOT in ReconcileResult.reconciled

    run_at must be supplied by the caller (not generated internally) so that
    archived_at is deterministic and testable.
    """
    # TODO: reconcile() intentionally has no awareness of archived capsules.
    # Callers MUST check archived output before treating a reappearing window
    # as brand-new, or this silently reintroduces the drop bug one layer up.
    # Option B (caller-driven reanimation): if a subsequent Seeq pull produces
    # a window that overlaps a previously archived capsule's time range, the
    # caller is responsible for passing that ArchivedCapsule.capsule back into
    # `existing`. reconcile() will then treat it as a normal drift/update and
    # preserve the original ID and metadata.

    components = _find_components(existing, incoming)
    reconciled: list[Capsule] = []
    archived: list[ArchivedCapsule] = []

    for e_indices, i_indices in components:
        e_caps = [existing[i] for i in e_indices]
        i_wins = [incoming[i] for i in i_indices]

        # No incoming overlap — archive rather than drop.
        if not i_wins:
            for cap in e_caps:
                archived.append(ArchivedCapsule(
                    capsule=cap,
                    archived_reason="no_overlap",
                    archived_at=run_at,
                ))
            continue

        # Brand-new window.
        if not e_caps:
            reconciled.append(Capsule(
                id=str(uuid.uuid4()),
                start=i_wins[0].start,
                end=i_wins[0].end,
            ))
            continue

        anchor = min(e_caps, key=lambda c: c.created_at)
        is_merge = len(e_caps) >= 2
        is_split = len(i_wins) >= 2

        # Build base metadata for this component.
        if is_merge:
            # JC1=B: deduplicated structured list; annotation text dedup guards
            # against a split fragment re-merging and doubling the same sentence.
            base_meta: dict[str, Any] = {
                "merged_from": _build_merged_from(sorted(e_caps, key=lambda c: c.start))
            }
            if is_split:
                base_meta["complex_warning"] = True
        else:
            base_meta = dict(e_caps[0].metadata)

        if not is_split:
            # Drift or pure merge — one output capsule.
            reconciled.append(Capsule(
                id=anchor.id,
                start=i_wins[0].start,
                end=i_wins[0].end,
                metadata=base_meta,
                created_at=anchor.created_at,
            ))
        else:
            # Split or complex: rank windows by total overlap against all existing
            # capsules in this component, then assign the anchor ID to the winner.
            def total_overlap(win: Window) -> float:
                return sum(
                    _overlap_seconds(c.start, c.end, win.start, win.end)
                    for c in e_caps
                )

            ranked = sorted(i_wins, key=total_overlap, reverse=True)
            primary, secondaries = ranked[0], ranked[1:]

            # JC2=A: full metadata on every fragment; note_origin is always present
            # (symmetric) so downstream can filter on the field without silently
            # missing the primary fragment. A user edit to any fragment should clear
            # note_origin — the note then becomes original to that fragment.
            reconciled.append(Capsule(
                id=anchor.id,
                start=primary.start,
                end=primary.end,
                metadata={**base_meta, "split_warning": True, "note_origin": "original"},
                created_at=anchor.created_at,
            ))
            for win in secondaries:
                reconciled.append(Capsule(
                    id=str(uuid.uuid4()),
                    start=win.start,
                    end=win.end,
                    metadata={
                        **base_meta,
                        "split_warning": True,
                        "note_origin": f"split_from:{anchor.id}",
                    },
                ))

    return ReconcileResult(reconciled=reconciled, archived=archived)


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

def _dt(hour: int, minute: int = 0) -> datetime:
    """Shorthand: fixed date at HH:MM UTC, for readable fixtures."""
    return datetime(2026, 1, 15, hour, minute, tzinfo=timezone.utc)


# Fixed reconcile run timestamp used across all tests.
RUN_AT = _dt(12)


def _run_tests() -> None:
    passed = 0
    failed = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            print(f"  PASS  {label}")
            passed += 1
        else:
            print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))
            failed += 1

    # ------------------------------------------------------------------
    # Scenario 1: Boundary drift — capsule grows/shrinks, ID must survive.
    # Existing [08:00, 10:00] → incoming [07:58, 10:03].
    # ------------------------------------------------------------------
    print("\nScenario 1: Boundary drift")
    cap = Capsule(
        id="drift-id", start=_dt(8), end=_dt(10),
        metadata={"annotation": "Analyzer offline"}, created_at=_dt(0),
    )
    result = reconcile([cap], [Window(start=_dt(7, 58), end=_dt(10, 3))], run_at=RUN_AT).reconciled
    check("Single capsule returned", len(result) == 1)
    check("ID preserved", result[0].id == "drift-id")
    check("Bounds updated", result[0].start == _dt(7, 58) and result[0].end == _dt(10, 3))
    check("Metadata preserved", result[0].metadata.get("annotation") == "Analyzer offline")

    # ------------------------------------------------------------------
    # Scenario 2: Merge — two adjacent capsules become one window.
    # cap-a [08:00, 10:00], cap-b [10:00, 12:00] → incoming [08:00, 12:00].
    # cap-a is earliest-created so its ID survives.
    # ------------------------------------------------------------------
    print("\nScenario 2: Merge (two capsules → one window)")
    cap_a = Capsule(
        id="cap-a", start=_dt(8), end=_dt(10),
        metadata={"annotation": "Motor trip"}, created_at=_dt(0),
    )
    cap_b = Capsule(
        id="cap-b", start=_dt(10), end=_dt(12),
        metadata={"annotation": "Leak on sample line"}, created_at=_dt(1),
    )
    result = reconcile([cap_a, cap_b], [Window(start=_dt(8), end=_dt(12))], run_at=RUN_AT).reconciled
    check("Single capsule returned", len(result) == 1)
    check("Earliest ID survives (cap-a)", result[0].id == "cap-a")
    check("Bounds span full merge", result[0].start == _dt(8) and result[0].end == _dt(12))
    mf = result[0].metadata.get("merged_from", [])
    check("merged_from has two entries", len(mf) == 2)
    check("cap-a annotation in merged_from",
          any(e.get("annotation") == "Motor trip" for e in mf))
    check("cap-b annotation in merged_from",
          any(e.get("annotation") == "Leak on sample line" for e in mf))
    check("merged_from sorted by start (cap-a first)", mf[0].get("id") == "cap-a")

    # ------------------------------------------------------------------
    # Scenario 3: Split — one capsule becomes two windows.
    # Existing [08:00, 12:00] → incoming [08:00, 09:00] (1 h) and
    # [09:30, 12:00] (2.5 h). Original ID goes to the larger-overlap fragment.
    # Both fragments should carry the annotation with split_warning=True.
    # ------------------------------------------------------------------
    print("\nScenario 3: Split (one capsule → two windows)")
    cap_orig = Capsule(
        id="orig-id", start=_dt(8), end=_dt(12),
        metadata={"annotation": "Long outage"}, created_at=_dt(0),
    )
    win_small = Window(start=_dt(8), end=_dt(9))       # 1 h overlap with [08,12]
    win_large = Window(start=_dt(9, 30), end=_dt(12))  # 2.5 h overlap with [08,12]
    result = reconcile([cap_orig], [win_small, win_large], run_at=RUN_AT).reconciled
    check("Two capsules returned", len(result) == 2)
    check("Original ID present exactly once", sum(1 for r in result if r.id == "orig-id") == 1)
    primary = next(r for r in result if r.id == "orig-id")
    check("Original ID on larger fragment [09:30, 12:00]",
          primary.start == _dt(9, 30) and primary.end == _dt(12))
    check("Both fragments have split_warning",
          all(r.metadata.get("split_warning") is True for r in result))
    check("Both fragments carry annotation (JC2=A)",
          all(r.metadata.get("annotation") == "Long outage" for r in result))
    secondary_s3 = next(r for r in result if r.id != "orig-id")
    check("Primary has note_origin='original'",
          primary.metadata.get("note_origin") == "original")
    check("Secondary has note_origin='split_from:orig-id'",
          secondary_s3.metadata.get("note_origin") == "split_from:orig-id")

    # ------------------------------------------------------------------
    # Scenario 4: Idempotency — two identical reconcile passes must not
    # mint new IDs.
    # ------------------------------------------------------------------
    print("\nScenario 4: Idempotency")
    cap_idem = Capsule(
        id="idem-id", start=_dt(7, 58), end=_dt(10, 3),
        metadata={"annotation": "Test"}, created_at=_dt(0),
    )
    same_win = Window(start=_dt(7, 58), end=_dt(10, 3))
    r1 = reconcile([cap_idem], [same_win], run_at=RUN_AT).reconciled
    r2 = reconcile(r1, [same_win], run_at=RUN_AT).reconciled
    check("First pass: ID preserved", len(r1) == 1 and r1[0].id == "idem-id")
    check("Second pass: ID preserved", len(r2) == 1 and r2[0].id == "idem-id")
    check("Second pass: bounds unchanged",
          r2[0].start == _dt(7, 58) and r2[0].end == _dt(10, 3))

    # ------------------------------------------------------------------
    # Scenario 5: New capsule — no existing capsules at all.
    # ------------------------------------------------------------------
    print("\nScenario 5: New capsule (no existing overlap)")
    result = reconcile([], [Window(start=_dt(14), end=_dt(16))], run_at=RUN_AT).reconciled
    check("One capsule minted", len(result) == 1)
    check("Has a UUID", bool(result[0].id))
    check("Empty metadata", result[0].metadata == {})

    # ------------------------------------------------------------------
    # Scenario 6: Split provenance — both fragments carry the full note
    # and the correct note_origin marker. Same geometry as Scenario 3.
    # ------------------------------------------------------------------
    print("\nScenario 6: Split provenance markers")
    cap_prov = Capsule(
        id="prov-id", start=_dt(8), end=_dt(12),
        metadata={"annotation": "Long outage"}, created_at=_dt(0),
    )
    result = reconcile([cap_prov], [win_small, win_large], run_at=RUN_AT).reconciled
    prim6 = next(r for r in result if r.id == "prov-id")
    sec6  = next(r for r in result if r.id != "prov-id")
    check("Both fragments carry full annotation",
          all(r.metadata.get("annotation") == "Long outage" for r in result))
    check("Primary note_origin='original'",
          prim6.metadata.get("note_origin") == "original")
    check("Secondary note_origin='split_from:prov-id'",
          sec6.metadata.get("note_origin") == "split_from:prov-id")
    check("note_origin present on every fragment (symmetric)",
          all("note_origin" in r.metadata for r in result))

    # ------------------------------------------------------------------
    # Scenario 7: Merge after split — annotation must not be duplicated.
    # Take the two split fragments from Scenario 6 (both have "Long outage")
    # and let Seeq re-detect one continuous window. merged_from should contain
    # "Long outage" exactly once.
    # ------------------------------------------------------------------
    print("\nScenario 7: Merge-after-split deduplication")
    result = reconcile([prim6, sec6], [Window(start=_dt(8), end=_dt(12))], run_at=RUN_AT).reconciled
    check("Single capsule returned", len(result) == 1)
    mf7 = result[0].metadata.get("merged_from", [])
    long_outage_count = sum(1 for e in mf7 if e.get("annotation") == "Long outage")
    check("'Long outage' appears exactly once in merged_from", long_outage_count == 1)

    # ------------------------------------------------------------------
    # Scenario 8: Divergence after split — fragments are independent objects;
    # editing one must not affect the other. Simulates what a real edit path
    # would do: update annotation, clear note_origin (note is now original to
    # this fragment). After divergence, a subsequent merge must preserve both
    # distinct annotations since they no longer match.
    # ------------------------------------------------------------------
    print("\nScenario 8: Divergence after split (edit-clears-marker)")
    result6 = reconcile([cap_prov], [win_small, win_large], run_at=RUN_AT).reconciled
    frag_p = next(r for r in result6 if r.id == "prov-id")
    frag_s = next(r for r in result6 if r.id != "prov-id")

    # Simulate a user independently editing frag_s.
    # A real edit path would do the same: overwrite annotation, clear note_origin.
    frag_s.metadata = {"annotation": "Separate leak issue"}

    check("Sibling (frag_p) annotation unchanged after edit",
          frag_p.metadata.get("annotation") == "Long outage")
    check("Sibling (frag_p) note_origin unchanged",
          frag_p.metadata.get("note_origin") == "original")
    check("Edited fragment has new annotation",
          frag_s.metadata.get("annotation") == "Separate leak issue")
    check("note_origin cleared by edit (note is now original to this fragment)",
          "note_origin" not in frag_s.metadata)

    result8 = reconcile([frag_p, frag_s], [Window(start=_dt(8), end=_dt(12))], run_at=RUN_AT).reconciled
    mf8 = result8[0].metadata.get("merged_from", [])
    annotations8 = [e.get("annotation") for e in mf8]
    check("Both diverged annotations survive merge (no dedup of distinct text)",
          "Long outage" in annotations8 and "Separate leak issue" in annotations8)

    # ------------------------------------------------------------------
    # Scenario 9: No-overlap archive — capsule with no incoming match goes to
    # archived, not reconciled. archived_reason and archived_at must be set.
    # A concurrent new window (different time range) still lands in reconciled.
    # ------------------------------------------------------------------
    print("\nScenario 9: No-overlap → archived with marker")
    cap_drop = Capsule(
        id="drop-id", start=_dt(8), end=_dt(10),
        metadata={"annotation": "Will be archived"}, created_at=_dt(0),
    )
    r9 = reconcile(
        [cap_drop],
        [Window(start=_dt(14), end=_dt(16))],  # no overlap with [08,10]
        run_at=RUN_AT,
    )
    check("Archived capsule absent from reconciled",
          not any(c.id == "drop-id" for c in r9.reconciled))
    check("Archived capsule present in archived", len(r9.archived) == 1)
    check("archived_reason='no_overlap'",
          r9.archived[0].archived_reason == "no_overlap")
    check("archived_at matches run_at",
          r9.archived[0].archived_at == RUN_AT)
    check("Original capsule ID intact in archive",
          r9.archived[0].capsule.id == "drop-id")
    check("New window still minted in reconciled", len(r9.reconciled) == 1)

    # ------------------------------------------------------------------
    # Scenario 10: Note text survives archive intact.
    # Capsule with annotation and note_origin — both must be present and
    # unchanged on the archived entry. No incoming windows at all this run.
    # ------------------------------------------------------------------
    print("\nScenario 10: Note text and metadata survive archive intact")
    cap_noted = Capsule(
        id="noted-id", start=_dt(8), end=_dt(10),
        metadata={"annotation": "Compressor fault", "note_origin": "original"},
        created_at=_dt(0),
    )
    r10 = reconcile([cap_noted], [], run_at=RUN_AT)
    check("Capsule archived (no incoming windows)", len(r10.archived) == 1)
    check("annotation survives in archive",
          r10.archived[0].capsule.metadata.get("annotation") == "Compressor fault")
    check("note_origin survives in archive",
          r10.archived[0].capsule.metadata.get("note_origin") == "original")
    check("Metadata object matches original (not mutated)",
          r10.archived[0].capsule.metadata == cap_noted.metadata)
    check("Reconciled is empty", r10.reconciled == [])

    # ------------------------------------------------------------------
    # Scenario 11: Reappearance after archive (Option B — caller-driven).
    #
    # Design decision (JC3): reconcile() has no awareness of the archive store.
    # If a window reappears that overlaps a previously archived capsule, the
    # CALLER is responsible for detecting this and passing the archived capsule
    # back into `existing`. reconcile() then treats it as a normal drift/update,
    # preserving the original ID and metadata. If the caller skips this check
    # and passes nothing into existing, a new UUID is minted — silently
    # reintroducing the drop bug one layer up. This test demonstrates the
    # correct (caller-checks-archive) path only; the incorrect path is not
    # tested here but is documented in the TODO comment in reconcile().
    # ------------------------------------------------------------------
    print("\nScenario 11: Reappearance after archive (caller-driven reanimation)")
    cap_return = Capsule(
        id="return-id", start=_dt(8), end=_dt(10),
        metadata={"annotation": "Intermittent fault"}, created_at=_dt(0),
    )

    # Pass 1: no incoming windows → capsule archived.
    r11a = reconcile([cap_return], [], run_at=RUN_AT)
    check("Pass 1: capsule archived",
          len(r11a.archived) == 1 and r11a.archived[0].capsule.id == "return-id")
    check("Pass 1: reconciled is empty", r11a.reconciled == [])

    # Pass 2: caller notices a new window overlapping the archived time range,
    # looks up the archive, and passes the original capsule back into existing.
    reanimated = r11a.archived[0].capsule
    r11b = reconcile(
        [reanimated],
        [Window(start=_dt(8), end=_dt(10, 30))],
        run_at=_dt(13),
    )
    check("Pass 2: original ID preserved (not a new mint)",
          len(r11b.reconciled) == 1 and r11b.reconciled[0].id == "return-id")
    check("Pass 2: bounds updated to new window",
          r11b.reconciled[0].end == _dt(10, 30))
    check("Pass 2: annotation preserved through archive and back",
          r11b.reconciled[0].metadata.get("annotation") == "Intermittent fault")
    check("Pass 2: nothing re-archived", r11b.archived == [])

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        print("SPIKE NOT CLEAN — review failures above.")
    else:
        print("All scenarios pass. Reconciliation logic validated.")


if __name__ == "__main__":
    _run_tests()
