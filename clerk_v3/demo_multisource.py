"""
demo_multisource.py (v3) — run the REAL clerk on a two-source,
multi-obligation dataset (SRU + Boiler_15) and show that BOTH gaps found on
this data in v2 are now closed:

  Gap #1 (T5a): source_down_hours groups by (Unit, Obligation), so a valid
  monitor for one pollutant never masks another pollutant's outage.
  Gap #2: build_list_c(analyzer_units=...) emits records for diluent-
  propagated downtime, so the standalone List C matches the hourly grid.

Roster is DATA-DRIVEN: samples_multisource/roster.csv, read by the
production clerk.schemas.read_analyzer_units — now including the Obligation
(pollutant) column the engine consumes in v3.

Inputs are the same format as the demo notebook:
  list_a.csv  events (List A)      list_b.csv  detections/capsules (List B)
"""
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clerk.grid import build_grid, source_down_hours
from clerk.listc import build_list_c
from clerk.schemas import (Capsule, CellValid, Event, EventType,
                           OperatingWindow, SiteConfig, read_analyzer_units)

HERE = Path(__file__).parent / "samples_multisource"


def _dt(s):
    s = s.strip()
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def read_events(path):
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append(Event(
                EventID=r["EventID"], EventType=EventType(r["Type"]),
                TargetEventID=r["TargetEventID"] or None,
                ExtentStartUTC=_dt(r["StartUTC"]), ExtentEndUTC=_dt(r["EndUTC"]),
                AnalyzerCEMIDs=[r["Analyzer"]], Category=r["Category"],
                ReasonCode=r["ReasonCode"], Actor=r["Actor"],
                ActedAt=_dt(r["ActedAtUTC"]), Reason=r["Note"],
                CorrectiveAction=r["CorrectiveAction"], DetectionClass=r["DetectionClass"]))
    return out


def read_caps(path):
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append(Capsule(r["Analyzer"], r["DetectionClass"],
                               _dt(r["CapsuleStartUTC"]), _dt(r["CapsuleEndUTC"])))
    return out


def obligations_of(path):
    with open(path, newline="") as f:
        return {r["Analyzer"]: (r.get("Obligation") or "").strip()
                for r in csv.DictReader(f)}


def hh(dt):
    return dt.strftime("%H:%M")


def main():
    units = read_analyzer_units(HERE / "roster.csv")
    obligation = obligations_of(HERE / "roster.csv")
    events = read_events(HERE / "list_a.csv")
    capsules = read_caps(HERE / "list_b.csv")

    day = datetime(2026, 4, 2, tzinfo=timezone.utc)
    start, end = day + timedelta(hours=5), day + timedelta(hours=16)

    # Both units operating the whole window.
    operating = [OperatingWindow(u, start, end) for u in {au.Unit for au in units}]
    config = SiteConfig(SiteTimeZoneIANA="America/New_York", LookbackMonths=8,
                        LateXThresholdDays=7, JitterToleranceMin=5,
                        PartialOperatingHourApplicability={},
                        ReasonParagraphMap={"MM-01": "(i)", "QA-01": "(iii)"})

    cells = build_grid(events, capsules, operating, units, [], config, start, end)
    # v3 Gap #2: pass the roster so diluent-propagated downtime becomes
    # List C records on the dependent analyzers.
    records = build_list_c(events, capsules, cells, config.JitterToleranceMin,
                           analyzer_units=units)

    def down(analyzer):
        return sorted(hh(c.HourStartUTC) for c in cells
                      if c.Analyzer == analyzer and c.Valid is CellValid.invalid)

    print("=" * 78)
    print("ROSTER (data-driven — samples_multisource/roster.csv)")
    print("=" * 78)
    print(f"{'Analyzer':10} {'Unit':10} {'Obligation':11} {'Role':20} {'Diluent basis'}")
    for au in units:
        print(f"{au.Analyzer:10} {au.Unit:10} {obligation[au.Analyzer]:11} "
              f"{au.DiluentsRole:20} {au.DiluentBasis or '—'}")

    print("\n" + "=" * 78)
    print("PER-ANALYZER DOWN HOURS (from the hourly grid)")
    print("=" * 78)
    for au in units:
        d = down(au.Analyzer)
        tag = ""
        if au.DiluentsRole == "diluent-corrected" and d:
            tag = "  <- includes hours PROPAGATED from its O2 diluent"
        print(f"  {au.Analyzer:10} ({obligation[au.Analyzer]:4}): {d or '[]'}{tag}")

    print("\n" + "=" * 78)
    print("LIST C — enriched records")
    print("=" * 78)
    for r in records:
        oblig = obligation.get(r.Analyzer, "?")
        print(f"\n  {r.Analyzer} [{oblig}]  state={r.State}  source={r.SourceUsed}")
        print(f"     resolved:  {' + '.join(f'{s:%H:%M}-{e:%H:%M}' for s,e in r.ResolvedWindows)}"
              f"  ({r.ResolvedMinutes:.0f} min)")
        print(f"     reason:    {r.ReasonCode or '—'}   note: {r.Note or '—'}")
        print(f"     approver:  {r.ApproverName or '—'} / {r.ApproverDecision or '—'}")
        print(f"     paragraph: {';'.join(r.GoverningParagraphs) or '—'}   "
              f"down hours: {[hh(h) for h in r.DownHours]}")
        print(f"     provenance: {';'.join(r.ContributingRecords)}")

    print("\n" + "=" * 78)
    print("SOURCE ROLLUP — v3 per-OBLIGATION (Gap #1 fix)")
    print("=" * 78)
    print("  source_down_hours now returns {(Unit, Obligation): [hours]} —")
    print("  the intersection is WITHIN each obligation, never across the unit.\n")
    rollup = source_down_hours(units, cells)
    by_unit = {}
    for au in units:
        by_unit.setdefault(au.Unit, set()).add(au.Obligation)
    for unit in sorted(by_unit):
        print(f"  {unit}:")
        for ob in sorted(by_unit[unit]):
            hrs = sorted(hh(h) for h in rollup.get((unit, ob), []))
            print(f"      {ob:4} obligation: {hrs or '[] (covered)'}")

    # The v2 discriminator: Boiler_15 NOx-only outage 06-08 with O2/CO valid.
    b15_nox = sorted(hh(h) for h in rollup.get(("Boiler_15", "NOx"), []))
    print("\n  Gap #1 check (Boiler_15 NOx obligation, O2 & CO valid at 06-08):")
    print(f"      v2 per-unit intersection reported: []  (valid CO/O2 masked it)")
    print(f"      v3 per-obligation reports NOx down: {b15_nox}")
    print("      -> " + ("CLOSED: NOx obligation correctly down at 06:00/07:00."
                          if b15_nox[:2] == ["06:00", "07:00"] else "STILL WRONG."))

    print("\n" + "=" * 78)
    print("GAP #2 check — dependent analyzers now carry propagated List C records")
    print("=" * 78)
    rec_analyzers = {r.Analyzer for r in records}
    grid_down = {c.Analyzer for c in cells if c.Valid is CellValid.invalid}
    missing = sorted(grid_down - rec_analyzers)
    print(f"  analyzers DOWN in grid:        {sorted(grid_down)}")
    print(f"  analyzers WITH a List C record: {sorted(rec_analyzers)}")
    print(f"  DOWN in grid but NO record:     {missing or '[] (none — Gap #2 closed)'}")
    for r in records:
        if "propagated" in r.SourceUsed:
            print(f"    propagated record: {r.Analyzer} {[hh(h) for h in r.DownHours]} "
                  f"<- {r.SourceUsed}")

    print("\n" + "=" * 78)
    print("SUMMARY (v3)")
    print("=" * 78)
    print("  Gap #1 (per-obligation rollup): " +
          ("CLOSED." if b15_nox[:2] == ["06:00", "07:00"] else "OPEN."))
    print("  Gap #2 (propagated downtime in List C): " +
          ("CLOSED." if not missing else f"OPEN — missing {missing}."))


if __name__ == "__main__":
    main()
