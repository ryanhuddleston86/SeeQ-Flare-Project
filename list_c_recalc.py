"""
PI AF recalc-triggering logic spike.

Models List C as a collection of downtime records and computes two calcs:
  - RAW calc:      snapshot taken once at first-push time, never revisited.
  - MODIFIED calc: pure function of current List C state, recomputed on every push.

Aggregate: total downtime hours per calendar month, grouped by the month the
downtime record *starts* in. Duration = (end - start) in fractional hours.
No status filtering — all records are included regardless of status, by design.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

# (year, month) → total downtime hours
MonthKey = Tuple[int, int]
CalcResult = Dict[MonthKey, float]


@dataclass
class DowntimeRecord:
    id: str
    start: datetime
    end: datetime
    status: str
    last_modified: datetime

    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


def compute_modified_calc(records: List[DowntimeRecord]) -> CalcResult:
    """
    Pure function: given the current List C state, return total downtime hours
    per calendar month. Stateless — no accumulation between calls.
    """
    totals: CalcResult = {}
    for rec in records:
        key: MonthKey = (rec.start.year, rec.start.month)
        totals[key] = totals.get(key, 0.0) + rec.duration_hours()
    return totals


def take_raw_snapshot(records: List[DowntimeRecord]) -> CalcResult:
    """
    Call once at first-push time. Returns a frozen calc result that is never
    updated again, regardless of subsequent List C corrections.
    """
    return compute_modified_calc(records)
