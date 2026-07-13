"""
dar.py — §60.7(d) Summary Report (DAR) roll-up (pure, no clerk-logic changes).

Item 4 (run-window vs reporting-period): the fold/grid process the WHOLE
evaluation window (so a late amendment anywhere in it is caught); this
roll-up FILTERS the grid to a reporting sub-range [reporting_start,
reporting_end) before aggregating. Both bounds are parameters.

Item 5 (§60.7(d) Summary Report): per pollutant per analyzer, over the
reporting period:
  - total operating time (the denominator: assessed hours; full unit-offline
    hours excluded)
  - total monitor downtime (hours and % of operating time)
  - total excess-emission duration (hours and %)
  - the >=5% downtime and >=1% excess threshold flags
  - a downtime breakdown by reason category (MM/NM/QA/OK/UK)
  - the report trigger: >=5% downtime OR >=1% excess -> the FULL
    excess-emission report is required; under BOTH -> summary only.

HONEST SCOPE NOTE: the clerk derives monitor downtime from data VALIDITY.
Excess-emission duration is a different quantity — concentration vs. the
emission standard — which the clerk does not ingest. It is accepted here as
an optional `excess_windows` input (per analyzer). When none is supplied the
DAR reports 0 excess hours and sets ExcessDataProvided=false, rather than
inferring excess emissions from validity (which would be wrong).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from clerk.grid import CellValid, GridCell, is_down_hour
from clerk.listc import ListCRecord

Interval = Tuple[datetime, datetime]

# ReasonCode -> DAR downtime reason category.
_REASON_CATEGORY = {
    "MM-01": "MM", "NM-01": "NM", "QA-01": "QA", "OK-01": "OK", "UK-01": "UK",
}
_CATEGORIES = ["MM", "NM", "QA", "OK", "UK"]


@dataclass
class DARRow:
    Analyzer: str
    Obligation: str
    OperatingHours: int
    DowntimeHours: int
    DowntimePct: float
    ExcessHours: int
    ExcessPct: float
    ExcessDataProvided: bool
    Flag5pctDowntime: bool
    Flag1pctExcess: bool
    ReportRequired: str            # "full excess-emission report" | "summary only"
    DowntimeByReason: Dict[str, int]


def _in_period(hour: datetime, start: datetime, end: datetime) -> bool:
    return start <= hour < end


def _reason_category_for_hour(analyzer: str, hour: datetime,
                              records: List[ListCRecord]) -> str:
    """Attribute a down hour to a reason category via the List C record that
    covers it. OOC records carry ReasonCode QA-01 -> QA. Unattributed down
    hours fall to UK (unknown)."""
    for r in records:
        if r.Analyzer == analyzer and hour in r.DownHours:
            return _REASON_CATEGORY.get(r.ReasonCode, "UK")
    return "UK"


def dar_rollup(
    cells: List[GridCell],
    records: List[ListCRecord],
    analyzer_units,
    reporting_start: datetime,
    reporting_end: datetime,
    excess_windows: Optional[Dict[str, List[Interval]]] = None,
) -> List[DARRow]:
    """Build one DAR row per analyzer over [reporting_start, reporting_end)."""
    excess_windows = excess_windows or {}
    obligation = {au.Analyzer: au.Obligation for au in analyzer_units}

    # Filter the grid to the reporting period.
    period = [c for c in cells if _in_period(c.HourStartUTC, reporting_start, reporting_end)]
    by_analyzer: Dict[str, List[GridCell]] = {}
    for c in period:
        by_analyzer.setdefault(c.Analyzer, []).append(c)

    rows: List[DARRow] = []
    for analyzer in sorted(by_analyzer):
        acells = by_analyzer[analyzer]
        operating = sum(1 for c in acells if c.Valid is not CellValid.not_operating)
        down_cells = [c for c in acells if is_down_hour(c)]
        downtime = len(down_cells)

        # Excess-emission hours: only from provided excess data (never inferred).
        ex_provided = analyzer in excess_windows
        excess = 0
        for c in acells:
            he = c.HourStartUTC + timedelta(hours=1)
            if any(s < he and c.HourStartUTC < e for s, e in excess_windows.get(analyzer, [])):
                excess += 1

        dt_pct = (100.0 * downtime / operating) if operating else 0.0
        ex_pct = (100.0 * excess / operating) if operating else 0.0

        by_reason = {cat: 0 for cat in _CATEGORIES}
        for c in down_cells:
            by_reason[_reason_category_for_hour(analyzer, c.HourStartUTC, records)] += 1

        flag_dt = dt_pct >= 5.0
        flag_ex = ex_pct >= 1.0
        rows.append(DARRow(
            Analyzer=analyzer, Obligation=obligation.get(analyzer, ""),
            OperatingHours=operating, DowntimeHours=downtime,
            DowntimePct=round(dt_pct, 3),
            ExcessHours=excess, ExcessPct=round(ex_pct, 3),
            ExcessDataProvided=ex_provided,
            Flag5pctDowntime=flag_dt, Flag1pctExcess=flag_ex,
            ReportRequired=("full excess-emission report"
                            if (flag_dt or flag_ex) else "summary only"),
            DowntimeByReason=by_reason,
        ))
    return rows


_DAR_HEADERS = [
    "Analyzer", "Obligation", "OperatingHours", "DowntimeHours", "DowntimePct",
    "ExcessHours", "ExcessPct", "ExcessDataProvided",
    "Flag5pctDowntime", "Flag1pctExcess", "ReportRequired",
    "DT_MM", "DT_NM", "DT_QA", "DT_OK", "DT_UK",
]


def write_dar(path, rows: List[DARRow]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_DAR_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "Analyzer": r.Analyzer, "Obligation": r.Obligation,
                "OperatingHours": r.OperatingHours, "DowntimeHours": r.DowntimeHours,
                "DowntimePct": r.DowntimePct, "ExcessHours": r.ExcessHours,
                "ExcessPct": r.ExcessPct, "ExcessDataProvided": r.ExcessDataProvided,
                "Flag5pctDowntime": r.Flag5pctDowntime, "Flag1pctExcess": r.Flag1pctExcess,
                "ReportRequired": r.ReportRequired,
                "DT_MM": r.DowntimeByReason["MM"], "DT_NM": r.DowntimeByReason["NM"],
                "DT_QA": r.DowntimeByReason["QA"], "DT_OK": r.DowntimeByReason["OK"],
                "DT_UK": r.DowntimeByReason["UK"],
            })
