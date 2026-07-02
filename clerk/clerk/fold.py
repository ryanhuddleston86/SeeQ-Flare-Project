"""
Step 2 — fold.py (pure, no I/O). Extends list_c_recalc.py's pure-fold
discipline (deterministic replay, no mutation, idempotent) into the Events
ledger domain. list_c_recalc.py itself is untouched — its 8 tests keep
passing unmodified, since the schemas don't overlap.

Replays events per observation in ActedAt order, EventID as the
deterministic tiebreak (spec Step 2). Output is the per-observation
adjudicated state (see Observation).

Grouping (Ryan, 2026-07-02): TargetEventID always references the
observation's ORIGIN event's EventID directly — flat, never a chain.
Origin event types are TechEntry and SeeqDetection; every other event
type's TargetEventID equals that origin id throughout the observation's
whole history, regardless of which specific prior event (e.g. which of
several DismissalProposed cycles) the action conceptually responds to.
fold.py needs no chain-walking: group by (EventID if origin else
TargetEventID), sort each group by (ActedAt, EventID), replay in order.
(SourceEventID in the Ashley handoff = "originating event's UUID" is the
same flat-origin-reference role TargetEventID plays in this schema.)

Two governing principles (Ryan, 2026-07-02):
1. Fold does not re-validate upstream business rules. "Confirmed tickets
   never withdraw" is delta.py's job to enforce ON WRITE, not fold.py's to
   re-check on replay. Replay honestly; trust the stream.
2. Status never gates whether an observation's extent counts as invalid
   time downstream (Guarantee A — unadjudicated downtime counts
   immediately; Needs review counts exactly like Confirmed). fold.py
   returns EVERY observation regardless of status — filtering by status
   belongs to grid.py (Step 4), never here. Do not let an "only Confirmed
   counts" filter creep in.

Corrections are folded without judgment: a reductive Correction (a
shortened extent) is applied exactly like any other Correction, full stop.
The unapproved-flip safety check lives in diff.py (Step 7, Decision 1 —
"IsReduction classification by diffing goes away") — not here.

`detection_class` on the output Observation is sourced from the ORIGIN
event's `DetectionClass` column (nullable — blank on TechEntry origins,
since that column is only ever populated on machine-authored events; see
schemas.py). Added 2026-07-02 so grid.py can match a signed dismissal
against a live capsule by analyzer+class, not analyzer alone.

Analyzers on the output Observation are the ORIGIN event's AnalyzerCEMIDs
only. A later event listing a broader/different CEMID set (e.g. a
Correction naming an additional analyzer) does not widen it — spec Step
4's "manual event listing multiple CEMIDs contributes to every listed
analyzer's union" is grid.py's concern, reading the raw events directly,
not something fold.py aggregates onto one observation.

FLAGGED, not blocking (Ryan, 2026-07-02): TechEntry's initial status while
CorrectiveAction is blank is a GUESS at the app's actual capture flow
(single-form submit vs. fill-in-later): default Needs review, Confirmed
only if the origin TechEntry event itself already carries CorrectiveAction
at fold time. Low stakes — per principle 2, status never touches grid
math. Flag for Ryan to correct if the real capture flow differs. (Every
other event type has an explicit status effect below, so this guess only
ever applies at the single moment the origin TechEntry is folded.)

Note: the spec (12_) lists "Corrected" among possible statuses, but no
event effect in this ruling sets it — Correction explicitly leaves status
unchanged. "Corrected" is kept in the Status enum for spec fidelity but is
presently UNREACHABLE under the current effect table. Flagged, not a hard
stop (not a §5 category).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

from clerk.schemas import Event, EventType

ORIGIN_TYPES = frozenset({EventType.TechEntry, EventType.SeeqDetection})


class Status(str, Enum):
    needs_review      = "Needs review"
    confirmed         = "Confirmed"
    dismissal_pending = "Dismissal pending"
    dismissed         = "Dismissed"
    corrected         = "Corrected"   # spec-listed; unreachable — see module docstring
    withdrawn         = "Withdrawn"
    superseded        = "Superseded"


@dataclass
class Observation:
    origin_event_id: str
    status: Status
    extent_start_utc: datetime
    extent_end_utc: datetime
    signed_dismissal_extent: Optional[Tuple[datetime, datetime]]
    detection_class: Optional[str]
    has_corrective_action: bool
    analyzers: List[str]
    latest_acted_at: datetime
    contributing_event_ids: List[str]


def _origin_id(e: Event) -> str:
    return e.EventID if e.EventType in ORIGIN_TYPES else e.TargetEventID


def fold(events: List[Event]) -> List[Observation]:
    """Pure function: the full event set -> per-observation adjudicated
    state. Late-arriving events simply change the fold — grouping and the
    (ActedAt, EventID) sort key are all that determine the result, not the
    order events were handed to this function."""
    groups: Dict[str, List[Event]] = {}
    for e in events:
        groups.setdefault(_origin_id(e), []).append(e)

    return [_replay(sorted(group, key=lambda e: (e.ActedAt, e.EventID)))
            for group in groups.values()]


def _replay(ordered: List[Event]) -> Observation:
    origin = next(e for e in ordered if e.EventType in ORIGIN_TYPES)

    status: Status = Status.needs_review
    extent_start = origin.ExtentStartUTC
    extent_end = origin.ExtentEndUTC
    signed_dismissal_extent: Optional[Tuple[datetime, datetime]] = None
    has_corrective_action = False
    latest_acted_at = ordered[0].ActedAt
    contributing: List[str] = []

    for e in ordered:
        contributing.append(e.EventID)
        latest_acted_at = e.ActedAt
        if (e.CorrectiveAction or "").strip():
            has_corrective_action = True

        if e.EventType is EventType.SeeqDetection:
            status = Status.needs_review
        elif e.EventType is EventType.TechEntry:
            # [FLAGGED — see module docstring]: guessed default.
            status = Status.confirmed if has_corrective_action else Status.needs_review
        elif e.EventType is EventType.Confirmation:
            status = Status.confirmed
        elif e.EventType is EventType.DismissalProposed:
            status = Status.dismissal_pending
        elif e.EventType is EventType.Approval:
            status = Status.dismissed
            # ledger 17: THIS event's own stated extent, never the
            # observation's current extent.
            signed_dismissal_extent = (e.ExtentStartUTC, e.ExtentEndUTC)
        elif e.EventType is EventType.DismissalRejected:
            status = Status.needs_review
        elif e.EventType is EventType.Reopen:
            status = Status.needs_review
        elif e.EventType is EventType.BoundaryUpdate:
            extent_start, extent_end = e.ExtentStartUTC, e.ExtentEndUTC
            # status unchanged; a co-emitted Reopen (if any) is a separate
            # event the replay picks up on its own.
        elif e.EventType is EventType.Withdrawn:
            status = Status.withdrawn
        elif e.EventType is EventType.Superseded:
            status = Status.superseded
        elif e.EventType is EventType.Correction:
            extent_start, extent_end = e.ExtentStartUTC, e.ExtentEndUTC
            # status unchanged; folded without judgment — no reductive-edit
            # gating here (diff.py, Step 7, owns that check).
        else:
            raise AssertionError(f"unhandled EventType in fold: {e.EventType}")

    return Observation(
        origin_event_id=origin.EventID,
        status=status,
        extent_start_utc=extent_start,
        extent_end_utc=extent_end,
        signed_dismissal_extent=signed_dismissal_extent,
        detection_class=(origin.DetectionClass or "").strip() or None,
        has_corrective_action=has_corrective_action,
        analyzers=list(origin.AnalyzerCEMIDs),
        latest_acted_at=latest_acted_at,
        contributing_event_ids=contributing,
    )
