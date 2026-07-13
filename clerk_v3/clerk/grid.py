"""
Steps 4-5 — grid.py (union with provenance, then per-hour rule evaluation).
Consumes fold.py's Observations and drives rules.py; owns no regulatory
logic of its own.

Step 4 union (spec): invalid time per analyzer =
    live detections (capsules.csv) ∪ folded manual/confirmation windows ∪
    QA/OOC windows (qa_windows.csv)
  MINUS signed-dismissal extents, only where a live capsule still matches
  the signed extent (jitter tolerance) — any excess beyond the signed
  extent stays invalid.

Guarantee A (fold.py handoff, Ryan 2026-07-02): "folded manual/confirmation
windows" is every folded Observation regardless of status, EXCEPT
Dismissed/Withdrawn/Superseded — Needs review counts exactly like
Confirmed. `contributing_observation_windows` is the direct, independently
testable implementation of this filter; nothing downstream may re-narrow
it to "only Confirmed counts."

Two sub-sources of "folded windows", split by ORIGIN event type (looked up
from the raw events list — fold.py's Observation does not carry origin
type, so grid.py derives it from clerk.fold.ORIGIN_TYPES):
  - TechEntry-origin  -> manual_qa_windows  (branch (iii)(A) TRIGGER — "a
    manual entry IS a QA/maintenance hour," per calc manual §1)
  - SeeqDetection-origin -> detected_invalid_windows (subtract-only, never
    a trigger — routes through the normal branch like any other detected
    invalidity)
qa_windows.csv always joins manual_qa_windows (QA activity is inherently
maintenance-triggering, never dismissible — no subtraction path for it).

Why the dismissal subtraction only touches detected_invalid_windows: the
folded-window terms are ALREADY Guarantee-A-filtered (Dismissed contributes
nothing there), so there is nothing left there to subtract. Capsules,
however, are ticket-status-agnostic by design ("tickets are not math
inputs") — a dismissed episode's raw capsule keeps flowing into the union
independent of what happened to its ticket. The signed-dismissal
subtraction is what actually implements "an approved dismissal removes the
hours," conditioned on a live capsule still corroborating it.

Origin-type guard on the subtraction (grid.py's own addition, not asked for
by name but required for correctness): only SeeqDetection-origin Dismissed
observations feed the subtraction list. A TechEntry-origin observation
that happens to be Dismissed has no capsule counterpart at all (manual
entries never have one) — including it would risk canceling an unrelated
SeeqDetection ticket's genuinely-invalid capsule time at the same analyzer
via the analyzer-only matching below.

RESOLVED 2026-07-02 (was flagged): the capsule-match for dismissal
subtraction is now analyzer+DetectionClass, not analyzer-only — fold.py's
Observation carries `detection_class` (sourced from the origin event's
DetectionClass column). A dismissal signed against one class can no longer
be corroborated by an unrelated class's live capsule at the same analyzer.

RESOLVED 2026-07-02 (was flagged, diff.py's provenance gap): every
GridCell.ContributingEventIDs now cites raw capsule contributions too, not
only folded Observations — `capsule_provenance_id` gives each capsule a
deterministic synthetic id (analyzer+class+start+end, prefixed `CAP:` to
stay distinguishable from real EventIDs). This is what lets diff.py
resolve an hour invalid purely from an unticketed live capsule as silent
(unchanged detection, nothing to approve) instead of an unconditional
INTEGRITY ALERT.

RESOLVED (F3, was flagged): the daily-calibration event source now exists —
validations.csv → ValidationEvent → build_grid's `validations` parameter.
A failed validation in an hour sets failed_cal_at (branch (iv) fires); a
subsequent in-hour pass sets passing_cal_at for (iv)'s recovery test. The
backdating path (validation_invalidation_windows) invalidates everything
between the last PASSING validation event and the failure instant.

Operating gate is source-agnostic by construction (spec): OperatingWindow
carries no "how do we know this" field — a continuous-signal unit and a
unit whose "operating" state is asserted via manual capsule (Lube Flare)
flow through the exact same Unit/StartUTC/EndUTC rows and the exact same
code path here. No branching on source anywhere in this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from clerk.fold import ORIGIN_TYPES, Observation, Status, fold
from clerk.rules import (
    HourContext,
    evaluate_hour,
    intervals_overlap,
    subtract_intervals,
)
from clerk.schemas import (
    AnalyzerUnit,
    Capsule,
    CellValid,
    Event,
    EventType,
    GridCell,
    OperatingWindow,
    QAWindow,
    SiteConfig,
    ValidationEvent,
)

# W10 type alias
# v3 Gap #1: the rollup is keyed by (Unit, Obligation), not by Unit alone —
# each pollutant obligation rolls up independently.
SourceDownHours = Dict[Tuple[str, str], List[datetime]]

Interval = Tuple[datetime, datetime]

_EXCLUDED_STATUSES = frozenset({Status.dismissed, Status.withdrawn, Status.superseded})


# ---------------------------------------------------------------------------
# Guarantee A — the folded-window contribution filter (fold.py handoff)
# ---------------------------------------------------------------------------

def _contributing(observations: List[Observation]):
    """Yield (analyzer, interval, observation) for every observation whose
    extent counts toward its analyzers' union. Guarantee A: every status
    EXCEPT Dismissed/Withdrawn/Superseded contributes — Needs review counts
    exactly like Confirmed.

    T8 (Doc 50): a START-ONLY entry (no ExtentEndUTC — e.g. a logged
    'filter change' marker) is a recorded fact but NOT a window: it
    contributes no invalid time and must never be read as open-ended
    (infinite) downtime. Same guard for a missing start. The observation
    itself still exists in the fold and the adjudicated output."""
    for obs in observations:
        if obs.status in _EXCLUDED_STATUSES:
            continue
        if obs.extent_start_utc is None or obs.extent_end_utc is None:
            continue
        interval = (obs.extent_start_utc, obs.extent_end_utc)
        for analyzer in obs.analyzers:
            yield analyzer, interval, obs


def contributing_observation_windows(observations: List[Observation]) -> Dict[str, List[Interval]]:
    """Public, directly testable Guarantee-A filter: every folded
    observation contributes its current extent to its analyzers' union
    UNLESS Dismissed/Withdrawn/Superseded — Needs review counts exactly
    like Confirmed. No "only Confirmed counts" filter, here or anywhere
    downstream."""
    out: Dict[str, List[Interval]] = {}
    for analyzer, interval, _obs in _contributing(observations):
        out.setdefault(analyzer, []).append(interval)
    return out


def _origin_types(events: List[Event]) -> Dict[str, EventType]:
    return {e.EventID: e.EventType for e in events if e.EventType in ORIGIN_TYPES}


# ---------------------------------------------------------------------------
# T2 wiring (Doc 50) — reason code → resolved CFR paragraph per hour
# ---------------------------------------------------------------------------

def _governing_reason(obs: Observation, events_by_id: Dict[str, Event]) -> Optional[str]:
    """The observation's governing reason code: the LATEST non-blank
    ReasonCode in its contributing-event history (same rule run.py uses
    for adjudicated_condition rows)."""
    for eid in reversed(obs.contributing_event_ids):
        e = events_by_id.get(eid)
        if e is not None and e.ReasonCode.strip():
            return e.ReasonCode.strip()
    return None


def _reason_paragraph_windows(
    observations: List[Observation],
    events_by_id: Dict[str, Event],
    config: SiteConfig,
) -> Dict[str, List[Tuple[Interval, str]]]:
    """Per analyzer: (interval, paragraph-label) for every contributing
    observation whose governing reason code maps in ReasonParagraphMap.
    This is what makes paragraph selection reason-driven end to end
    (Doc 50 T2): the same invalid window folds under (iii) when the reason
    is QA-01 but under (i) when it is MM-01."""
    out: Dict[str, List[Tuple[Interval, str]]] = {}
    for analyzer, interval, obs in _contributing(observations):
        reason = _governing_reason(obs, events_by_id)
        paragraph = config.ReasonParagraphMap.get(reason) if reason else None
        if paragraph:
            out.setdefault(analyzer, []).append((interval, paragraph))
    return out


def _resolve_hour_paragraph(
    windows: List[Tuple[Interval, str]],
    hour_start: datetime,
    hour_end: datetime,
) -> Optional[str]:
    """The hour's resolved paragraph: the single distinct mapped label among
    overlapping reason-mapped observations.
    # FLAG: provisional — when observations with CONFLICTING mapped
    # paragraphs overlap the same hour, resolution falls back to
    # auto-selection (returns None). Which reason governs a contested hour
    # is an open design question awaiting Ryan/SME."""
    labels = {p for (s, e), p in windows if s < hour_end and hour_start < e}
    if len(labels) == 1:
        return labels.pop()
    return None


def _manual_and_detected_windows(
    observations: List[Observation],
    origin_types: Dict[str, EventType],
) -> Tuple[Dict[str, List[Interval]], Dict[str, List[Interval]]]:
    manual: Dict[str, List[Interval]] = {}
    detected: Dict[str, List[Interval]] = {}
    for analyzer, interval, obs in _contributing(observations):
        bucket = manual if origin_types.get(obs.origin_event_id) is EventType.TechEntry else detected
        bucket.setdefault(analyzer, []).append(interval)
    return manual, detected


# ---------------------------------------------------------------------------
# Signed-dismissal subtraction (only detected_invalid_windows; see docstring)
# ---------------------------------------------------------------------------

def _signed_dismissals(
    observations: List[Observation],
    origin_types: Dict[str, EventType],
) -> List[Tuple[str, Optional[str], Interval]]:
    """(analyzer, detection_class, signed_dismissal_extent) for every
    SeeqDetection-origin Dismissed observation. TechEntry-origin dismissals
    are excluded — they have no capsule counterpart and must not cancel an
    unrelated ticket's detected invalid time at the same analyzer."""
    out: List[Tuple[str, Optional[str], Interval]] = []
    for obs in observations:
        if obs.status is not Status.dismissed or obs.signed_dismissal_extent is None:
            continue
        if origin_types.get(obs.origin_event_id) is not EventType.SeeqDetection:
            continue
        for analyzer in obs.analyzers:
            out.append((analyzer, obs.detection_class, obs.signed_dismissal_extent))
    return out


def _capsule_still_matches(
    signed_extent: Interval,
    capsules: List[Interval],
    jitter_minutes: int,
) -> bool:
    """A live capsule 'still matches' the signed extent if it overlaps, or
    is separated from it by no more than the jitter tolerance."""
    sds, sde = signed_extent
    slack = timedelta(minutes=jitter_minutes)
    return any(cs <= sde + slack and ce >= sds - slack for cs, ce in capsules)


def apply_dismissal_subtraction(
    detected_windows: Dict[str, List[Interval]],
    observations: List[Observation],
    origin_types: Dict[str, EventType],
    capsules_by_analyzer_class: Dict[Tuple[str, Optional[str]], List[Interval]],
    jitter_minutes: int,
) -> Dict[str, List[Interval]]:
    """Minus signed-dismissal extents, only where a live capsule of the
    SAME analyzer+DetectionClass still matches (jitter tolerance). A
    dismissal signed against one class is never corroborated by a
    different class's capsule, even if it overlaps the same analyzer at
    the same time. Subtracts exactly the SIGNED extent, never the
    capsule's own (possibly wider) interval — any excess beyond the signed
    extent stays invalid automatically, since it was never removed."""
    out = {a: list(v) for a, v in detected_windows.items()}
    for analyzer, detection_class, signed_extent in _signed_dismissals(observations, origin_types):
        if analyzer not in out:
            continue
        capsules = capsules_by_analyzer_class.get((analyzer, detection_class), [])
        if _capsule_still_matches(signed_extent, capsules, jitter_minutes):
            out[analyzer] = subtract_intervals(out[analyzer], [signed_extent])
    return out


# ---------------------------------------------------------------------------
# Per-source grouping helpers
# ---------------------------------------------------------------------------

def _capsules_by_analyzer(capsules: List[Capsule]) -> Dict[str, List[Interval]]:
    out: Dict[str, List[Interval]] = {}
    for c in capsules:
        out.setdefault(c.Analyzer, []).append((c.CapsuleStartUTC, c.CapsuleEndUTC))
    return out


def _capsules_by_analyzer_class(capsules: List[Capsule]) -> Dict[Tuple[str, Optional[str]], List[Interval]]:
    """Keyed by (Analyzer, DetectionClass) for dismissal-subtraction
    matching — distinct from `_capsules_by_analyzer`, which stays flat
    since the general detected-invalid union doesn't care about class."""
    out: Dict[Tuple[str, Optional[str]], List[Interval]] = {}
    for c in capsules:
        key = (c.Analyzer, c.DetectionClass or None)
        out.setdefault(key, []).append((c.CapsuleStartUTC, c.CapsuleEndUTC))
    return out


def _qa_windows_by_analyzer(qa_windows: List[QAWindow]) -> Dict[str, List[Interval]]:
    out: Dict[str, List[Interval]] = {}
    for w in qa_windows:
        out.setdefault(w.Analyzer, []).append((w.StartUTC, w.EndUTC))
    return out


def _operating_by_unit(windows: List[OperatingWindow]) -> Dict[str, List[Interval]]:
    out: Dict[str, List[Interval]] = {}
    for w in windows:
        out.setdefault(w.Unit, []).append((w.StartUTC, w.EndUTC))
    return out


def _clip(intervals: List[Interval], lo: datetime, hi: datetime) -> List[Interval]:
    out: List[Interval] = []
    for s, e in intervals:
        cs, ce = max(s, lo), min(e, hi)
        if cs < ce:
            out.append((cs, ce))
    return out


def _local_label(hour_start_utc: datetime, tz_name: str) -> str:
    return hour_start_utc.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

# Prefix distinguishes synthetic capsule provenance ids from real EventIDs
# in ContributingEventIDs — an unticketed capsule contribution is real
# provenance (spec: "every resulting interval carries source event IDs"),
# not something to omit just because no ticket exists for it yet.
CAPSULE_ID_PREFIX = "CAP:"


def capsule_provenance_id(c: Capsule) -> str:
    """Deterministic synthetic identifier: analyzer+class+start+end. Two
    capsules with an identical id ARE the same physical detection, unchanged
    — this identity is exactly what diff.py's silent-pass-on-stable-
    unticketed-capsule check relies on (Ryan, 2026-07-02)."""
    return (f"{CAPSULE_ID_PREFIX}{c.Analyzer}:{c.DetectionClass}:"
            f"{c.CapsuleStartUTC.isoformat()}:{c.CapsuleEndUTC.isoformat()}")


def _provenance_index(
    observations: List[Observation],
    capsules: List[Capsule],
) -> List[Tuple[str, Interval, str]]:
    obs_provenance = [(analyzer, interval, obs.origin_event_id)
                      for analyzer, interval, obs in _contributing(observations)]
    capsule_provenance = [
        (c.Analyzer, (c.CapsuleStartUTC, c.CapsuleEndUTC), capsule_provenance_id(c))
        for c in capsules
    ]
    return obs_provenance + capsule_provenance


def _contributing_ids(
    provenance: List[Tuple[str, Interval, str]],
    analyzer: str,
    hour_start: datetime,
    hour_end: datetime,
) -> List[str]:
    return sorted({oid for a, (s, e), oid in provenance
                   if a == analyzer and intervals_overlap([(s, e)], hour_start, hour_end)})


# ---------------------------------------------------------------------------
# Step 4 + 5 driver
# ---------------------------------------------------------------------------

def build_grid(
    events: List[Event],
    capsules: List[Capsule],
    operating_windows: List[OperatingWindow],
    analyzer_units: List[AnalyzerUnit],
    qa_windows: List[QAWindow],
    config: SiteConfig,
    window_start: datetime,
    window_end: datetime,
    validations: Optional[List[ValidationEvent]] = None,
) -> List[GridCell]:
    """Pure function: fixtures (already read) + an explicit [window_start,
    window_end) hour-aligned UTC range -> the grid. Which range to evaluate
    on a given run (LookbackMonths etc.) is run.py's orchestration concern
    (Step 6), not this module's."""
    validations = validations or []
    observations = fold(events)
    origin_types = _origin_types(events)

    manual_windows, detected_windows = _manual_and_detected_windows(observations, origin_types)

    capsules_by_analyzer = _capsules_by_analyzer(capsules)
    for analyzer, intervals in capsules_by_analyzer.items():
        detected_windows.setdefault(analyzer, []).extend(intervals)
    detected_windows = apply_dismissal_subtraction(
        detected_windows, observations, origin_types, _capsules_by_analyzer_class(capsules),
        config.JitterToleranceMin)

    # F3: validation-failure backdating joins the union AFTER the dismissal
    # subtraction — a failed daily validation is a measured fact, never
    # dismissible via a capsule-matched signed extent.
    for analyzer, intervals in validation_invalidation_windows(validations).items():
        detected_windows.setdefault(analyzer, []).extend(intervals)

    for analyzer, intervals in _qa_windows_by_analyzer(qa_windows).items():
        manual_windows.setdefault(analyzer, []).extend(intervals)

    validations_by_analyzer: Dict[str, List[ValidationEvent]] = {}
    for v in validations:
        validations_by_analyzer.setdefault(v.Analyzer, []).append(v)

    # T2: reason-driven paragraph selection, resolved per analyzer x hour.
    events_by_id = {e.EventID: e for e in events}
    reason_windows = _reason_paragraph_windows(observations, events_by_id, config)

    operating_by_unit = _operating_by_unit(operating_windows)
    provenance = _provenance_index(observations, capsules)

    cells: List[GridCell] = []
    for au in analyzer_units:
        unit_windows = operating_by_unit.get(au.Unit, [])
        hour = window_start
        while hour < window_end:
            hour_end = hour + timedelta(hours=1)
            # W9: diluent propagation — monitor effective downtime = own downtime
            # OR diluent-down. A diluent outage (O2/CO2) renders its dependent
            # pollutant monitors invalid for the same interval, even if the
            # pollutant analyzer itself shows no direct detected-invalid window.
            own_detected = detected_windows.get(au.Analyzer, [])
            if au.DiluentBasis:
                own_detected = own_detected + detected_windows.get(au.DiluentBasis, [])
            # F3: a failed daily validation in this hour selects branch (iv);
            # a subsequent pass in the same hour enables (iv)'s recovery test.
            failed_cal_at = passing_cal_at = None
            for v in validations_by_analyzer.get(au.Analyzer, []):
                if hour <= v.ValidatedAtUTC < hour_end:
                    if not v.Passed and (failed_cal_at is None or v.ValidatedAtUTC < failed_cal_at):
                        failed_cal_at = v.ValidatedAtUTC
            if failed_cal_at is not None:
                for v in validations_by_analyzer.get(au.Analyzer, []):
                    if v.Passed and failed_cal_at < v.ValidatedAtUTC < hour_end:
                        if passing_cal_at is None or v.ValidatedAtUTC < passing_cal_at:
                            passing_cal_at = v.ValidatedAtUTC
            ctx = HourContext(
                analyzer=au.Analyzer,
                hour_start=hour,
                seeq_covered=au.SeeqCovered,
                operating=_clip(unit_windows, hour, hour_end),
                manual_qa_windows=_clip(manual_windows.get(au.Analyzer, []), hour, hour_end),
                detected_invalid_windows=_clip(own_detected, hour, hour_end),
                failed_cal_at=failed_cal_at,
                passing_cal_at=passing_cal_at,
                resolved_paragraph=_resolve_hour_paragraph(
                    reason_windows.get(au.Analyzer, []), hour, hour_end),
            )
            valid, rule_applied = evaluate_hour(ctx)
            operated = sum((e - s for s, e in ctx.operating), timedelta(0))
            cells.append(GridCell(
                Analyzer=au.Analyzer,
                HourStartUTC=hour,
                HourLocalLabel=_local_label(hour, config.SiteTimeZoneIANA),
                OperatingFraction=operated / timedelta(hours=1),
                Valid=valid,
                RuleApplied=rule_applied,
                ContributingEventIDs=_contributing_ids(provenance, au.Analyzer, hour, hour_end),
            ))
            hour += timedelta(hours=1)
    return cells


# ---------------------------------------------------------------------------
# W7 — down-hour predicate
# ---------------------------------------------------------------------------

def is_down_hour(cell: GridCell) -> bool:
    """Return True only when the cell represents a compliance down-hour.

    A down-hour is one where the unit WAS operating but lacked sufficient
    valid monitoring data (CellValid.invalid). CellValid.not_operating and
    CellValid.not_assessed are NOT down-hours:
    - not_operating: the unit wasn't running — excluded from the DAR
      denominator entirely, not a deficiency to report.
    - not_assessed: detection coverage is absent — an open question, not
      a confirmed deficiency.
    """
    return cell.Valid is CellValid.invalid


# ---------------------------------------------------------------------------
# W10/F2 — source rollup
# ---------------------------------------------------------------------------

def source_down_hours(
    analyzer_units: List[AnalyzerUnit],
    cells: List[GridCell],
) -> SourceDownHours:
    """W10/F2 + v3 Gap #1: Source-level downtime = intersection (AND) of the
    IN-COVERAGE monitor downtimes WITHIN EACH OBLIGATION (D8: intersect by
    pollutant, never across a whole unit).

    v3 change (Gap #1): monitors are grouped by (Unit, Obligation), not by
    Unit alone. A unit that carries a NOx obligation and an O2 obligation
    rolls each up independently — so a valid O2 monitor can never "cover" a
    NOx outage, and vice-versa. Redundant monitors that share one obligation
    (a permanent + a temp NOx analyzer) still intersect together, exactly as
    before. Monitors with a blank Obligation are grouped together per unit,
    reproducing the pre-v3 per-unit intersection.

    Coverage gate (F2): a monitor counts toward its obligation only for hours
    inside its [InServiceDateUTC, OOSDateUTC) window. A temp not yet deployed
    (hour < InServiceDate) or already pulled (hour >= OOSDate) is ABSENT from
    that hour's intersection — neither "valid" nor "down."

    An obligation is down for an hour iff EVERY in-coverage monitor of that
    obligation shows CellValid.invalid AND at least one in-coverage monitor
    exists. Down evidence can come from any invalidity path — a manual (List
    A) entry with no capsule participates exactly like a capsule-detected one.

    Additional exclusions per hour:
    - any in-coverage monitor lacking a grid cell → hour skipped (outside
      the build window; not claimable as source-down);
    - any in-coverage monitor not_operating → the unit wasn't running;
      excluded from the DAR denominator, not a deficiency;
    - any in-coverage monitor not_assessed → defensive only.

    Returns {(unit, obligation): [hour_start_utc, ...]} ascending per group.
    Groups with no down hours are absent from the result.
    """
    group_to_monitors: Dict[Tuple[str, str], List[AnalyzerUnit]] = {}
    for au in analyzer_units:
        group_to_monitors.setdefault((au.Unit, au.Obligation), []).append(au)

    cell_status: Dict[Tuple[str, datetime], CellValid] = {
        (c.Analyzer, c.HourStartUTC): c.Valid for c in cells
    }
    all_hours = sorted({c.HourStartUTC for c in cells})

    result: SourceDownHours = {}
    for group, roster in group_to_monitors.items():
        down: List[datetime] = []
        for hour in all_hours:
            in_cov = [au for au in roster if au.in_coverage(hour)]
            # No in-coverage monitor at all: nothing can assert source-down.
            if not in_cov:
                continue
            statuses = [cell_status.get((au.Analyzer, hour)) for au in in_cov]
            # Skip if any in-coverage monitor has no cell (outside build window)
            if None in statuses:
                continue
            if any(s in (CellValid.not_operating, CellValid.not_assessed)
                   for s in statuses):
                continue
            # Obligation-down = ALL in-coverage monitors of the obligation
            # simultaneously invalid
            if all(s is CellValid.invalid for s in statuses):
                down.append(hour)
        if down:
            result[group] = down
    return result


# ---------------------------------------------------------------------------
# W6/F3 — backdate to the last passing VALIDATION EVENT (not last valid cell)
# ---------------------------------------------------------------------------

# FLAG: provisional — when a validation FAILS with no prior passing
# validation on record, there is no anchor to backdate to. Fallback: one
# daily-validation period (24 h) before the failure. Awaiting Ryan's ruling
# on the correct no-anchor behavior (full-history invalidation vs 24 h).
_NO_ANCHOR_FALLBACK = timedelta(hours=24)


def backdate_to_last_passing(
    analyzer: str,
    validations: List[ValidationEvent],
    at: datetime,
) -> Optional[datetime]:
    """F3: return the timestamp of the most recent PASSING daily-validation
    event for the analyzer at or before `at`, or None if no passing
    validation exists on record.

    This anchors invalidation to the validation-EVENT stream, not to data
    validity: between two daily validations the data can read valid every
    hour, yet a failed validation invalidates everything back to the prior
    passing validation event — up to a full day earlier. (The previous form
    returned the most recent CellValid.valid grid cell, i.e. the last hour
    with valid DATA — the wrong anchor, and it was never wired into the
    invalidation path. It now is: see validation_invalidation_windows and
    build_grid.)
    """
    best: Optional[datetime] = None
    for v in validations:
        if v.Analyzer == analyzer and v.Passed and v.ValidatedAtUTC <= at:
            if best is None or v.ValidatedAtUTC > best:
                best = v.ValidatedAtUTC
    return best


def validation_invalidation_windows(
    validations: List[ValidationEvent],
) -> Dict[str, List[Interval]]:
    """F3: the invalidation path the backdate anchor feeds. For every FAILED
    validation at time T, emit the invalid window (anchor, T) where anchor
    is the most recent passing validation at or before T. The failure hour
    itself is governed by branch (iv) — build_grid sets failed_cal_at on
    that hour's context — so this window intentionally stops at T.

    Overlapping windows from consecutive failures merge naturally in the
    downstream interval union.
    """
    out: Dict[str, List[Interval]] = {}
    for v in validations:
        if v.Passed:
            continue
        anchor = backdate_to_last_passing(v.Analyzer, validations, v.ValidatedAtUTC)
        if anchor is None:
            # FLAG: provisional no-anchor fallback — see _NO_ANCHOR_FALLBACK.
            anchor = v.ValidatedAtUTC - _NO_ANCHOR_FALLBACK
        if anchor < v.ValidatedAtUTC:
            out.setdefault(v.Analyzer, []).append((anchor, v.ValidatedAtUTC))
    return out
