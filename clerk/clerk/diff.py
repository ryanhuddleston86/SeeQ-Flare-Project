"""
Step 7 — diff.py (grid diff + alarm taxonomy, gate G5). Pure, no I/O.

Compares grid.csv (current) against prior_grid.csv (yesterday's). Two
independent checks:

1. Cell flip ✗→✓ (invalid -> valid): must trace to EXACTLY one of three
   outcomes — no fourth outcome (gate G5):
     - an Approval event covering that extent           -> SILENT PASS
     - a machine-attributed Withdrawn/BoundaryUpdate     -> digest-informational
     - anything else (including nothing traceable)       -> INTEGRITY ALERT
   A Correction moving a boundary such that an hour flips valid, without
   going through the machine BoundaryUpdate path, correctly falls through
   to INTEGRITY ALERT — this IS the "unapproved-flip safety check" fold.py's
   docstring deferred here (Decision 1: "IsReduction classification by
   diffing goes away" — diff.py catches it structurally, not by inspecting
   Correction's own polarity).

2. New ✗ (invalid now, not invalid before) whose hour's SITE-LOCAL calendar
   day is older than LateXThresholdDays -> late-arrival ping. Independent
   of the flip check; a cell can be both a late-arrival AND unrelated to
   any flip (e.g. a first-time invalid hour, not a former valid one).

First run (spec): prior_grid.csv absent -> prior_cells == [] -> every
current cell is reported as new; no flip-tracing is attempted (there is
nothing to diff against).

PROVENANCE CHECK (Ryan's ask, before building the trace logic): grid.py's
RuleApplied is branch-level only ("(i)", "(iii)(A)", etc.) — not usable for
flip attribution, and G5 doesn't need it; only Valid transitions matter.
ContributingEventIDs IS sufficient for the traceable cases — every ticket
that made an hour invalid is a folded Observation with an origin_event_id,
and Observation.contributing_event_ids gives the full chronological event
history needed to find the specific Approval/Withdrawn/BoundaryUpdate that
explains a flip.

RESOLVED 2026-07-02 (was flagged as a provenance gap): ContributingEventIDs
now also carries synthetic capsule ids (`grid.capsule_provenance_id`,
prefixed `CAP:`) for raw, unticketed detection contributions. A flip whose
ONLY contributors are unticketed capsules that are identity-unchanged
(same analyzer+class+start+end) between the prior pull and tonight's
`current_capsules` resolves SILENT — the raw detection didn't change, so
there is nothing new to approve or explain. A capsule that's new, moved
(any interval change yields a different synthetic id), or gone entirely
still has no Observation to check and falls to INTEGRITY ALERT exactly as
before — this only closes the "stable, still-unticketed" case, not the
general gap of unticketed detections having no ledger trail at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from clerk.delta import MACHINE_ACTOR
from clerk.fold import Observation, Status, fold
from clerk.grid import CAPSULE_ID_PREFIX, capsule_provenance_id
from clerk.schemas import Capsule, CellValid, Event, EventType, GridCell


class FlipOutcome(str, Enum):
    silent_pass            = "silent_pass"
    machine_informational  = "machine_informational"
    integrity_alert        = "integrity_alert"


@dataclass
class CellFlip:
    analyzer: str
    hour_start_utc: datetime
    outcome: FlipOutcome
    explanation: str
    origin_event_ids: List[str]


@dataclass
class LateArrival:
    analyzer: str
    hour_start_utc: datetime
    days_old: int


@dataclass
class NewInvalidCell:
    """Every cell newly invalid since the prior grid — a superset of
    `late_arrivals` (which is filtered to those past LateXThresholdDays).
    Exists so digest.py's "new downtime since yesterday" section doesn't
    need to re-derive this from prior/current cells itself."""
    analyzer: str
    hour_start_utc: datetime


@dataclass
class DiffResult:
    is_first_run: bool
    new_cell_count: int
    silent_passes: List[CellFlip] = field(default_factory=list)
    machine_informational: List[CellFlip] = field(default_factory=list)
    integrity_alerts: List[CellFlip] = field(default_factory=list)
    late_arrivals: List[LateArrival] = field(default_factory=list)
    new_invalid_cells: List[NewInvalidCell] = field(default_factory=list)


@dataclass
class _Explanation:
    outcome: FlipOutcome
    detail: str


def _local_date(hour_start_utc: datetime, tz_name: str) -> date:
    return hour_start_utc.astimezone(ZoneInfo(tz_name)).date()


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def _find_last_event(
    obs: Observation,
    events_by_id: Dict[str, Event],
    types: Set[EventType],
) -> Optional[Event]:
    """The most recent event (by fold order — contributing_event_ids is
    already chronological) among `types` in this observation's history."""
    for eid in reversed(obs.contributing_event_ids):
        e = events_by_id.get(eid)
        if e is not None and e.EventType in types:
            return e
    return None


def _explain_capsule(capsule_id: str, current_capsule_ids: Set[str]) -> _Explanation:
    """A capsule-provenance contributor (no ticket ever existed for it).
    Identity-unchanged across both pulls -> nothing new happened at the
    detection layer, so the flip needs no approval. New, moved (any
    interval change yields a different id), or gone entirely -> there is
    no Observation to check, so this still falls to INTEGRITY ALERT."""
    if capsule_id in current_capsule_ids:
        return _Explanation(FlipOutcome.silent_pass,
                            f"{capsule_id}: unticketed capsule unchanged across both "
                            "pulls — nothing to approve")
    return _Explanation(FlipOutcome.integrity_alert,
                        f"{capsule_id}: unticketed capsule no longer present (or moved) "
                        "— no Observation exists to explain the flip")


def _explain_one(
    origin_id: str,
    observations_by_origin: Dict[str, Observation],
    events_by_id: Dict[str, Event],
    hour_start: datetime,
    hour_end: datetime,
) -> _Explanation:
    obs = observations_by_origin.get(origin_id)
    if obs is None:
        return _Explanation(FlipOutcome.integrity_alert,
                            f"{origin_id}: no longer found in today's fold")

    if obs.status is Status.dismissed and obs.signed_dismissal_extent is not None:
        sds, sde = obs.signed_dismissal_extent
        if _overlaps(sds, sde, hour_start, hour_end):
            return _Explanation(FlipOutcome.silent_pass,
                                f"{origin_id}: Approval's signed extent covers this hour")
        return _Explanation(FlipOutcome.integrity_alert,
                            f"{origin_id}: Dismissed but the signed extent does not "
                            "cover this hour")

    if obs.status is Status.withdrawn:
        withdrawn = _find_last_event(obs, events_by_id, {EventType.Withdrawn})
        if withdrawn is not None and withdrawn.Actor == MACHINE_ACTOR:
            return _Explanation(FlipOutcome.machine_informational,
                                f"{origin_id}: machine-attributed Withdrawn")
        return _Explanation(FlipOutcome.integrity_alert,
                            f"{origin_id}: Withdrawn but not machine-attributed")

    if not _overlaps(obs.extent_start_utc, obs.extent_end_utc, hour_start, hour_end):
        # The ticket is still "live" (not Dismissed/Withdrawn/Superseded) but
        # its current extent no longer covers this hour — something moved
        # the boundary. Only a machine-attributed BoundaryUpdate is an
        # allowed explanation; a Correction (human path) is NOT — this is
        # the unapproved-flip safety net fold.py deferred here.
        mover = _find_last_event(obs, events_by_id,
                                 {EventType.BoundaryUpdate, EventType.Correction})
        if (mover is not None and mover.EventType is EventType.BoundaryUpdate
                and mover.Actor == MACHINE_ACTOR):
            return _Explanation(FlipOutcome.machine_informational,
                                f"{origin_id}: machine-attributed BoundaryUpdate moved "
                                "the boundary off this hour")
        if mover is not None and mover.EventType is EventType.Correction:
            return _Explanation(FlipOutcome.integrity_alert,
                                f"{origin_id}: extent moved via Correction (human path), "
                                "not an approved machine BoundaryUpdate")
        return _Explanation(FlipOutcome.integrity_alert,
                            f"{origin_id}: extent no longer covers this hour with no "
                            "machine-attributed BoundaryUpdate to explain it")

    return _Explanation(FlipOutcome.integrity_alert,
                        f"{origin_id}: still contributing-eligible with no explanation "
                        "for the flip")


def _trace_flip(
    analyzer: str,
    hour_start: datetime,
    origin_ids: List[str],
    observations_by_origin: Dict[str, Observation],
    events_by_id: Dict[str, Event],
    current_capsule_ids: Set[str],
) -> CellFlip:
    hour_end = hour_start + timedelta(hours=1)

    if not origin_ids:
        return CellFlip(analyzer, hour_start, FlipOutcome.integrity_alert,
                        "no contributing ticket or capsule recorded for the prior "
                        "invalid hour", [])

    explanations = [
        _explain_capsule(oid, current_capsule_ids) if oid.startswith(CAPSULE_ID_PREFIX)
        else _explain_one(oid, observations_by_origin, events_by_id, hour_start, hour_end)
        for oid in origin_ids
    ]
    detail = "; ".join(e.detail for e in explanations)

    if all(e.outcome is FlipOutcome.silent_pass for e in explanations):
        outcome = FlipOutcome.silent_pass
    elif all(e.outcome is not FlipOutcome.integrity_alert for e in explanations):
        outcome = FlipOutcome.machine_informational
    else:
        outcome = FlipOutcome.integrity_alert

    return CellFlip(analyzer, hour_start, outcome, detail, origin_ids)


def diff_grids(
    prior_cells: List[GridCell],
    current_cells: List[GridCell],
    events: List[Event],
    run_date: date,
    late_x_threshold_days: int,
    site_timezone: str,
    current_capsules: List[Capsule] = (),
) -> DiffResult:
    """Pure function. `prior_cells` == [] means first run — everything is
    reported new, no flip-tracing (nothing to diff against).

    `current_capsules` is tonight's raw capsule pull — the same list the
    caller already read to build `current_cells`. It is only consulted for
    unticketed capsule-provenance contributors (ids prefixed `CAP:`) to
    check identity-unchanged-across-both-pulls; ticket-based tracing
    doesn't use it at all."""
    if not prior_cells:
        return DiffResult(is_first_run=True, new_cell_count=len(current_cells))

    prior_by_key: Dict[tuple, GridCell] = {(c.Analyzer, c.HourStartUTC): c for c in prior_cells}
    current_by_key: Dict[tuple, GridCell] = {(c.Analyzer, c.HourStartUTC): c for c in current_cells}

    observations_by_origin = {o.origin_event_id: o for o in fold(events)}
    events_by_id = {e.EventID: e for e in events}
    current_capsule_ids = {capsule_provenance_id(c) for c in current_capsules}

    result = DiffResult(is_first_run=False, new_cell_count=0)

    for (analyzer, hour_start), prior in prior_by_key.items():
        current = current_by_key.get((analyzer, hour_start))
        if current is None:
            continue
        if prior.Valid is CellValid.invalid and current.Valid is CellValid.valid:
            flip = _trace_flip(analyzer, hour_start, prior.ContributingEventIDs,
                               observations_by_origin, events_by_id, current_capsule_ids)
            {
                FlipOutcome.silent_pass: result.silent_passes,
                FlipOutcome.machine_informational: result.machine_informational,
                FlipOutcome.integrity_alert: result.integrity_alerts,
            }[flip.outcome].append(flip)

    for (analyzer, hour_start), current in current_by_key.items():
        if current.Valid is not CellValid.invalid:
            continue
        prior = prior_by_key.get((analyzer, hour_start))
        if prior is not None and prior.Valid is CellValid.invalid:
            continue  # not new
        result.new_invalid_cells.append(NewInvalidCell(analyzer, hour_start))
        local_day = _local_date(hour_start, site_timezone)
        age_days = (run_date - local_day).days
        if age_days > late_x_threshold_days:
            result.late_arrivals.append(LateArrival(analyzer, hour_start, age_days))

    return result
