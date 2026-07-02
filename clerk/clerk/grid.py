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

FLAGGED, not blocking: the capsule-match for dismissal subtraction is
ANALYZER-ONLY, not analyzer+DetectionClass — fold.py's Observation output
doesn't carry DetectionClass (not in its agreed output field list), so
grid.py can't key on it without a schema addition. Safe for every fixture
in this repo today (CEMS-001's two classes never time-overlap), but is a
real gap if an analyzer ever runs two overlapping-in-time detection
classes concurrently. Flag for Ryan if that becomes a live scenario.

FLAGGED, not blocking: no daily-calibration event source exists yet in any
fixture or schema. HourContext.failed_cal_at/passing_cal_at are always
None here — branch (iv) never fires via build_grid until that ingestion
path is defined. Cells simply route through the other branches.

Operating gate is source-agnostic by construction (spec): OperatingWindow
carries no "how do we know this" field — a continuous-signal unit and a
unit whose "operating" state is asserted via manual capsule (Lube Flare)
flow through the exact same Unit/StartUTC/EndUTC rows and the exact same
code path here. No branching on source anywhere in this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Tuple
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
)

Interval = Tuple[datetime, datetime]

_EXCLUDED_STATUSES = frozenset({Status.dismissed, Status.withdrawn, Status.superseded})


# ---------------------------------------------------------------------------
# Guarantee A — the folded-window contribution filter (fold.py handoff)
# ---------------------------------------------------------------------------

def _contributing(observations: List[Observation]):
    """Yield (analyzer, interval, observation) for every observation whose
    extent counts toward its analyzers' union. Guarantee A: every status
    EXCEPT Dismissed/Withdrawn/Superseded contributes — Needs review counts
    exactly like Confirmed."""
    for obs in observations:
        if obs.status in _EXCLUDED_STATUSES:
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
) -> List[Tuple[str, Interval]]:
    """(analyzer, signed_dismissal_extent) for every SeeqDetection-origin
    Dismissed observation. TechEntry-origin dismissals are excluded — they
    have no capsule counterpart and must not cancel an unrelated ticket's
    detected invalid time at the same analyzer."""
    out: List[Tuple[str, Interval]] = []
    for obs in observations:
        if obs.status is not Status.dismissed or obs.signed_dismissal_extent is None:
            continue
        if origin_types.get(obs.origin_event_id) is not EventType.SeeqDetection:
            continue
        for analyzer in obs.analyzers:
            out.append((analyzer, obs.signed_dismissal_extent))
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
    capsules_by_analyzer: Dict[str, List[Interval]],
    jitter_minutes: int,
) -> Dict[str, List[Interval]]:
    """Minus signed-dismissal extents, only where a live capsule still
    matches (jitter tolerance). Subtracts exactly the SIGNED extent, never
    the capsule's own (possibly wider) interval — any excess beyond the
    signed extent stays invalid automatically, since it was never removed."""
    out = {a: list(v) for a, v in detected_windows.items()}
    for analyzer, signed_extent in _signed_dismissals(observations, origin_types):
        if analyzer not in out:
            continue
        if _capsule_still_matches(signed_extent, capsules_by_analyzer.get(analyzer, []), jitter_minutes):
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

def _provenance_index(observations: List[Observation]) -> List[Tuple[str, Interval, str]]:
    return [(analyzer, interval, obs.origin_event_id)
            for analyzer, interval, obs in _contributing(observations)]


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
) -> List[GridCell]:
    """Pure function: fixtures (already read) + an explicit [window_start,
    window_end) hour-aligned UTC range -> the grid. Which range to evaluate
    on a given run (LookbackMonths etc.) is run.py's orchestration concern
    (Step 6), not this module's."""
    observations = fold(events)
    origin_types = _origin_types(events)

    manual_windows, detected_windows = _manual_and_detected_windows(observations, origin_types)

    capsules_by_analyzer = _capsules_by_analyzer(capsules)
    for analyzer, intervals in capsules_by_analyzer.items():
        detected_windows.setdefault(analyzer, []).extend(intervals)
    detected_windows = apply_dismissal_subtraction(
        detected_windows, observations, origin_types, capsules_by_analyzer,
        config.JitterToleranceMin)

    for analyzer, intervals in _qa_windows_by_analyzer(qa_windows).items():
        manual_windows.setdefault(analyzer, []).extend(intervals)

    operating_by_unit = _operating_by_unit(operating_windows)
    provenance = _provenance_index(observations)

    cells: List[GridCell] = []
    for au in analyzer_units:
        unit_windows = operating_by_unit.get(au.Unit, [])
        hour = window_start
        while hour < window_end:
            hour_end = hour + timedelta(hours=1)
            ctx = HourContext(
                analyzer=au.Analyzer,
                hour_start=hour,
                seeq_covered=au.SeeqCovered,
                operating=_clip(unit_windows, hour, hour_end),
                manual_qa_windows=_clip(manual_windows.get(au.Analyzer, []), hour, hour_end),
                detected_invalid_windows=_clip(detected_windows.get(au.Analyzer, []), hour, hour_end),
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
