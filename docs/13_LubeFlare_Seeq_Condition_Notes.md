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

Maps onto the spec's DetectionClass enum with one wrinkle, **confirmed deliberate (Ryan, 2026-07-01)**: H2S %/ppm have no FailedValidation term because a failed daily validation drives the GC status signal to 0, and the Invalid-Hour condition absorbs it. Only BTU carries a separate explicit MACT validation condition. Consequences: (a) on H2S channels a failed validation arrives classed as `status-offline`, indistinguishable at the detection layer from a physical outage — dismissal eligibility is evidence-based per CEMS-DM-2026-01 (D-1..D-4), not class-based, so the design holds; (b) rule-engine branch (iv) selection for H2S channels cannot key off detection class; (c) the pre-2026-02-26 status splice therefore hides validation-failure history too, not just offline history. All three fold into the queued InvalidHr-formula review: what hour rule does InvalidHr apply, and does the clerk trust its verdicts as branches (i)/(ii)/(iv) or re-derive them from sub-hour data.

**Legacy manual-layer conditions** (become Events-ledger inputs, then retire): Lube Flare Manual Offline (0F15078A-FF03-EA50-A0AC-C5E37E420319, shared across channels); per-channel Manual Down / Manual Not Down conditions (IDs in the export's Items sheet).

## 3. Operational facts that shape the clerk

- **Max capsule duration = 2.0 h on all three conditions** (export warning: `removeLongerThan` ineffective because max duration is 2.0h). Long outages arrive as chained/abutting capsules — the Jun 8–9 event lands as 12 h + 5 h abutting at exactly 12:00Z. The delta writer's merge/split handling is not theoretical; it fires on night one.
- **Capsules are hour-quantized** (all boundaries :00, whole-hour durations) — the "$InvalidHr" ingredients are already per-hour verdicts computed upstream in Seeq. Pull the InvalidHr formulas next session to see what hour rule they apply; the clerk's rule engine must not double-apply or contradict it.
- **GC status signal is spliced:** `FS:AIT35.ValCycle` forced to constant 1000 (healthy) before **2026-02-26** — status-based detection for lube flare cannot see earlier than that date. Any backfill before 2/26 needs a different evidence source.
- Status signal semantics: 1000 = healthy; Percent Good 99.9% over the window.

## 3a. Resolved condition bug + the real-data drift pair (2026-07-01 evening)

The June 9 divergence was a condition bug, found and fixed by Ryan same evening. Post-fix re-export (Jun 8–12 window) shows: the phantom **12 h overnight capsule (Jun 8 20:00 → Jun 9 08:00 ET) on both H2S channels is gone**; BTU capsules unchanged; the blended Downtime formulas and parameter IDs are byte-identical across the two exports — the fix landed **upstream** (in an InvalidHr ingredient or a manual condition), which is consistent with the architecture.

The two exports are now the **real-data drift pair**: `capsules_lubeflare_drift_night1.csv` (pre-fix, 15 capsules, 30-day pull) and `capsules_lubeflare_drift_night2.csv` (post-fix, 7 capsules, 4.4-day pull), with `drift_pull_windows.csv` carrying each pull's window. Expected delta-writer behavior night 1 → night 2: (a) the phantom Jun 8–9 episode on H2S-PCT and H2S-PPM → **Withdrawn** (machine-attributed ✗→✓ in the diff, digest-informational); (b) all in-window episodes re-associate with **zero events** (exact match); (c) the Jun 16 episodes lie **outside night 2's pull window and must NOT be withdrawn** — this pair is the concrete test for the withdrawal-window-scoping rule (spec open item 7), and it requires pull-window metadata as a fixture/schema input.

**Open design question surfaced by this data:** pre-fix, the H2S Jun 8–9 event arrived as two *abutting* capsules (touching at 12:00Z, zero overlap). Strict temporal-overlap matching makes those two tickets, and night 2 then withdraws fragment 1 while fragment 2 exact-matches. Alternative: treat abutment as association (one ticket per physical episode). Strict is the fail-safe default (nothing lost, mildly noisier queue); Ryan to rule.

**Canonical demo seed:** the original 30-day fixture (`capsules_lubeflare_real.csv`) contains the phantom — superseded for seeding. Regenerate from a fresh full-window post-fix export when Ryan runs one.

## 4. Still missing (blocks nothing this week, needed for live wiring)

- Per-unit **operating signal** name(s) — for flares, define what "operating" means for the gate (Manual Offline is the current stand-in).
- Real **CEMIDs** for the three channels — fixture uses placeholders `LUBEFLR-NHV-BTU / LUBEFLR-H2S-PCT / LUBEFLR-H2S-PPM`.
- **Logbook rows** — the .iqy is a live query pointer (list 674b3ee6… on sites/OperatorExtravaganza), not data; needs an authenticated export.
