"""
Step 7 — digest.py (morning digest renderer). Pure, no I/O — takes already-
computed results and returns markdown text; the caller (run.py) writes it
to out/<run_date>/digest.md.

Sections, IN ORDER, per spec:
  1. Heartbeat line ("Clerk run <timestamp> — LastSuccessfulRun") — the
     ABSENCE of the digest is itself the alarm (Guarantee D). This module
     only guarantees the line is always first when rendering succeeds; the
     no-digest-at-all case is necessarily an operational/orchestration
     concern (run.py, Step 6), not something this pure function can enforce.
  2. Integrity alerts
  3. New downtime since yesterday
  4. Late-arriving ✗
  5. Pending dismissals + age (with any cancel-approval flags)
  6. Withdrawals and conflicts (confirmed-ticket-vs-vanished-signal)
  7. Summary counts

First run (diff_result.is_first_run): sections 2-4 collapse to a single
"first run" note per cell-flip section, since there is nothing to diff
against — per spec, "digest says so."

Inputs beyond diff.py's DiffResult:
  - `events`: today's full event set, re-folded here (cheap, pure) to find
    Dismissal-pending observations and their proposal age. diff.py's
    DiffResult doesn't carry the full Observation list — only flip/alert
    data — so this module re-derives what it needs directly.
  - `delta_result`: tonight's DeltaResult (new_events + flags) — the source
    for "withdrawals" (tonight's machine-emitted Withdrawn events) and
    "conflicts" (delta.py's CONFLICT-prefixed flags) and the cancel-
    approval flag text attached to a pending dismissal, if any.

FLAGGED, not blocking — two implementation choices without a spec-literal
answer: (1) pending-dismissal "age" is computed in UTC calendar days from
the observation's DismissalProposed ActedAt to run_date — NOT site-local
like the late-arrival check, since age here is a coarse staleness signal,
not an hour-precision compliance boundary; (2) "new downtime since
yesterday" on a first run is summarized as a single count, not itemized
(itemizing every cell on day one would be noise, not signal).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional

from clerk.delta import DeltaResult
from clerk.diff import DiffResult
from clerk.fold import Observation, Status, fold
from clerk.schemas import Event, EventType


def _find_last_event(
    obs: Observation,
    events_by_id: Dict[str, Event],
    event_type: EventType,
) -> Optional[Event]:
    for eid in reversed(obs.contributing_event_ids):
        e = events_by_id.get(eid)
        if e is not None and e.EventType is event_type:
            return e
    return None


def _cancel_approval_flag_for(origin_id: str, flags: List[str]) -> Optional[str]:
    return next(
        (f for f in flags if f.startswith("CANCEL-APPROVAL") and origin_id in f),
        None,
    )


def _heartbeat(run_timestamp: datetime) -> str:
    return f"# Clerk run {run_timestamp.isoformat()} — LastSuccessfulRun"


def _integrity_alerts_section(diff_result: DiffResult) -> str:
    lines = ["## Integrity Alerts"]
    if diff_result.is_first_run:
        lines.append("First run — no prior grid to compare against; nothing to trace.")
    elif not diff_result.integrity_alerts:
        lines.append("None.")
    else:
        for a in diff_result.integrity_alerts:
            lines.append(f"- {a.analyzer} {a.hour_start_utc.isoformat()} — {a.explanation}")
    return "\n".join(lines)


def _new_downtime_section(diff_result: DiffResult) -> str:
    lines = ["## New Downtime Since Yesterday"]
    if diff_result.is_first_run:
        lines.append(f"First run: {diff_result.new_cell_count} cells reported as new "
                     "(no prior grid to compare against).")
    elif not diff_result.new_invalid_cells:
        lines.append("None.")
    else:
        for c in sorted(diff_result.new_invalid_cells, key=lambda c: (c.analyzer, c.hour_start_utc)):
            lines.append(f"- {c.analyzer} {c.hour_start_utc.isoformat()}")
    return "\n".join(lines)


def _late_arrivals_section(diff_result: DiffResult) -> str:
    lines = ["## Late-Arriving Invalid Hours"]
    if diff_result.is_first_run:
        lines.append("First run — not applicable.")
    elif not diff_result.late_arrivals:
        lines.append("None.")
    else:
        for a in sorted(diff_result.late_arrivals, key=lambda a: (a.analyzer, a.hour_start_utc)):
            lines.append(f"- {a.analyzer} {a.hour_start_utc.isoformat()} "
                        f"— {a.days_old} days old")
    return "\n".join(lines)


def _pending_dismissals_section(
    observations: List[Observation],
    events_by_id: Dict[str, Event],
    run_date: date,
    delta_flags: List[str],
) -> str:
    lines = ["## Pending Dismissals"]
    pending = [o for o in observations if o.status is Status.dismissal_pending]
    if not pending:
        lines.append("None.")
        return "\n".join(lines)

    for obs in sorted(pending, key=lambda o: o.origin_event_id):
        proposed = _find_last_event(obs, events_by_id, EventType.DismissalProposed)
        age_days = (run_date - proposed.ActedAt.date()).days if proposed is not None else None
        age_text = f"{age_days} days old" if age_days is not None else "age unknown"
        line = f"- {obs.origin_event_id} ({', '.join(obs.analyzers)}) — {age_text}"
        cancel_flag = _cancel_approval_flag_for(obs.origin_event_id, delta_flags)
        if cancel_flag is not None:
            line += f" — {cancel_flag}"
        lines.append(line)
    return "\n".join(lines)


def _withdrawals_and_conflicts_section(delta_result: DeltaResult) -> str:
    lines = ["## Withdrawals & Conflicts"]
    withdrawals = [e for e in delta_result.new_events if e.EventType is EventType.Withdrawn]
    conflicts = [f for f in delta_result.flags if f.startswith("CONFLICT")]

    if not withdrawals and not conflicts:
        lines.append("None.")
        return "\n".join(lines)

    for e in withdrawals:
        lines.append(f"- Withdrawn: {e.TargetEventID} ({', '.join(e.AnalyzerCEMIDs)}) "
                    f"— {e.Reason}")
    for f in conflicts:
        lines.append(f"- {f}")
    return "\n".join(lines)


def _summary_section(diff_result: DiffResult, pending_count: int, delta_result: DeltaResult) -> str:
    withdrawals = sum(1 for e in delta_result.new_events if e.EventType is EventType.Withdrawn)
    conflicts = sum(1 for f in delta_result.flags if f.startswith("CONFLICT"))
    lines = [
        "## Summary",
        f"- Integrity alerts: {len(diff_result.integrity_alerts)}",
        f"- Silent passes: {len(diff_result.silent_passes)}",
        f"- Machine-informational flips: {len(diff_result.machine_informational)}",
        f"- New invalid cells: {diff_result.new_cell_count if diff_result.is_first_run else len(diff_result.new_invalid_cells)}",
        f"- Late-arriving invalid cells: {len(diff_result.late_arrivals)}",
        f"- Pending dismissals: {pending_count}",
        f"- Withdrawals tonight: {withdrawals}",
        f"- Conflicts: {conflicts}",
    ]
    return "\n".join(lines)


def render_digest(
    run_timestamp: datetime,
    events: List[Event],
    diff_result: DiffResult,
    delta_result: DeltaResult,
    run_date: date,
) -> str:
    """Pure function: today's events + diff.py's result + tonight's delta
    result -> the full digest.md text, sections in spec order."""
    observations = fold(events)
    events_by_id = {e.EventID: e for e in events}
    pending = [o for o in observations if o.status is Status.dismissal_pending]

    sections = [
        _heartbeat(run_timestamp),
        _integrity_alerts_section(diff_result),
        _new_downtime_section(diff_result),
        _late_arrivals_section(diff_result),
        _pending_dismissals_section(observations, events_by_id, run_date, delta_result.flags),
        _withdrawals_and_conflicts_section(delta_result),
        _summary_section(diff_result, len(pending), delta_result),
    ]
    return "\n\n".join(sections) + "\n"
