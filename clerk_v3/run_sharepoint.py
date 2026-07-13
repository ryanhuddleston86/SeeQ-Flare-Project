"""
run_sharepoint.py — production run of the v3 clerk directly on the REAL
SharePoint Events export (via clerk.adapters), against a roster.

    Events.csv        (SharePoint Events list export)  -->  list_c.csv
    roster.csv        (analyzers, SharePoint identity)      list_c_hours.csv
    [list_b.csv]      (optional Seeq detections)
    [validations.csv] (optional validation stream -> OOC, App F 4.3.1)

Same fold as run_clerk.py; only the Events READER changes
(read_events_sharepoint). The three inputs are folded as ONE continuous
production pull — there is NO Scenario column and NO per-scenario grouping
anywhere in the pipeline (events/detections/validations are streams of
records, keyed only by Analyzer + time).

Optional inputs: List B (Seeq detections) and the validation stream are
each optional. If not given explicitly they are AUTO-DETECTED by filename
(`list_b.csv`, `validations.csv`) next to the Events file or in the current
directory. Omit them and the run is manual-log driven (a valid mode); with
no validations.csv, OOC simply does not fire.

Input formats (timestamps accept BOTH ISO-8601 and MM/DD/YYYY HH:MM:SS UTC):
  Events.csv      : the 22-column SharePoint Events export (see adapters.py)
  roster.csv      : Analyzer, Unit, Obligation, SeeqCovered, DiluentsRole,
                    DiluentSpecies, DiluentBasis  (Analyzer = 'Unit - Pollutant')
  list_b.csv      : Analyzer, DetectionClass, CapsuleStartUTC, CapsuleEndUTC
  validations.csv : Analyzer, StartUTC, EndUTC, Status
                    Status in {Pass, 2x_Fail, 4x_Fail}

Usage:
    python run_sharepoint.py EVENTS.csv ROSTER.csv OUTDIR [LIST_B.csv] [VALIDATIONS.csv]
    python run_sharepoint.py            # defaults to the SRU/Boiler_15 seed
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from clerk.adapters import (is_failed_validation,                            # noqa: E402
                            read_events_sharepoint)
from clerk.dar import dar_rollup, write_dar                                  # noqa: E402
from clerk.grid import (build_grid, detection_capsules,                      # noqa: E402
                        unit_offline_windows_from_capsules)
from clerk.listc import build_list_c, write_list_c                           # noqa: E402
from clerk.ooc import ValidationCapsule, compute_ooc_windows                 # noqa: E402
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


def _parse_ts(s):
    """Accept BOTH ISO-8601 (2026-04-02T09:00:00Z) and the SharePoint US
    format (04/02/2026 09:00:00), assumed UTC. Blank -> None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.strptime(s, "%m/%d/%Y %H:%M:%S").replace(tzinfo=timezone.utc)


def _read_capsules(path):
    """List B (Seeq detections): Analyzer, DetectionClass, CapsuleStartUTC,
    CapsuleEndUTC."""
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append(Capsule(r["Analyzer"], r["DetectionClass"],
                               _parse_ts(r["CapsuleStartUTC"]), _parse_ts(r["CapsuleEndUTC"])))
    return out


def _read_validations(path):
    """Validation-capsule stream (drives OOC, App F 4.3.1):
    Analyzer, StartUTC, EndUTC, Status  where Status in {Pass,2x_Fail,4x_Fail}."""
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append(ValidationCapsule(
                r["Analyzer"].strip(),
                _parse_ts(r["StartUTC"]), _parse_ts(r["EndUTC"]),
                r["Status"].strip()))
    return out


def run(events_path, roster_path, out_dir, list_b_path=None, validations_path=None,
        reporting_start=None, reporting_end=None):
    analyzer_units = read_analyzer_units(roster_path)
    events = read_events_sharepoint(events_path)
    all_capsules = _read_capsules(list_b_path) if list_b_path and Path(list_b_path).exists() else []
    val_caps = (_read_validations(validations_path)
                if validations_path and Path(validations_path).exists() else [])

    # Item 1: split SeeQ unit-offline capsules out of the detection stream.
    unit_offline = unit_offline_windows_from_capsules(all_capsules, analyzer_units)
    capsules = detection_capsules(all_capsules)

    known = {au.Analyzer for au in analyzer_units}
    referenced = ({a for e in events for a in e.AnalyzerCEMIDs}
                  | {c.Analyzer for c in all_capsules}
                  | {v.Analyzer for v in val_caps})
    unknown = sorted(referenced - known)
    if unknown:
        raise SystemExit(f"These analyzers appear in the inputs but not in the "
                         f"roster: {unknown}\n(the roster must key on the same "
                         f"'Unit - Pollutant' identity the inputs use)")

    stamps = [t for e in events for t in (e.ExtentStartUTC, e.ExtentEndUTC) if t]
    stamps += [t for c in all_capsules for t in (c.CapsuleStartUTC, c.CapsuleEndUTC)]
    stamps += [t for v in val_caps for t in (v.StartUTC, v.EndUTC) if t]
    if not stamps:
        raise SystemExit("No timestamps found in any input — nothing to run.")
    start = _floor_hour(min(stamps))
    end = _floor_hour(max(stamps))
    if end < max(stamps):
        end += timedelta(hours=1)

    # Item 4: fold the WHOLE window; the DAR reports on [reporting_start,
    # reporting_end). Default the reporting period to the full window.
    reporting_start = reporting_start or start
    reporting_end = reporting_end or end

    # OOC (App F 4.3.1) from the validation stream. Open tails held invalid
    # through the evaluation window end (the permanent rule).
    ooc_windows, ooc_flags = compute_ooc_windows(val_caps, open_tail_end=end)
    # Item 2: MQAQC windows = each validation-check window (per analyzer).
    mqaqc_windows = {}
    for v in val_caps:
        mqaqc_windows.setdefault(v.Analyzer, []).append((v.StartUTC, v.EndUTC))

    operating = [OperatingWindow(u, start, end)
                 for u in sorted({au.Unit for au in analyzer_units})]
    cells = build_grid(events, capsules, operating, analyzer_units, [], CONFIG,
                       start, end, ooc_windows=ooc_windows,
                       unit_offline_windows=unit_offline, mqaqc_windows=mqaqc_windows)
    records = build_list_c(events, capsules, cells, CONFIG.JitterToleranceMin,
                           analyzer_units=analyzer_units)
    dar = dar_rollup(cells, records, analyzer_units, reporting_start, reporting_end)

    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    write_grid(out_dir / "list_c_hours.csv", cells)
    write_list_c(out_dir / "list_c.csv", records)
    write_dar(out_dir / "dar_summary.csv", dar)

    failed_val = [e.EventID for e in events if is_failed_validation(e.Reason)]
    multi = [e.EventID for e in events if len(e.AnalyzerCEMIDs) > 1]
    print(f"Inputs: {len(events)} events, {len(capsules)} detections, "
          f"{len(unit_offline)} unit(s) with offline capsules, "
          f"{len(val_caps)} validation capsules ({len(referenced)} analyzers)")
    print(f"  multi-analyzer events split for the fold: {multi or 'none'}")
    print(f"  failed-validation Reason entries flagged: {failed_val or 'none'}")
    print(f"  OOC windows computed: "
          f"{ {a: len(w) for a, w in ooc_windows.items()} or 'none'}")
    for fl in ooc_flags:
        print(f"  OOC note [{fl.Analyzer}] {fl.kind}: {fl.detail}")
    print(f"Fold window: {start.isoformat()} .. {end.isoformat()} "
          f"({int((end-start).total_seconds()//3600)}h)")
    print(f"Reporting period: {reporting_start.isoformat()} .. {reporting_end.isoformat()}")
    for row in dar:
        print(f"  DAR {row.Analyzer} [{row.Obligation}]: op={row.OperatingHours}h "
              f"down={row.DowntimeHours}h ({row.DowntimePct}%) "
              f"{'>=5% DOWNTIME' if row.Flag5pctDowntime else ''} -> {row.ReportRequired}")
    print(f"Wrote {out_dir/'list_c_hours.csv'} ({len(cells)} rows), "
          f"{out_dir/'list_c.csv'} ({len(records)} records), "
          f"{out_dir/'dar_summary.csv'} ({len(dar)} rows)")
    return records, cells


def _sibling(events_path, name):
    """Auto-detect an optional input in the EVENTS FILE'S OWN directory only
    (never cwd — that could grab an unrelated file). Drop list_b.csv /
    validations.csv next to your Events file and they are picked up."""
    cand = Path(events_path).resolve().parent / name
    return cand if cand.exists() else None


def main(argv):
    ev = Path(argv[1]) if len(argv) > 1 else _HERE / "samples_sharepoint" / "Events_seed_SRU_Boiler15.csv"
    roster = Path(argv[2]) if len(argv) > 2 else _HERE / "samples_sharepoint" / "roster_sru_boiler15.csv"
    out = Path(argv[3]) if len(argv) > 3 else _HERE / "out_sharepoint"
    # Optional inputs: explicit 4th/5th args, else auto-detected by name.
    list_b = argv[4] if len(argv) > 4 else _sibling(ev, "list_b.csv")
    validations = argv[5] if len(argv) > 5 else _sibling(ev, "validations.csv")
    # Item 4: optional reporting-period bounds (ISO-8601 or MM/DD/YYYY).
    rep_start = _parse_ts(argv[6]) if len(argv) > 6 else None
    rep_end = _parse_ts(argv[7]) if len(argv) > 7 else None
    for p in (ev, roster):
        if not Path(p).exists():
            raise SystemExit(f"Missing input file: {p}")
    run(ev, roster, out, list_b, validations, rep_start, rep_end)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
