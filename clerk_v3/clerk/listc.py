"""
List C enrichment — the standalone compliance record (pure, no I/O).

One record per resolved downtime episode per analyzer: everything a
manager or auditor needs without joining to another table — the resolved
extent actually used, reason/note/corrective action from List A, which
source window won (A, B, or a picked value), the approver + decision +
timestamp, the governing CFR paragraphs, the hourly resolution, and full
provenance.

Discipline (unchanged from the rest of the clerk): append-only inputs,
re-derived on every fold, pure and idempotent — build_list_c is the only
thing that produces List C records, and calling it twice on the same
inputs yields identical output. It consumes the SAME fold and the SAME
grid cells the nightly run produces; it changes no validity math.

Episode formation: within an analyzer, List A observations (TechEntry
origin), List B observations (SeeqDetection origin), and raw capsules are
grouped into one episode by TEMPORAL OVERLAP chaining (consistent with the
reconciliation module's stable-identity rule: match by overlap).

A/B concurrence: List A's window(s) agree with List B's when the merged
interval sets match endpoint-for-endpoint within the jitter tolerance.
Anything else — including B showing an internal valid gap that A's single
block papers over — is a DISAGREEMENT and the record is `pending` until a
WindowPick event resolves it.

States:
    auto-approved  A/B concur (approver "auto"), or only one source exists
                   (nothing to disagree about)
    pending        A and B disagree and no pick event has folded in
    approved       a WindowPick event exists — approver name, decision,
                   and timestamp are carried on the record

Pending semantics (# FLAG: provisional — policy decision for Ryan):
while a disagreement is pending, ResolvedWindows is the UNION of both
claims — the same union the hourly grid already uses (Guarantee A:
unadjudicated downtime counts). Both raw claims stay visible on the
record (WindowA / WindowsB), so nothing is silently collapsed and B's
internal valid gaps remain on file; but the pending MINUTES total counts
the union. If pending totals should instead count B-only (or A-only),
that is a one-line change here — it does not touch the grid.

WindowPick never alters the hourly verdicts: it resolves the RECORD. If a
picked window should also flow into hourly validity, that is a Correction
event (already exists) — deliberate seam, flagged in the fix report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from clerk.fold import ORIGIN_TYPES, Observation, Status, fold
from clerk.grid import OOC_RULE, capsule_provenance_id

# SourceUsed label for an OOC (Appendix F §4.3.1) List C record.
OOC_SOURCE = "OOC (App F 4.3.1)"
from clerk.rules import merge_intervals
from clerk.schemas import (AnalyzerUnit, Capsule, CellValid, Event, EventType,
                           GridCell)

Interval = Tuple[datetime, datetime]

_EXCLUDED = frozenset({Status.dismissed, Status.withdrawn, Status.superseded})


@dataclass
class ListCRecord:
    Analyzer: str
    State: str                        # auto-approved | pending | approved
    ResolvedWindows: List[Interval]   # the extent(s) actually used
    ResolvedMinutes: float
    SourceUsed: str                   # concurrence (A=B) | A only | B only |
                                      # union (pending) | pick:use-A/use-B/corrected
    Disagreement: str                 # "" when A/B concur; else a description
    WindowA: List[Interval]           # List A's window(s), always preserved
    WindowsB: List[Interval]          # List B's interval(s), always preserved
    ReasonCode: str                   # from List A (latest non-blank)
    Note: str                         # from List A (latest non-blank Reason text)
    CorrectiveAction: str             # from List A (latest non-blank)
    ApproverName: str                 # "auto" on concurrence; else the Actor
    ApproverDecision: str             # "concurrence" | "" (pending) | the pick choice
    ApprovedAtUTC: Optional[datetime]
    GoverningParagraphs: List[str]    # distinct RuleApplied over the resolved hours
    DownHours: List[datetime]         # hourly resolution, kept
    ContributingRecords: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Episode grouping by temporal overlap
# ---------------------------------------------------------------------------

def _hull(intervals: List[Interval]) -> Interval:
    return (min(s for s, _ in intervals), max(e for _, e in intervals))


def _overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _group_by_overlap(items: List[Tuple[Interval, object]]) -> List[List[object]]:
    """Chain-group items whose intervals overlap (transitively)."""
    items = sorted(items, key=lambda it: it[0])
    groups: List[Tuple[Interval, List[object]]] = []
    for interval, payload in items:
        if groups and _overlaps(groups[-1][0], interval):
            hull, members = groups[-1]
            members.append(payload)
            groups[-1] = (_hull([hull, interval]), members)
        else:
            groups.append((interval, [payload]))
    return [members for _, members in groups]


# ---------------------------------------------------------------------------
# Concurrence
# ---------------------------------------------------------------------------

def _windows_agree(a: List[Interval], b: List[Interval], jitter_minutes: int) -> bool:
    """A and B concur iff their MERGED interval sets match endpoint-for-
    endpoint within jitter. A count mismatch (e.g. B carries an internal
    valid gap that A's single block does not) is a disagreement."""
    a, b = merge_intervals(a), merge_intervals(b)
    if len(a) != len(b):
        return False
    slack = timedelta(minutes=jitter_minutes)
    return all(abs(x[0] - y[0]) <= slack and abs(x[1] - y[1]) <= slack
               for x, y in zip(a, b))


def _describe_disagreement(a: List[Interval], b: List[Interval]) -> str:
    def fmt(ws):
        return " + ".join(f"{s:%H:%M}-{e:%H:%M}" for s, e in ws)
    parts = [f"A logs {fmt(merge_intervals(a))}; B detects {fmt(merge_intervals(b))}"]
    # Surface B's internal valid gaps explicitly — the thing a silent
    # collapse to A's block would erase.
    merged_b = merge_intervals(b)
    gaps = [(merged_b[i][1], merged_b[i + 1][0]) for i in range(len(merged_b) - 1)]
    if gaps:
        parts.append("B shows signal VALID during " + fmt(gaps))
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

def _latest_nonblank(obs_list: List[Observation], events_by_id: Dict[str, Event],
                     attr: str) -> str:
    best_at, best = None, ""
    for obs in obs_list:
        for eid in obs.contributing_event_ids:
            e = events_by_id.get(eid)
            if e is None or e.EventType is EventType.WindowPick:
                # pick events carry approval metadata, never the record's
                # reason/note/corrective-action content
                continue
            value = getattr(e, attr, "") or ""
            if value.strip() and (best_at is None or e.ActedAt >= best_at):
                best_at, best = e.ActedAt, value.strip()
    return best


def _contiguous_hours(hours: List[datetime]) -> List[Interval]:
    """Group hour-aligned datetimes into contiguous (start, end) windows,
    where end is the last hour + 1h. [] -> []."""
    hours = sorted(hours)
    out: List[Interval] = []
    for h in hours:
        if out and h == out[-1][1]:
            out[-1] = (out[-1][0], h + timedelta(hours=1))
        else:
            out.append((h, h + timedelta(hours=1)))
    return out


def _ooc_records(cells_by_analyzer: Dict[str, List[GridCell]]) -> List[ListCRecord]:
    """OOC (Appendix F §4.3.1) List C records. OOC-invalid hours carry no
    event or capsule of their own (they come from the validation stream), so
    without this the standalone record would show a clean analyzer while the
    grid shows it OOC-invalid. One record per contiguous run of OOC-invalid
    hours per analyzer, under the OOC (QA/QC) paragraph. Boundary hours that
    resolved VALID under OOC are not downtime and get no record."""
    out: List[ListCRecord] = []
    for analyzer, cells in cells_by_analyzer.items():
        ooc_down = sorted(c.HourStartUTC for c in cells
                          if c.Valid is CellValid.invalid and c.RuleApplied == OOC_RULE)
        if not ooc_down:
            continue
        for window in _contiguous_hours(ooc_down):
            hrs = [h for h in ooc_down if window[0] <= h < window[1]]
            out.append(ListCRecord(
                Analyzer=analyzer, State="auto-approved",
                ResolvedWindows=[window],
                ResolvedMinutes=(window[1] - window[0]).total_seconds() / 60.0,
                SourceUsed=OOC_SOURCE, Disagreement="",
                WindowA=[], WindowsB=[],
                ReasonCode="QA-01",
                Note="Out-of-control (Appendix F 4.3.1) — validation-driven "
                     "invalidation; whole window invalid end-to-end",
                CorrectiveAction="",
                ApproverName="auto", ApproverDecision="ooc",
                ApprovedAtUTC=None,
                GoverningParagraphs=[OOC_RULE],
                DownHours=hrs,
                ContributingRecords=[f"OOC:{analyzer}"],
            ))
    return out


def _propagated_records(
    analyzer_units: List[AnalyzerUnit],
    cells: List[GridCell],
    own_records: List[ListCRecord],
    cells_by_analyzer: Dict[str, List[GridCell]],
) -> List[ListCRecord]:
    """v3 Gap #2: a diluent outage propagates to its dependent analyzers in
    the hourly grid (build_grid W9), but those dependents have no event or
    capsule of their own, so they would carry NO List C record — the
    standalone record would say a dependent was fine while the grid shows it
    invalid. This emits a record for every dependent analyzer's
    PROPAGATED-only invalid hours (its grid-invalid hours not already
    covered by one of its own-source records), citing the diluent basis."""
    own_down_by_analyzer: Dict[str, set] = {}
    basis_records_by_analyzer: Dict[str, List[ListCRecord]] = {}
    for r in own_records:
        own_down_by_analyzer.setdefault(r.Analyzer, set()).update(r.DownHours)
        basis_records_by_analyzer.setdefault(r.Analyzer, []).append(r)

    out: List[ListCRecord] = []
    for au in analyzer_units:
        if not au.DiluentBasis:
            continue
        invalid_hours = {c.HourStartUTC for c in cells_by_analyzer.get(au.Analyzer, [])
                         if c.Valid is CellValid.invalid}
        propagated = sorted(invalid_hours - own_down_by_analyzer.get(au.Analyzer, set()))
        if not propagated:
            continue
        for window in _contiguous_hours(propagated):
            hrs = [h for h in propagated if window[0] <= h < window[1]]
            # Pull reason/note/provenance from the basis monitor's own record
            # that overlaps this window (the outage that caused propagation).
            basis_reason = basis_note = ""
            basis_prov: List[str] = []
            for br in basis_records_by_analyzer.get(au.DiluentBasis, []):
                if any(window[0] <= h < window[1] for h in br.DownHours):
                    basis_reason = basis_reason or br.ReasonCode
                    basis_note = basis_note or br.Note
                    basis_prov = sorted(set(basis_prov) | set(br.ContributingRecords))
            overlapping = [c for c in cells_by_analyzer.get(au.Analyzer, [])
                           if window[0] <= c.HourStartUTC < window[1]]
            out.append(ListCRecord(
                Analyzer=au.Analyzer, State="auto-approved",
                ResolvedWindows=[window],
                ResolvedMinutes=(window[1] - window[0]).total_seconds() / 60.0,
                SourceUsed=f"diluent-propagated (from {au.DiluentBasis})",
                Disagreement="",
                WindowA=[], WindowsB=[],
                ReasonCode=basis_reason,
                Note=(f"Invalid because diluent {au.DiluentBasis} was down"
                      + (f": {basis_note}" if basis_note else "")),
                CorrectiveAction="",
                ApproverName="auto", ApproverDecision="diluent-propagation",
                ApprovedAtUTC=None,
                GoverningParagraphs=sorted({c.RuleApplied for c in overlapping}),
                DownHours=hrs,
                ContributingRecords=[f"DILUENT:{au.DiluentBasis}"] + basis_prov,
            ))
    return out


def build_list_c(
    events: List[Event],
    capsules: List[Capsule],
    cells: List[GridCell],
    jitter_minutes: int = 5,
    analyzer_units: Optional[List[AnalyzerUnit]] = None,
) -> List[ListCRecord]:
    """events + capsules + the already-built hourly grid -> enriched List C.
    Pure function; deterministic ordering (analyzer, episode start).

    v3 Gap #2: pass analyzer_units to also emit records for diluent-
    PROPAGATED downtime on dependent analyzers (so the standalone record
    matches the hourly grid). Omitting analyzer_units preserves the pre-v3
    behavior exactly (own-source records only)."""
    observations = fold(events)
    events_by_id = {e.EventID: e for e in events}
    origin_types = {e.EventID: e.EventType for e in events if e.EventType in ORIGIN_TYPES}

    # Contributing items per analyzer: (interval, payload)
    per_analyzer: Dict[str, List[Tuple[Interval, Tuple[str, object]]]] = {}
    for obs in observations:
        if obs.status in _EXCLUDED:
            continue
        if obs.extent_start_utc is None or obs.extent_end_utc is None:
            continue  # start-only markers are recorded elsewhere, not windows
        kind = "A" if origin_types.get(obs.origin_event_id) is EventType.TechEntry else "B"
        interval = (obs.extent_start_utc, obs.extent_end_utc)
        for analyzer in obs.analyzers:
            per_analyzer.setdefault(analyzer, []).append((interval, (kind, obs)))
    for c in capsules:
        per_analyzer.setdefault(c.Analyzer, []).append(
            ((c.CapsuleStartUTC, c.CapsuleEndUTC), ("cap", c)))

    cells_by_analyzer: Dict[str, List[GridCell]] = {}
    for cell in cells:
        cells_by_analyzer.setdefault(cell.Analyzer, []).append(cell)

    records: List[ListCRecord] = []
    for analyzer in sorted(per_analyzer):
        for members in _group_by_overlap(per_analyzer[analyzer]):
            a_obs = [p for k, p in members if k == "A"]
            b_obs = [p for k, p in members if k == "B"]
            caps = [p for k, p in members if k == "cap"]

            windows_a = [(o.extent_start_utc, o.extent_end_utc) for o in a_obs]
            windows_b = merge_intervals(
                [(o.extent_start_utc, o.extent_end_utc) for o in b_obs]
                + [(c.CapsuleStartUTC, c.CapsuleEndUTC) for c in caps])

            provenance = sorted(
                {eid for o in a_obs + b_obs for eid in o.contributing_event_ids}
                | {capsule_provenance_id(c) for c in caps})

            # Pick event (winner-pick), if one folded in on the A side.
            pick = next((o for o in a_obs if o.pick_choice), None)

            if pick is not None:
                state, approver = "approved", pick.pick_approver or ""
                decision, approved_at = f"pick:{pick.pick_choice}", pick.pick_acted_at
                if pick.pick_choice == "use-A":
                    resolved, source = merge_intervals(windows_a), "pick:use-A"
                elif pick.pick_choice == "use-B":
                    resolved, source = windows_b, "pick:use-B"
                else:  # corrected — the pick event's own stated window
                    resolved = [pick.pick_extent] if pick.pick_extent else \
                        merge_intervals(windows_a + windows_b)
                    source = "pick:corrected"
                disagreement = (_describe_disagreement(windows_a, windows_b)
                                if windows_a and windows_b
                                and not _windows_agree(windows_a, windows_b, jitter_minutes)
                                else "")
            elif windows_a and windows_b:
                if _windows_agree(windows_a, windows_b, jitter_minutes):
                    state, approver, decision = "auto-approved", "auto", "concurrence"
                    approved_at = None
                    resolved, source = merge_intervals(windows_a), "concurrence (A=B)"
                    disagreement = ""
                else:
                    # FLAG: provisional — pending resolves to the UNION (the
                    # same union the hourly grid already counts, Guarantee A).
                    # Both raw claims stay on the record; see module docstring.
                    state, approver, decision, approved_at = "pending", "", "", None
                    resolved = merge_intervals(windows_a + windows_b)
                    source = "union (pending)"
                    disagreement = _describe_disagreement(windows_a, windows_b)
            elif windows_a:
                state, approver, decision, approved_at = "auto-approved", "auto", "single-source", None
                resolved, source, disagreement = merge_intervals(windows_a), "A only", ""
            else:
                state, approver, decision, approved_at = "auto-approved", "auto", "single-source", None
                resolved, source, disagreement = windows_b, "B only", ""

            minutes = sum((e - s).total_seconds() / 60.0 for s, e in resolved)

            overlapping = [cell for cell in cells_by_analyzer.get(analyzer, [])
                           if any(cell.HourStartUTC < e
                                  and s < cell.HourStartUTC + timedelta(hours=1)
                                  for s, e in resolved)]
            paragraphs = sorted({cell.RuleApplied for cell in overlapping})
            down_hours = sorted(cell.HourStartUTC for cell in overlapping
                                if cell.Valid is CellValid.invalid)

            records.append(ListCRecord(
                Analyzer=analyzer, State=state,
                ResolvedWindows=resolved, ResolvedMinutes=minutes,
                SourceUsed=source, Disagreement=disagreement,
                WindowA=merge_intervals(windows_a), WindowsB=windows_b,
                ReasonCode=_latest_nonblank(a_obs or b_obs, events_by_id, "ReasonCode"),
                Note=_latest_nonblank(a_obs, events_by_id, "Reason"),
                CorrectiveAction=_latest_nonblank(a_obs, events_by_id, "CorrectiveAction"),
                ApproverName=approver, ApproverDecision=decision,
                ApprovedAtUTC=approved_at,
                GoverningParagraphs=paragraphs, DownHours=down_hours,
                ContributingRecords=provenance,
            ))
    # OOC (App F §4.3.1) records — emitted BEFORE the propagated pass so their
    # hours count as "covered" and detected-propagation does not double-book
    # an OOC-caused invalid hour.
    records.extend(_ooc_records(cells_by_analyzer))

    # v3 Gap #2: add records for diluent-propagated downtime on dependents.
    if analyzer_units:
        records.extend(_propagated_records(
            analyzer_units, cells, records, cells_by_analyzer))

    records.sort(key=lambda r: (r.Analyzer,
                                r.ResolvedWindows[0][0] if r.ResolvedWindows else datetime.max))
    return records


# ---------------------------------------------------------------------------
# CSV writer (single-writer discipline: only the fold output reaches disk)
# ---------------------------------------------------------------------------

_LIST_C_HEADERS = [
    "Analyzer", "State", "ResolvedWindows", "ResolvedMinutes", "SourceUsed",
    "Disagreement", "WindowA", "WindowsB", "ReasonCode", "Note",
    "CorrectiveAction", "ApproverName", "ApproverDecision", "ApprovedAtUTC",
    "GoverningParagraphs", "DownHours", "ContributingRecords",
]


def write_list_c(path, records: List[ListCRecord]) -> None:
    import csv
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    def ws(intervals):
        return ";".join(f"{s.isoformat()}/{e.isoformat()}" for s, e in intervals)

    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_LIST_C_HEADERS)
        w.writeheader()
        for r in records:
            w.writerow({
                "Analyzer": r.Analyzer, "State": r.State,
                "ResolvedWindows": ws(r.ResolvedWindows),
                "ResolvedMinutes": r.ResolvedMinutes,
                "SourceUsed": r.SourceUsed, "Disagreement": r.Disagreement,
                "WindowA": ws(r.WindowA), "WindowsB": ws(r.WindowsB),
                "ReasonCode": r.ReasonCode, "Note": r.Note,
                "CorrectiveAction": r.CorrectiveAction,
                "ApproverName": r.ApproverName,
                "ApproverDecision": r.ApproverDecision,
                "ApprovedAtUTC": r.ApprovedAtUTC.isoformat() if r.ApprovedAtUTC else "",
                "GoverningParagraphs": ";".join(r.GoverningParagraphs),
                "DownHours": ";".join(h.isoformat() for h in r.DownHours),
                "ContributingRecords": ";".join(r.ContributingRecords),
            })
