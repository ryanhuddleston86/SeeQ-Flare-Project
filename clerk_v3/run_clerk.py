"""
run_clerk.py — the production execution path for the v3 clerk.

Reads three CSVs from the working directory and writes two:

    INPUTS                          OUTPUTS
    ------                          -------
    list_a.csv   (manual log)  -->  list_c.csv        (enriched List C records)
    list_b.csv   (detections)       list_c_hours.csv  (hourly verdict grid)
    roster.csv   (analyzers)

It runs the real v3 fold (clerk.grid.build_grid -> clerk.listc.build_list_c),
the same code path the nightly clerk uses. No demo scaffolding: no hardcoded
scenario names, no golden-trap checks, no assertions. Works on any List A/B
whose analyzers are defined in the roster, regardless of the Scenario column
(which is treated as a passthrough label and never used for logic).

Usage:
    python run_clerk.py                       # files in the current directory
    python run_clerk.py A.csv B.csv R.csv     # explicit input paths
    python run_clerk.py A.csv B.csv R.csv OUTDIR

Expected columns
----------------
roster.csv : Analyzer, Unit, Obligation, SeeqCovered, DiluentsRole,
             DiluentSpecies, DiluentBasis
list_a.csv : Scenario(optional label), EventID, Type, TargetEventID,
             Analyzer, StartUTC, EndUTC, ReasonCode, Category, Actor,
             ActedAtUTC, Note, CorrectiveAction, DetectionClass
list_b.csv : Scenario(optional label), Analyzer, DetectionClass,
             CapsuleStartUTC, CapsuleEndUTC
All timestamps ISO-8601 UTC (e.g. 2026-04-01T09:00:00Z). Blank EndUTC in
List A = a start-only marker (recorded, no downtime window).
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the clerk package importable whether run from clerk_v3/ or its parent.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from clerk.grid import build_grid                       # noqa: E402
from clerk.listc import build_list_c, write_list_c      # noqa: E402
from clerk.schemas import (Capsule, Event, EventType,   # noqa: E402
                           OperatingWindow, SiteConfig, read_analyzer_units,
                           write_grid)

# Site configuration. In production these come from the site config file;
# kept here as explicit defaults so the runner is self-contained.
CONFIG = SiteConfig(
    SiteTimeZoneIANA="America/New_York",
    LookbackMonths=8, LateXThresholdDays=7, JitterToleranceMin=5,
    PartialOperatingHourApplicability={},
    # Reason-code -> governing CFR paragraph (the site's mapping).
    ReasonParagraphMap={"MM-01": "(i)", "NM-01": "(i)", "QA-01": "(iii)",
                        "OK-01": "(i)", "UK-01": "(i)"},
)


def _dt(s: str):
    s = (s or "").strip()
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def read_events(path: Path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(Event(
                EventID=r["EventID"], EventType=EventType(r["Type"]),
                TargetEventID=(r.get("TargetEventID") or "").strip() or None,
                ExtentStartUTC=_dt(r["StartUTC"]), ExtentEndUTC=_dt(r.get("EndUTC", "")),
                AnalyzerCEMIDs=[c.strip() for c in r["Analyzer"].split(";") if c.strip()],
                Category=r.get("Category", ""), ReasonCode=r.get("ReasonCode", ""),
                Actor=r.get("Actor", ""), ActedAt=_dt(r["ActedAtUTC"]),
                Reason=r.get("Note", ""), CorrectiveAction=r.get("CorrectiveAction", ""),
                DetectionClass=r.get("DetectionClass", "")))
    return rows


def read_capsules(path: Path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(Capsule(
                Analyzer=r["Analyzer"], DetectionClass=r["DetectionClass"],
                CapsuleStartUTC=_dt(r["CapsuleStartUTC"]),
                CapsuleEndUTC=_dt(r["CapsuleEndUTC"])))
    return rows


def _floor_hour(d):
    return d.replace(minute=0, second=0, microsecond=0)


def _window(events, capsules):
    """Hour-aligned [start, end) covering every timestamp in the inputs.
    Derived from the data — no hardcoded dates."""
    stamps = []
    for e in events:
        stamps += [t for t in (e.ExtentStartUTC, e.ExtentEndUTC) if t]
    for c in capsules:
        stamps += [c.CapsuleStartUTC, c.CapsuleEndUTC]
    if not stamps:
        raise SystemExit("No timestamps found in list_a.csv / list_b.csv — nothing to run.")
    start = _floor_hour(min(stamps))
    end = _floor_hour(max(stamps))
    if end < max(stamps):
        end += timedelta(hours=1)
    return start, end


def run(a_path: Path, b_path: Path, roster_path: Path, out_dir: Path):
    analyzer_units = read_analyzer_units(roster_path)
    events = read_events(a_path)
    capsules = read_capsules(b_path)

    known = {au.Analyzer for au in analyzer_units}
    # Every analyzer referenced by the inputs must be in the roster, or its
    # rows would silently produce no cells. Fail loud instead.
    referenced = ({a for e in events for a in e.AnalyzerCEMIDs}
                  | {c.Analyzer for c in capsules})
    unknown = sorted(referenced - known)
    if unknown:
        raise SystemExit(f"These analyzers appear in list_a/list_b but not in "
                         f"roster.csv: {unknown}")

    start, end = _window(events, capsules)
    # Every unit operates across the full window; the offline gate is driven
    # by OperatingWindow rows in production. With none supplied, units are
    # assumed operating for the evaluated range.
    operating = [OperatingWindow(u, start, end)
                 for u in sorted({au.Unit for au in analyzer_units})]

    cells = build_grid(events, capsules, operating, analyzer_units, [],
                       CONFIG, start, end)
    records = build_list_c(events, capsules, cells, CONFIG.JitterToleranceMin,
                           analyzer_units=analyzer_units)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_grid(out_dir / "list_c_hours.csv", cells)
    write_list_c(out_dir / "list_c.csv", records)

    down = sum(1 for c in cells if c.Valid.name == "invalid")
    print(f"Window        : {start.isoformat()} .. {end.isoformat()} "
          f"({int((end - start).total_seconds() // 3600)} hours)")
    print(f"Analyzers     : {len(known)} in roster; "
          f"{len(referenced)} referenced by inputs")
    print(f"List A / List B: {len(events)} events, {len(capsules)} detections")
    print(f"Wrote {out_dir / 'list_c_hours.csv'}  ({len(cells)} hourly rows, {down} down)")
    print(f"Wrote {out_dir / 'list_c.csv'}        ({len(records)} enriched records)")
    return records, cells


def main(argv):
    a = Path(argv[1]) if len(argv) > 1 else Path("list_a.csv")
    b = Path(argv[2]) if len(argv) > 2 else Path("list_b.csv")
    roster = Path(argv[3]) if len(argv) > 3 else Path("roster.csv")
    out_dir = Path(argv[4]) if len(argv) > 4 else Path(".")
    for p in (a, b, roster):
        if not p.exists():
            raise SystemExit(f"Missing input file: {p}")
    run(a, b, roster, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
