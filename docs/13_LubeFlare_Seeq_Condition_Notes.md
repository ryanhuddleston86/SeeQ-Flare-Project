# Lube Flare — Seeq condition intel (from 2026-07-01 export)

**Source:** `SeeqExport_CBG_Flare_Calcs_Lube_Flare_CEMS_Downtime_0F175B6B….xlsx` · Window: 2026-05-31 20:25 → 2026-07-01 06:25 US/Eastern · Companion fixture: `capsules_lubeflare_real.csv` (15 capsules, UTC-converted, totals verified against the export's Statistics sheet: 9 h BTU, 22 h each H2S %/ppm).

## 1. The exported conditions are the OLD architecture, baked into Seeq

All three "Downtime (True/False)" conditions are **blended** — signal detection AND manual logbook entries merged inside the formula:

```
BTU:      ($InvalidHr OR $FailedValidation OR $ManualDown) AND (not $ManualNotDown) AND (not $ManualOffline)
H2S %/ppm:($InvalidHr OR $ManualDown)                      AND (not $ManualNotDown) AND (not $ManualOffline)
```

Two consequences:

- **`AND (not $ManualNotDown)` is the inference-based cancellation Rev 2 outlaws** — a "Manual GC Not Down" entry silently subtracts any overlapping detected downtime: no approval, no extent binding, no record of what was cancelled. This is the dismissal-annexation failure mode, running in production today. It is also the strongest demo exhibit available: same act, before (silent formula subtraction) vs. after (explicit dismissal, signed extent, permanent line).
- **These blended conditions are NOT what the clerk pulls in production.** The clerk wants the raw ingredients (below); manual judgment enters through the Events ledger, and operating gating through the operating signal. The blended capsules are fine as the *demo seed* (`DetectionClass=legacy-blended` in the fixture) because the union math is idempotent — but live wiring must target the ingredients.

## 2. The ingredient conditions (the real pull targets), with IDs

| Role | Condition name | Seeq ID |
|---|---|---|
| Raw invalid-hour, BTU/NHV | Lube Flare BTU Invalid Hour (True/False) | 0F15F910-D848-6000-937C-C9F5D193748D |
| Failed daily validation (MACT), BTU | Lube Flare BTU Failed Validation Hour MACT (True/False) | 0F15F9D9-EADA-73C0-BCEA-13712ADF4198 |
| Raw invalid-hour, H2S % | Lube Flare GC H2S Percent Invalid Hour (True/False) | 0F168EF2-7DE7-F980-A317-AD513D311A22 |
| Raw invalid-hour, H2S ppm | Lube Flare GC H2S ppm Invalid Hour (True/False) | 0F15093C-09E9-F930-80B2-443F270CB0BE |

Maps cleanly onto the spec's DetectionClass enum: Invalid-Hour conditions → `status-offline` class; Failed-Validation → `failed-daily-validation` class. Note H2S %/ppm have **no FailedValidation term** — only BTU carries the MACT validation condition. Confirm whether that's intentional scope or a gap.

**Legacy manual-layer conditions** (become Events-ledger inputs, then retire): Lube Flare Manual Offline (0F15078A-FF03-EA50-A0AC-C5E37E420319, shared across channels); per-channel Manual Down / Manual Not Down conditions (IDs in the export's Items sheet).

## 3. Operational facts that shape the clerk

- **Max capsule duration = 2.0 h on all three conditions** (export warning: `removeLongerThan` ineffective because max duration is 2.0h). Long outages arrive as chained/abutting capsules — the Jun 8–9 event lands as 12 h + 5 h abutting at exactly 12:00Z. The delta writer's merge/split handling is not theoretical; it fires on night one.
- **Capsules are hour-quantized** (all boundaries :00, whole-hour durations) — the "$InvalidHr" ingredients are already per-hour verdicts computed upstream in Seeq. Pull the InvalidHr formulas next session to see what hour rule they apply; the clerk's rule engine must not double-apply or contradict it.
- **GC status signal is spliced:** `FS:AIT35.ValCycle` forced to constant 1000 (healthy) before **2026-02-26** — status-based detection for lube flare cannot see earlier than that date. Any backfill before 2/26 needs a different evidence source.
- Status signal semantics: 1000 = healthy; Percent Good 99.9% over the window.

## 4. Still missing (blocks nothing this week, needed for live wiring)

- Per-unit **operating signal** name(s) — for flares, define what "operating" means for the gate (Manual Offline is the current stand-in).
- Real **CEMIDs** for the three channels — fixture uses placeholders `LUBEFLR-NHV-BTU / LUBEFLR-H2S-PCT / LUBEFLR-H2S-PPM`.
- **Logbook rows** — the .iqy is a live query pointer (list 674b3ee6… on sites/OperatorExtravaganza), not data; needs an authenticated export.
