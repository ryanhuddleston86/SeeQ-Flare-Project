"""
capsule_reconciler.py — Spike: temporal-overlap capsule ID reconciliation.

Validates the S-4 reconciliation logic before building the real Seeq merge
phase. Not production code — run it, inspect it, throw it away.

Judgment calls recorded here:
  JC1 (merge metadata): B — structured merged_from list preserving source IDs
    and all metadata fields; nothing flattened or dropped.
  JC2 (split metadata): A — full metadata copied to every fragment; each
    fragment gets split_warning=True so the caller knows to decide ownership.
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reconcile(existing: list[Capsule], incoming: list[Window]) -> list[Capsule]:
    """
    Reconcile incoming Seeq windows against stored capsules by temporal overlap.

    Per-component rules:
      0 existing, 1 incoming  → new UUID, empty metadata
      1 existing, 1 incoming  → preserve UUID + metadata, update bounds (drift)
      N existing, 1 incoming  → merge: earliest-created UUID survives;
                                 metadata = {"merged_from": [{id, ...fields}, ...]}
                                 sorted by capsule start (JC1=B)
      1 existing, N incoming  → split: original UUID on largest-overlap fragment;
                                 full metadata copied to every fragment with
                                 split_warning=True (JC2=A)
      M existing, N incoming  → complex merge+split: merge logic for anchor,
                                 split logic for fragments, complex_warning=True
      existing with no incoming overlap → silently dropped (Seeq removed it)
    """
    components = _find_components(existing, incoming)
    result: list[Capsule] = []

    for e_indices, i_indices in components:
        e_caps = [existing[i] for i in e_indices]
        i_wins = [incoming[i] for i in i_indices]

        # Dropped by Seeq.
        if not i_wins:
            continue

        # Brand-new window.
        if not e_caps:
            result.append(Capsule(
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
            # JC1=B: preserve full metadata from every source capsule.
            base_meta: dict[str, Any] = {
                "merged_from": [
                    {"id": c.id, **c.metadata}
                    for c in sorted(e_caps, key=lambda c: c.start)
                ]
            }
            if is_split:
                base_meta["complex_warning"] = True
        else:
            base_meta = dict(e_caps[0].metadata)

        if not is_split:
            # Drift or pure merge — one output capsule.
            result.append(Capsule(
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

            # JC2=A: full metadata on every fragment so nothing silently vanishes;
            # split_warning signals that annotation ownership is unresolved.
            result.append(Capsule(
                id=anchor.id,
                start=primary.start,
                end=primary.end,
                metadata={**base_meta, "split_warning": True},
                created_at=anchor.created_at,
            ))
            for win in secondaries:
                result.append(Capsule(
                    id=str(uuid.uuid4()),
                    start=win.start,
                    end=win.end,
                    metadata={**base_meta, "split_warning": True},
                ))

    return result


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

def _dt(hour: int, minute: int = 0) -> datetime:
    """Shorthand: fixed date at HH:MM UTC, for readable fixtures."""
    return datetime(2026, 1, 15, hour, minute, tzinfo=timezone.utc)


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
    result = reconcile([cap], [Window(start=_dt(7, 58), end=_dt(10, 3))])
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
    result = reconcile([cap_a, cap_b], [Window(start=_dt(8), end=_dt(12))])
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
    result = reconcile([cap_orig], [win_small, win_large])
    check("Two capsules returned", len(result) == 2)
    check("Original ID present exactly once", sum(1 for r in result if r.id == "orig-id") == 1)
    primary = next(r for r in result if r.id == "orig-id")
    check("Original ID on larger fragment [09:30, 12:00]",
          primary.start == _dt(9, 30) and primary.end == _dt(12))
    check("Both fragments have split_warning (JC2=A)",
          all(r.metadata.get("split_warning") is True for r in result))
    check("Both fragments carry annotation (JC2=A)",
          all(r.metadata.get("annotation") == "Long outage" for r in result))

    # ------------------------------------------------------------------
    # Scenario 4: Idempotency — two identical reconcile passes must not
    # mint new IDs. Uses the drift capsule from Scenario 1.
    # ------------------------------------------------------------------
    print("\nScenario 4: Idempotency")
    cap_idem = Capsule(
        id="idem-id", start=_dt(7, 58), end=_dt(10, 3),
        metadata={"annotation": "Test"}, created_at=_dt(0),
    )
    same_win = Window(start=_dt(7, 58), end=_dt(10, 3))
    r1 = reconcile([cap_idem], [same_win])
    r2 = reconcile(r1, [same_win])
    check("First pass: ID preserved", len(r1) == 1 and r1[0].id == "idem-id")
    check("Second pass: ID preserved", len(r2) == 1 and r2[0].id == "idem-id")
    check("Second pass: bounds unchanged",
          r2[0].start == _dt(7, 58) and r2[0].end == _dt(10, 3))

    # ------------------------------------------------------------------
    # Scenario 5: New capsule — no existing capsules at all.
    # ------------------------------------------------------------------
    print("\nScenario 5: New capsule (no existing overlap)")
    result = reconcile([], [Window(start=_dt(14), end=_dt(16))])
    check("One capsule minted", len(result) == 1)
    check("Has a UUID", bool(result[0].id))
    check("Empty metadata", result[0].metadata == {})

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
