"""
Step 6 — run.py, the orchestrator. schemas -> rules -> fold -> grid ->
delta -> diff -> digest, one command against a fixture directory. This is
the ONLY I/O boundary in the whole clerk besides schemas.py's readers/
writers — every step underneath is a pure function; run() just sequences
them and writes results to out/<run_date>/.

Step 1 — ingest + snapshot: read every fixture; copy events.csv verbatim
into out/<run_date>/snapshot/ (load-bearing habit — append-only becomes
tamper-evident in production, per spec, built in from day one).

The clerk never writes to the ledger (delta.py's own docstring): tonight's
grid/diff/digest reflect the EXISTING events.csv only. delta_result.
new_events are PROPOSALS for the external bridge to append; they are
NOT folded into tonight's own grid — a subsequent run picks them up once
appended, exactly as production is designed to work.

FLAGGED, not blocking — three open interpretive choices made to hit
tonight's synthetic-green target, none of them touching a SS5 hard-stop
category (verbatim/precedence/new-state), all documented here
and in CLAUDE.md:

1. Run window from LookbackMonths: `_months_before` snaps to the 1st of
   the month N months back from run_date — not exact day-for-day
   arithmetic, sidesteps end-of-month clamping entirely. Used VERBATIM
   from config (8 in the synthetic fixture) rather than shrunk for
   convenience — the honest non-decision is to trust config as given, even
   though most of the resulting window predates any operating.csv data and
   renders as a large block of correct-but-trivial NOT-OPERATING rows.

2. adjudicated_condition.csv shape: the spec's field list (Analyzer,
   ReasonCode, Status, RuleApplied, SourceEventIDs) has no explicit
   start/end, but "one capsule per episode" structurally implies bounds —
   added ConditionStartUTC/ConditionEndUTC (the episode's current extent).
   RuleApplied is the SORTED SET of distinct branch labels observed across
   the episode's overlapping grid-cell hours (an episode can span hours
   evaluated under different branches) — not a single reduced value.
   ReasonCode is the LATEST event in the episode's history carrying a
   non-blank ReasonCode (Confirmation-sourced, per schemas.py's note that
   the field applies to downtime confirmations only). Rows are emitted for
   EVERY folded Observation regardless of status — the push target is
   read as the full adjudicated record, not filtered to active tickets.

3. pi_payload.csv Value encoding: valid/invalid map to 1.0/0.0
   (uncontroversial). NOT-OPERATING and NOT-ASSESSED are left BLANK
   (Value=None) rather than assigned a numeric code — NOT-ASSESSED's
   encoding is directly entangled with the STILL-OPEN hard stop (how
   NOT-ASSESSED rolls into availability%/DAR — grid.py/CLAUDE.md) and
   inventing a number here would silently resolve it. Tag = Analyzer
   1:1 — no real PI tag-mapping table exists yet (per docs/13's "still
   missing" list); flagged as a placeholder.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

from clerk.delta import DeltaResult, episodes_from_detections, run_delta
from clerk.diff import DiffResult, diff_grids
from clerk.digest import render_digest
from clerk.fold import Observation, fold
from clerk.grid import build_grid
from clerk.schemas import (
    AdjudicatedCondition,
    CellValid,
    Event,
    GridCell,
    PIPayloadRow,
    read_analyzer_units,
    read_capsules,
    read_config,
    read_events,
    read_grid,
    read_operating,
    read_pull_windows,
    read_qa_windows,
    read_validations,
    write_adjudicated_condition,
    write_events,
    write_grid,
    write_pi_payload,
)


def _months_before(d: date, months: int) -> date:
    """1st of the month `months` months before d's month. FLAGGED choice
    #1 in the module docstring."""
    total = (d.year * 12 + (d.month - 1)) - months
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def _utc_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _last_nonblank_reason_code(obs: Observation, events_by_id: Dict[str, Event]) -> str:
    for eid in reversed(obs.contributing_event_ids):
        e = events_by_id.get(eid)
        if e is not None and e.ReasonCode.strip():
            return e.ReasonCode
    return ""


def _adjudicated_condition_rows(
    observations: List[Observation],
    events_by_id: Dict[str, Event],
    cells: List[GridCell],
) -> List[AdjudicatedCondition]:
    """FLAGGED choice #2 in the module docstring."""
    cells_by_analyzer: Dict[str, List[GridCell]] = {}
    for c in cells:
        cells_by_analyzer.setdefault(c.Analyzer, []).append(c)

    rows: List[AdjudicatedCondition] = []
    for obs in observations:
        reason_code = _last_nonblank_reason_code(obs, events_by_id)
        for analyzer in obs.analyzers:
            overlapping_rules = sorted({
                c.RuleApplied for c in cells_by_analyzer.get(analyzer, [])
                if c.HourStartUTC < obs.extent_end_utc
                and obs.extent_start_utc < c.HourStartUTC + timedelta(hours=1)
            })
            rows.append(AdjudicatedCondition(
                Analyzer=analyzer,
                ConditionStartUTC=obs.extent_start_utc,
                ConditionEndUTC=obs.extent_end_utc,
                Status=obs.status.value,
                ReasonCode=reason_code,
                RuleApplied=overlapping_rules,
                SourceEventIDs=list(obs.contributing_event_ids),
            ))
    return rows


def _pi_payload_rows(cells: List[GridCell]) -> List[PIPayloadRow]:
    """FLAGGED choice #3 in the module docstring."""
    rows: List[PIPayloadRow] = []
    for c in cells:
        if c.Valid is CellValid.valid:
            value = 1.0
        elif c.Valid is CellValid.invalid:
            value = 0.0
        else:
            value = None
        rows.append(PIPayloadRow(Tag=c.Analyzer, HourStartUTC=c.HourStartUTC, Value=value))
    return rows


@dataclass
class RunResult:
    grid_cells: List[GridCell]
    diff_result: DiffResult
    delta_result: DeltaResult
    digest_text: str
    out_dir: Path


def run(
    fixtures_dir: Path,
    out_dir: Path,
    run_date: date,
    run_at: datetime,
    pull_window_night: str = "1",
    id_prefix: str = "NEW",
    output_label: str = "",
) -> RunResult:
    """The orchestrator. `output_label`, if set, is written as a VISIBLE
    leading heading + rule in digest.md (never an HTML comment — a label
    nobody can see doesn't mark anything) — used by the real-data smoke
    test to mark itself provisional (never used for the synthetic run)."""
    # Step 1 — ingest + snapshot
    events = read_events(fixtures_dir / "events.csv")
    capsules = read_capsules(fixtures_dir / "capsules.csv")
    operating = read_operating(fixtures_dir / "operating.csv")
    analyzer_units = read_analyzer_units(fixtures_dir / "analyzer_units.csv")
    qa_windows = read_qa_windows(fixtures_dir / "qa_windows.csv")
    validations = read_validations(fixtures_dir / "validations.csv")  # [] if absent
    config = read_config(fixtures_dir / "config.csv")
    prior_cells = read_grid(fixtures_dir / "prior_grid.csv")
    pull_windows = read_pull_windows(fixtures_dir / "pull_windows.csv")
    pull_window = next(w for w in pull_windows if w.Night == pull_window_night)

    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = out_dir / "snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixtures_dir / "events.csv", snapshot_dir / "events.csv")

    # Step 3a — delta writer: proposals only, never folded into tonight's
    # own grid (see module docstring).
    episodes = episodes_from_detections(events)
    delta_result = run_delta(episodes, capsules, pull_window, run_at, id_prefix=id_prefix)
    write_events(out_dir / "new_events.csv", delta_result.new_events)

    # Steps 4-5 — union + rule engine
    window_start = _utc_midnight(_months_before(run_date, config.LookbackMonths))
    window_end = _utc_midnight(run_date) + timedelta(days=1)
    cells = build_grid(events, capsules, operating, analyzer_units, qa_windows,
                       config, window_start, window_end, validations=validations)
    write_grid(out_dir / "grid.csv", cells)

    # Step 6 — the two remaining writers
    observations = fold(events)
    events_by_id = {e.EventID: e for e in events}
    write_adjudicated_condition(
        out_dir / "adjudicated_condition.csv",
        _adjudicated_condition_rows(observations, events_by_id, cells))
    write_pi_payload(out_dir / "pi_payload.csv", _pi_payload_rows(cells))

    # Step 7 — diff + digest
    diff_result = diff_grids(prior_cells, cells, events, run_date,
                             config.LateXThresholdDays, config.SiteTimeZoneIANA, capsules)
    digest_text = render_digest(run_at, events, diff_result, delta_result, run_date)
    if output_label:
        # VISIBLE banner, not an HTML comment — a label nobody can see
        # defeats the purpose of marking output provisional.
        digest_text = f"# {output_label}\n\n---\n\n{digest_text}"
    (out_dir / "digest.md").write_text(digest_text)

    return RunResult(cells, diff_result, delta_result, digest_text, out_dir)
