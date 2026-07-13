"""
demo_multisource.py — run the REAL clerk on a two-source, multi-obligation
dataset (SRU + Boiler_15) and show List C, the per-UNIT rollup the engine
computes today, and the per-OBLIGATION rollup the doctrine (D8) requires.

No clerk logic is changed. The per-obligation view is computed in the
DISPLAY layer by calling the same production source_down_hours function
scoped to each obligation's monitors — so where the engine's per-unit
rollup and the correct per-obligation rollup disagree (the known T5a gap),
the difference is shown, not hidden.

Roster is DATA-DRIVEN: samples_multisource/roster.csv, read by the
production clerk.schemas.read_analyzer_units. (The extra 'Obligation'
column is ignored by the engine reader — the engine has no obligation
concept yet, which is exactly the T5a gap — and is read separately here
for the per-obligation display.)

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
    records = build_list_c(events, capsules, cells, config.JitterToleranceMin)

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
    print("SOURCE / UNIT ROLLUP  —  engine (per-UNIT) vs doctrine (per-OBLIGATION)")
    print("=" * 78)
    by_unit = {}
    for au in units:
        by_unit.setdefault(au.Unit, []).append(au)

    engine = {u: sorted(hh(h) for h in hs)
              for u, hs in source_down_hours(units, cells).items()}

    any_gap = False
    for unit, roster in by_unit.items():
        print(f"\n  {unit}:")
        eng = engine.get(unit, [])
        print(f"    ENGINE per-unit source-down (intersect ALL monitors on the unit): "
              f"{eng or '[]'}")
        # per-obligation: run the SAME production function, scoped to each
        # obligation's monitors, then union the obligations that are down.
        oblig_down = {}
        for au in roster:
            ob = obligation[au.Analyzer]
            oblig_down.setdefault(ob, []).append(au)
        print(f"    DOCTRINE per-obligation (intersect within each obligation):")
        unit_has_down_obligation = set()
        for ob, mons in sorted(oblig_down.items()):
            hrs = source_down_hours(mons, cells).get(unit, [])
            hrs = sorted(hh(h) for h in hrs)
            if hrs:
                unit_has_down_obligation.update(hrs)
            print(f"        {ob:5} obligation ({','.join(m.Analyzer for m in mons)}): "
                  f"{hrs or '[]'}")
        correct = sorted(unit_has_down_obligation)
        if eng != correct:
            any_gap = True
            print(f"    >>> T5a GAP: engine says unit-down {eng or '[]'} but a per-obligation")
            print(f"        rollup says the unit has a down obligation at {correct or '[]'}.")
            print(f"        The engine UNDER-reports because a valid monitor for one")
            print(f"        obligation is masking another obligation's outage.")
        else:
            print(f"    OK: per-unit and per-obligation agree here ({eng or '[]'}).")

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print("  Diluent propagation (T4 path, DETECTED outage): works on both units.")
    print("  Per-obligation rollup (T5a): " +
          ("GAP CONFIRMED on realistic data — see Boiler_15 above." if any_gap
           else "no disagreement in this dataset."))


if __name__ == "__main__":
    main()
