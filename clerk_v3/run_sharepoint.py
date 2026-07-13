"""
run_sharepoint.py — production run of the v3 clerk directly on the REAL
SharePoint Events export (via clerk.adapters), against a roster.

    Events_seed_*.csv   (SharePoint Events list export)  -->  list_c.csv
    roster.csv          (analyzers, SharePoint identity)      list_c_hours.csv
    [list_b.csv]        (optional Seeq detections)

Same fold as run_clerk.py; only the READER changes (read_events_sharepoint
instead of the simplified reader). SharePoint Events are List A only, so
List B (Seeq detections) is optional — omit it and the run is manual-log
driven, which is a valid production mode.

Usage:
    python run_sharepoint.py EVENTS.csv ROSTER.csv [OUTDIR] [LIST_B.csv]
    python run_sharepoint.py            # defaults to the SRU/Boiler_15 seed
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from clerk.adapters import (analyzer_unit_obligation, is_failed_validation,  # noqa: E402
                            read_events_sharepoint)
from clerk.grid import build_grid                                            # noqa: E402
from clerk.listc import build_list_c, write_list_c                           # noqa: E402
from clerk.schemas import (Capsule, OperatingWindow, SiteConfig,             # noqa: E402
                           read_analyzer_units, write_grid)
import csv                                                                   # noqa: E402
from datetime import datetime, timezone                                      # noqa: E402

CONFIG = SiteConfig(
    SiteTimeZoneIANA="America/New_York",
    LookbackMonths=8, LateXThresholdDays=7, JitterToleranceMin=5,
    PartialOperatingHourApplicability={},
    ReasonParagraphMap={"MM-01": "(i)", "NM-01": "(i)", "QA-01": "(iii)",
                        "OK-01": "(i)", "UK-01": "(i)"},
)


def _floor_hour(d):
    return d.replace(minute=0, second=0, microsecond=0)


def _read_capsules(path):
    def dt(s):
        s = (s or "").strip()
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append(Capsule(r["Analyzer"], r["DetectionClass"],
                               dt(r["CapsuleStartUTC"]), dt(r["CapsuleEndUTC"])))
    return out


def run(events_path, roster_path, out_dir, list_b_path=None):
    analyzer_units = read_analyzer_units(roster_path)
    events = read_events_sharepoint(events_path)
    capsules = _read_capsules(list_b_path) if list_b_path and Path(list_b_path).exists() else []

    known = {au.Analyzer for au in analyzer_units}
    referenced = ({a for e in events for a in e.AnalyzerCEMIDs}
                  | {c.Analyzer for c in capsules})
    unknown = sorted(referenced - known)
    if unknown:
        raise SystemExit(f"These analyzers appear in the Events export but not in the "
                         f"roster: {unknown}\\n(the roster must key on the same "
                         f"'Unit - Pollutant' identity the export uses)")

    stamps = [t for e in events for t in (e.ExtentStartUTC, e.ExtentEndUTC) if t]
    stamps += [t for c in capsules for t in (c.CapsuleStartUTC, c.CapsuleEndUTC)]
    start = _floor_hour(min(stamps))
    end = _floor_hour(max(stamps))
    if end < max(stamps):
        end += timedelta(hours=1)

    operating = [OperatingWindow(u, start, end)
                 for u in sorted({au.Unit for au in analyzer_units})]
    cells = build_grid(events, capsules, operating, analyzer_units, [], CONFIG, start, end)
    records = build_list_c(events, capsules, cells, CONFIG.JitterToleranceMin,
                           analyzer_units=analyzer_units)

    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    write_grid(out_dir / "list_c_hours.csv", cells)
    write_list_c(out_dir / "list_c.csv", records)

    failed_val = [e.EventID for e in events if is_failed_validation(e.Reason)]
    multi = [e.EventID for e in events if len(e.AnalyzerCEMIDs) > 1]
    print(f"Read SharePoint Events: {len(events)} events "
          f"({len(referenced)} analyzers referenced)")
    print(f"  multi-analyzer events split for the fold: {multi or 'none'}")
    print(f"  failed-validation entries flagged: {failed_val or 'none'}")
    print(f"Window: {start.isoformat()} .. {end.isoformat()} "
          f"({int((end-start).total_seconds()//3600)}h)")
    print(f"Wrote {out_dir/'list_c_hours.csv'} ({len(cells)} rows) and "
          f"{out_dir/'list_c.csv'} ({len(records)} records)")
    return records, cells


def main(argv):
    ev = Path(argv[1]) if len(argv) > 1 else _HERE / "samples_sharepoint" / "Events_seed_SRU_Boiler15.csv"
    roster = Path(argv[2]) if len(argv) > 2 else _HERE / "samples_sharepoint" / "roster_sru_boiler15.csv"
    out = Path(argv[3]) if len(argv) > 3 else _HERE / "out_sharepoint"
    list_b = argv[4] if len(argv) > 4 else None
    for p in (ev, roster):
        if not Path(p).exists():
            raise SystemExit(f"Missing input file: {p}")
    run(ev, roster, out, list_b)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
