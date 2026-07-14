"""
demo_passing_validation.py — proves the passing-validation-downtime fix.

Scenario: N days of a routine PASSING daily validation (a ~20-min cal-gas
check, 06:40-07:00) on two analyzers, with Seeq ALSO flagging the analyzer
offline during the cal gas (a coincident status-offline capsule) — the exact
real-run shape. No other events.

Expected: ZERO monitor downtime. A passing validation is normal QA activity
(40 CFR 60.13(h)(2)(iii)), not an outage. Each validation hour is judged by
(iii)(A) — two valid points >=15 min apart, satisfied by the valid data before
and after the check — never by (i)'s all-four-quadrants rule.

Pre-fix, each validation hour scored DOWN under (i) (the cal gas killed the
overlapping 15-min quadrant), piling ~1 phantom down-hour per analyzer per day.

Run:  python demo_passing_validation.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clerk.dar import dar_rollup
from clerk.grid import build_grid
from clerk.ooc import PASS, ValidationCapsule, compute_ooc_windows
from clerk.schemas import (AnalyzerUnit, Capsule, CellValid, OperatingWindow,
                           SiteConfig)

CONFIG = SiteConfig(SiteTimeZoneIANA="America/New_York", LookbackMonths=8,
                    LateXThresholdDays=7, JitterToleranceMin=5,
                    PartialOperatingHourApplicability={},
                    ReasonParagraphMap={"QA-01": "(iii)"})
D = datetime(2026, 4, 1, tzinfo=timezone.utc)
def t(h, m=0): return D + timedelta(hours=h, minutes=m)

DAYS = 21
ANALYZERS = ["Boiler_15 - NOx", "SRU - O2"]
units = [AnalyzerUnit(a, "U1", SeeqCovered=True, Obligation=a.split(" - ")[1])
         for a in ANALYZERS]
start, end = t(0), t(24 * DAYS)

val, caps, mqaqc = [], [], {}
for a in ANALYZERS:
    for d in range(DAYS):
        s, e = t(24 * d + 6, 40), t(24 * d + 7)          # 06:40-07:00 cal gas
        val.append(ValidationCapsule(a, s, e, PASS))
        caps.append(Capsule(a, "status-offline", s, e))  # Seeq flags offline
        mqaqc.setdefault(a, []).append((s, e))

ooc, _ = compute_ooc_windows(val, open_tail_end=end)     # a Pass yields no OOC
cells = build_grid([], caps, [OperatingWindow("U1", start, end)], units, [],
                   CONFIG, start, end, ooc_windows=ooc, mqaqc_windows=mqaqc)

down = [c for c in cells if c.Valid is CellValid.invalid]
print(f"{DAYS} days x {len(ANALYZERS)} analyzers, one passing daily validation each")
print(f"Invalid (down) hours across the whole grid: {len(down)}  (expected 0)")
for r in dar_rollup(cells, [], units, start, end):
    print(f"  DAR {r.Analyzer:16s} op={r.OperatingHours}h "
          f"down={r.DowntimeHours}h ({r.DowntimePct}%)  >=5%={r.Flag5pctDowntime}")

assert not down, f"FAIL: {len(down)} phantom down-hours from passing validations"
assert all(r.DowntimePct == 0.0 for r in dar_rollup(cells, [], units, start, end))
print("\nPASS — passing daily validations contribute zero downtime.")
print("(Pre-fix this scenario produced 1 phantom down-hour per analyzer per "
      f"day = {DAYS * len(ANALYZERS)} phantom down-hours.)")
