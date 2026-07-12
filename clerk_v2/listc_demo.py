"""
listc_demo.py — List A + List B -> List C demonstration wrapper.

Reads two CSVs (List A: the manual log; List B: Seeq detections), runs the
clerk's REAL fold (clerk.grid.build_grid — untouched), and writes the
resolved List C (the hourly verdict grid). No SharePoint, PI, or Seeq
connection anywhere — this script reads the two CSVs and nothing else.

Usage:
    python listc_demo.py                      # uses the paths below
    python listc_demo.py A.csv B.csv C.csv    # or point at other samples

On success it rewrites list_a.csv / list_b.csv exactly as the clerk parsed
them (normalized timestamps, defaults filled) so all three files display
consistently, and prints a run summary.

-------------------------------------------------------------------------
EXPECTED CSV COLUMNS
-------------------------------------------------------------------------
All timestamps: ISO-8601 UTC with Z suffix — e.g. 2026-04-01T09:20:00Z.
(Store UTC; site-local rendering is List C's HourLocalLabel column.)

list_a.csv — the manual log (each row becomes one TechEntry event):
    Analyzer    analyzer id, e.g. NOX-01
    StartUTC    entry start (required)
    EndUTC      entry end — LEAVE BLANK for a start-only marker: it is
                recorded but produces no downtime window (Doc 50 T8)
    ReasonCode  one of MM-01 (monitor malfunction), NM-01 (non-monitor
                malfunction), QA-01 (QA/calibration/maintenance),
                OK-01 (other-known), UK-01 (unknown). QA-01 routes the
                hour through CFR paragraph (iii); the others through (i).
    Tech        who logged it (free text)
    Note        free text

list_b.csv — detections (each row becomes one capsule):
    Analyzer          analyzer id
    DetectionClass    opaque class key, e.g. status-offline
    CapsuleStartUTC   detection start (required)
    CapsuleEndUTC     detection end (required)

list_c.csv — OUTPUT (the clerk's grid.csv format, written by write_grid):
    Analyzer, HourStartUTC, HourLocalLabel, OperatingFraction,
    Valid (1 | 0 | NOT-OPERATING), RuleApplied, ContributingEventIDs
-------------------------------------------------------------------------
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clerk.grid import build_grid
from clerk.schemas import (
    AnalyzerUnit,
    Capsule,
    CellValid,
    Event,
    EventType,
    SiteConfig,
    write_grid,
)
import csv

# --- Paths (overridable via command line: A B C) --------------------------
HERE = Path(__file__).parent
LIST_A_PATH = HERE / "samples" / "sample_list_a.csv"
LIST_B_PATH = HERE / "samples" / "sample_list_b.csv"
LIST_C_PATH = HERE / "samples" / "list_c.csv"

# --- Demo plant configuration ---------------------------------------------
# Every analyzer found in A∪B is placed on one synthetic unit, Seeq-covered
# (per doctrine every operating hour is assessed). Adjust the offline gaps
# to demonstrate dropped (NOT-OPERATING) hours.
DEMO_UNIT = "DEMO-UNIT"
SITE_TZ = "America/New_York"
OFFLINE_WINDOWS = [  # unit offline -> hours DROPPED, not down
    (datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
     datetime(2026, 4, 1, 13, 0, tzinfo=timezone.utc)),
]
# Evaluation window: set explicitly, or None to derive from the data
# (floor of earliest timestamp .. ceil of latest, hour-aligned).
WINDOW_START = None
WINDOW_END = None

# F7 reason vocabulary -> CFR paragraph (provisional seed, matches config.csv)
REASON_PARAGRAPH_MAP = {
    "MM-01": "(i)", "NM-01": "(i)", "QA-01": "(iii)",
    "OK-01": "(i)", "UK-01": "(i)",
}


def _dt(s):
    return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))


def _iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_list_a(path):
    """Manual log rows -> TechEntry events (the clerk's List A shape)."""
    events, rows = [], []
    with open(path, newline="") as f:
        for i, r in enumerate(csv.DictReader(f), start=1):
            start = _dt(r["StartUTC"])
            end = _dt(r["EndUTC"]) if (r.get("EndUTC") or "").strip() else None
            reason = (r.get("ReasonCode") or "").strip()
            events.append(Event(
                EventID=f"A{i:03d}", EventType=EventType.TechEntry,
                TargetEventID=None, ExtentStartUTC=start, ExtentEndUTC=end,
                AnalyzerCEMIDs=[r["Analyzer"].strip()], Category="",
                ReasonCode=reason, Actor=(r.get("Tech") or "").strip(),
                ActedAt=start, Reason=(r.get("Note") or "").strip(),
                CorrectiveAction="", DetectionClass="",
            ))
            rows.append({"Analyzer": r["Analyzer"].strip(), "StartUTC": _iso(start),
                         "EndUTC": _iso(end) if end else "", "ReasonCode": reason,
                         "Tech": (r.get("Tech") or "").strip(),
                         "Note": (r.get("Note") or "").strip()})
    return events, rows


def read_list_b(path):
    """Detection rows -> capsules (the clerk's List B shape)."""
    capsules, rows = [], []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            c = Capsule(Analyzer=r["Analyzer"].strip(),
                        DetectionClass=r["DetectionClass"].strip(),
                        CapsuleStartUTC=_dt(r["CapsuleStartUTC"]),
                        CapsuleEndUTC=_dt(r["CapsuleEndUTC"]))
            capsules.append(c)
            rows.append({"Analyzer": c.Analyzer, "DetectionClass": c.DetectionClass,
                         "CapsuleStartUTC": _iso(c.CapsuleStartUTC),
                         "CapsuleEndUTC": _iso(c.CapsuleEndUTC)})
    return capsules, rows


def _write_normalized(path, fieldnames, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _floor_hour(d):
    return d.replace(minute=0, second=0, microsecond=0)


def _derive_window(events, capsules):
    stamps = [e.ExtentStartUTC for e in events]
    stamps += [e.ExtentEndUTC for e in events if e.ExtentEndUTC]
    stamps += [c.CapsuleStartUTC for c in capsules] + [c.CapsuleEndUTC for c in capsules]
    lo, hi = _floor_hour(min(stamps)), max(stamps)
    hi_aligned = _floor_hour(hi)
    if hi_aligned < hi:
        hi_aligned += timedelta(hours=1)
    return lo, hi_aligned


def main(argv):
    a_path = Path(argv[1]) if len(argv) > 1 else LIST_A_PATH
    b_path = Path(argv[2]) if len(argv) > 2 else LIST_B_PATH
    c_path = Path(argv[3]) if len(argv) > 3 else LIST_C_PATH

    events, a_rows = read_list_a(a_path)
    capsules, b_rows = read_list_b(b_path)

    start, end = (WINDOW_START, WINDOW_END)
    if start is None or end is None:
        start, end = _derive_window(events, capsules)

    # Unit operating = the whole window minus the configured offline gaps.
    operating = []
    cursor = start
    for off_s, off_e in sorted(OFFLINE_WINDOWS):
        if off_s > cursor:
            operating.append((cursor, min(off_s, end)))
        cursor = max(cursor, off_e)
    if cursor < end:
        operating.append((cursor, end))
    from clerk.schemas import OperatingWindow
    operating_windows = [OperatingWindow(DEMO_UNIT, s, e) for s, e in operating if s < e]

    analyzers = sorted({e.AnalyzerCEMIDs[0] for e in events} |
                       {c.Analyzer for c in capsules})
    units = [AnalyzerUnit(a, DEMO_UNIT, SeeqCovered=True) for a in analyzers]

    config = SiteConfig(
        SiteTimeZoneIANA=SITE_TZ, LookbackMonths=8, LateXThresholdDays=7,
        JitterToleranceMin=5, PartialOperatingHourApplicability={},
        ReasonParagraphMap=dict(REASON_PARAGRAPH_MAP),
    )

    # THE SEAM — the clerk's real fold, pure in-memory, unchanged:
    cells = build_grid(events, capsules, operating_windows, units, [],
                       config, start, end)

    write_grid(c_path, cells)
    _write_normalized(a_path, ["Analyzer", "StartUTC", "EndUTC", "ReasonCode",
                               "Tech", "Note"], a_rows)
    _write_normalized(b_path, ["Analyzer", "DetectionClass", "CapsuleStartUTC",
                               "CapsuleEndUTC"], b_rows)

    down = [c for c in cells if c.Valid is CellValid.invalid]
    dropped = [c for c in cells if c.Valid is CellValid.not_operating]
    valid = [c for c in cells if c.Valid is CellValid.valid]
    markers = sum(1 for e in events if e.ExtentEndUTC is None)

    print(f"List A (manual log):  {len(a_rows)} rows -> {len(events)} TechEntry events"
          + (f" ({markers} start-only marker{'s' * (markers != 1)})" if markers else ""))
    print(f"List B (detections):  {len(b_rows)} rows -> {len(capsules)} capsules")
    print(f"Window: {_iso(start)} .. {_iso(end)}  "
          f"({int((end - start).total_seconds() // 3600)} hours x {len(analyzers)} analyzers"
          f" = {len(cells)} cells)")
    print(f"List C written: {c_path}  ({len(cells)} rows)")
    print(f"  Down hours (invalid):        {len(down)}")
    for c in down:
        print(f"    {c.Analyzer}: {_iso(c.HourStartUTC)}  via {c.RuleApplied}"
              f"  [{';'.join(c.ContributingEventIDs)}]")
    print(f"  Dropped (unit offline):      {len(dropped)}")
    for c in dropped:
        print(f"    {c.Analyzer}: {_iso(c.HourStartUTC)}")
    print(f"  Valid hours:                 {len(valid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
