"""
Step 3a — the delta writer (minimal core, pulled forward at Ryan's direction
2026-07-02 to make the real-data drift pair testable; rules.py remains parked
at its eCFR verbatim review pause).

Compares tonight's capsules against the known-episode set, per analyzer +
detection class, matched by STRICT temporal overlap — never start-time, and
abutment (end == start) is NOT overlap.

Abutment RULED (Ryan, 2026-07-02): at capsule ingestion — before matching
against history — same-analyzer + same-class capsules with zero gap coalesce
into one candidate episode for tonight's pull. Matching/withdrawal logic is
otherwise unchanged. Episode-vs-capsule abutment across the history boundary
remains strict no-match and is still flagged, not decided.

Pull-window scoping (spec open item 7, made concrete by the drift pair):
Withdrawn may only be emitted for episodes fully INSIDE the run's pull window.
Absence of a capsule outside the pulled range is not evidence of anything.

Fail-safe polarity notes:
- A matching failure can produce a duplicate ticket, never an understated hour
  (the grid consumes capsules directly; tickets are not math inputs).
- Confirmed episodes never withdraw — the human assertion stands and the
  conflict is flagged.
- Split/merge (many-to-many overlap) is flagged and left for the full Step 3a;
  emitting nothing there cannot understate hours, per the above.

Deferred to full Step 3a: reanimation (matching against Withdrawn episodes in
the lookback window), material-boundary checks against grid cells and
dismissal coverage (jitter-only here), and BoundaryUpdate splits.

Encoding RULED (Ryan, 2026-07-02): machine-authored events carry their class
in the Event's dedicated DetectionClass column, blank for human-authored
EventTypes. Category is unused and not repurposed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from clerk.schemas import Capsule, Event, EventType, PullWindow

# Every event this module emits carries this Actor. diff.py (Step 7) checks
# against it to confirm a Withdrawn/BoundaryUpdate is machine-attributed
# before treating a grid flip as digest-informational rather than an
# INTEGRITY ALERT.
MACHINE_ACTOR = "clerk-delta"


@dataclass
class Episode:
    """A known downtime episode: the folded state of one detection ticket."""
    EpisodeID: str                  # originating event ID
    Analyzer: str
    DetectionClass: str
    StartUTC: datetime
    EndUTC: datetime
    Confirmed: bool = False
    DismissalPending: bool = False


@dataclass
class DeltaResult:
    new_events: List[Event] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


def episodes_from_detections(events: List[Event]) -> List[Episode]:
    """Materialize episodes from SeeqDetection events (the dedicated
    DetectionClass column carries the class — see module docstring)."""
    eps = []
    for e in events:
        if e.EventType is not EventType.SeeqDetection:
            continue
        eps.append(Episode(
            EpisodeID=e.EventID,
            Analyzer=e.AnalyzerCEMIDs[0],
            DetectionClass=e.DetectionClass,
            StartUTC=e.ExtentStartUTC,
            EndUTC=e.ExtentEndUTC,
        ))
    return eps


def coalesce_capsules(capsules: List[Capsule]) -> List[Capsule]:
    """Ingestion-time coalescing (Ryan's ruling, 2026-07-02): same-analyzer +
    same-DetectionClass capsules with ZERO gap (end[i] == start[i+1]) merge
    into one candidate episode for tonight's pull. Upstream max capsule
    duration is 2 h, so long outages arrive as abutting chains — this makes
    them present as one continuous episode. Gaps of any size stay split."""
    ordered = sorted(capsules, key=lambda c: (c.Analyzer, c.DetectionClass,
                                              c.CapsuleStartUTC, c.CapsuleEndUTC))
    out: List[Capsule] = []
    for c in ordered:
        if (out
                and (out[-1].Analyzer, out[-1].DetectionClass) == (c.Analyzer, c.DetectionClass)
                and out[-1].CapsuleEndUTC == c.CapsuleStartUTC):
            out[-1] = Capsule(
                Analyzer=c.Analyzer,
                DetectionClass=c.DetectionClass,
                CapsuleStartUTC=out[-1].CapsuleStartUTC,
                CapsuleEndUTC=c.CapsuleEndUTC,
            )
        else:
            out.append(Capsule(
                Analyzer=c.Analyzer,
                DetectionClass=c.DetectionClass,
                CapsuleStartUTC=c.CapsuleStartUTC,
                CapsuleEndUTC=c.CapsuleEndUTC,
            ))
    return out


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    """Strict temporal overlap. Abutment (end == start) is zero overlap."""
    return a_start < b_end and b_start < a_end


def _abuts(a_start, a_end, b_start, b_end) -> bool:
    return a_end == b_start or b_end == a_start


def run_delta(
    episodes: List[Episode],
    capsules: List[Capsule],
    pull_window: PullWindow,
    run_at: datetime,
    jitter_tolerance_min: int = 5,
    id_prefix: str = "NEW",
) -> DeltaResult:
    """Pure function: (known episodes, tonight's capsules, pull window) →
    proposed events + flags. The clerk never writes to the ledger itself."""
    result = DeltaResult()
    jitter = timedelta(minutes=jitter_tolerance_min)
    seq = 0

    def next_id() -> str:
        nonlocal seq
        seq += 1
        return f"{id_prefix}-{seq:04d}"

    def propose(event_type: EventType, analyzer: str, detection_class: str,
                start, end, target: str | None, reason: str) -> None:
        result.new_events.append(Event(
            EventID=next_id(),
            EventType=event_type,
            TargetEventID=target,
            ExtentStartUTC=start,
            ExtentEndUTC=end,
            AnalyzerCEMIDs=[analyzer],
            Category="",                     # unused — not repurposed
            ReasonCode="",
            Actor=MACHINE_ACTOR,
            ActedAt=run_at,
            Reason=reason,
            CorrectiveAction="",
            DetectionClass=detection_class,  # machine-authored: dedicated column
        ))

    # Ingestion-time coalescing precedes matching (Ryan's ruling 2026-07-02);
    # coalesce_capsules also yields deterministic processing order.
    caps = coalesce_capsules(capsules)
    eps = sorted(episodes, key=lambda e: (e.Analyzer, e.DetectionClass,
                                          e.StartUTC, e.EndUTC))

    cap_matches: Dict[int, List[Episode]] = {i: [] for i in range(len(caps))}
    ep_matches: Dict[str, List[Capsule]] = {e.EpisodeID: [] for e in eps}

    for i, c in enumerate(caps):
        for e in eps:
            if (c.Analyzer, c.DetectionClass) != (e.Analyzer, e.DetectionClass):
                continue
            if _overlaps(c.CapsuleStartUTC, c.CapsuleEndUTC, e.StartUTC, e.EndUTC):
                cap_matches[i].append(e)
                ep_matches[e.EpisodeID].append(c)
            elif _abuts(c.CapsuleStartUTC, c.CapsuleEndUTC, e.StartUTC, e.EndUTC):
                result.flags.append(
                    f"ABUTMENT episode {e.EpisodeID} ({e.Analyzer} {e.StartUTC:%Y-%m-%dT%H:%MZ}"
                    f"–{e.EndUTC:%H:%MZ}) touches capsule "
                    f"{c.CapsuleStartUTC:%Y-%m-%dT%H:%MZ}–{c.CapsuleEndUTC:%H:%MZ} — "
                    "strict overlap treats them as unrelated (two tickets); Ryan to rule"
                )

    # (Capsule-capsule abutment within tonight's set no longer flags — it is
    # resolved by ingestion-time coalescing, per Ryan's ruling 2026-07-02.)

    # --- capsule side -------------------------------------------------------
    for i, c in enumerate(caps):
        matched = cap_matches[i]
        if not matched:
            propose(EventType.SeeqDetection, c.Analyzer, c.DetectionClass,
                    c.CapsuleStartUTC, c.CapsuleEndUTC, None,
                    "born Needs review")
        elif len(matched) == 1 and len(ep_matches[matched[0].EpisodeID]) == 1:
            e = matched[0]
            moved_start = abs(c.CapsuleStartUTC - e.StartUTC)
            moved_end = abs(c.CapsuleEndUTC - e.EndUTC)
            if moved_start > jitter or moved_end > jitter:
                propose(EventType.BoundaryUpdate, c.Analyzer, c.DetectionClass,
                        c.CapsuleStartUTC, c.CapsuleEndUTC, e.EpisodeID,
                        f"boundary moved from {e.StartUTC.isoformat()}/"
                        f"{e.EndUTC.isoformat()}")
            # sub-jitter movement writes nothing
        else:
            result.flags.append(
                f"SPLIT/MERGE candidate on {c.Analyzer} {c.DetectionClass} around "
                f"{c.CapsuleStartUTC:%Y-%m-%dT%H:%MZ} — deferred to full Step 3a; "
                "no event emitted (grid consumes capsules directly, hours cannot understate)"
            )

    # --- episode side: withdrawal, window-scoped ----------------------------
    for e in eps:
        if ep_matches[e.EpisodeID]:
            continue
        inside = (pull_window.PullStartUTC <= e.StartUTC
                  and e.EndUTC <= pull_window.PullEndUTC)
        if e.Confirmed:
            result.flags.append(
                f"CONFLICT confirmed episode {e.EpisodeID} ({e.Analyzer}) has no "
                "matching capsule — human assertion stands, no Withdrawn; digest"
            )
        elif inside:
            propose(EventType.Withdrawn, e.Analyzer, e.DetectionClass,
                    e.StartUTC, e.EndUTC, e.EpisodeID,
                    "no matching capsule in re-detection window")
            if e.DismissalPending:
                result.flags.append(
                    f"CANCEL-APPROVAL withdrawn episode {e.EpisodeID} has a "
                    "dismissal in flight — cancel the approval card; digest"
                )
        # outside the pull window: absence of evidence — emit nothing

    return result
