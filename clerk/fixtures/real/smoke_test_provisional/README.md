# PROVISIONAL smoke-test fixture set — NOT the demo seed

Built 2026-07-02 to run `run.py` (Step 6) once against real Lube Flare
capsule data, as a stretch goal after synthetic fixtures went fully green.
**This is a smoke test of the pipeline's plumbing, not a validated or
presentable compliance record.** Do not treat its output as more finished
than this file describes.

## What's real vs. placeholder, file by file

- **`capsules.csv`** — REAL. Byte-for-byte copy of
  `clerk/fixtures/real/capsules_lubeflare_drift_night2.csv` (the post-fix,
  7-capsule export; chosen over `capsules_lubeflare_real.csv` because that
  file is documented as containing the phantom Jun 8–9 capsule and is
  explicitly flagged "superseded as demo seed").
- **`pull_windows.csv`** — REAL. Night 2's window from
  `drift_pull_windows.csv` (2026-06-08T04:57:13Z – 2026-06-12T14:31:36Z).
- **`analyzer_units.csv`** — REAL analyzer/unit/coverage facts (the three
  Lube Flare channels → `LUBE_FLARE`, `SeeqCovered=true`), copied from the
  synthetic fixture — this mapping is established domain knowledge, not
  invented for this test.
- **`config.csv`** — REAL site config, copied from the synthetic fixture
  unmodified (site timezone, lookback, jitter, partial-hour applicability).
- **`operating.csv`** — **PLACEHOLDER, not real.** Per explicit direction:
  treats `LUBE_FLARE` as continuously operating for the exact span of
  Night 2's pull window. This is NOT sourced from any real Seeq operating
  condition — no such condition has been identified yet (see
  `docs/13_LubeFlare_Seeq_Condition_Notes.md`, "still missing: per-unit
  operating signal name(s)"). Any NOT-OPERATING cells outside this window
  are an artifact of the placeholder, not a real operating fact.
- **`events.csv`** — EMPTY (header only). No real Events ledger exists yet
  for Lube Flare (no authenticated logbook export, no real CEMIDs
  captured as ledger rows). Every grid cell's invalidity in this run comes
  from raw capsules only — there are no tickets, no adjudication, no
  dismissals to trace.
- **`qa_windows.csv`** — EMPTY. No real QA/CGA windows known for this data.
- **`prior_grid.csv`** — absent by design. This is necessarily a first run
  (no real prior grid exists) — diff.py reports everything as new.

## Consequences worth knowing before reading the output

- With no `events.csv` rows, every invalid hour's `ContributingEventIDs`
  in `grid.csv` will be capsule-provenance-only (`CAP:...` ids) — there is
  no ticket-side story to tell yet.
- The placeholder operating window means every hour outside
  2026-06-08T04:57:13Z–2026-06-12T14:31:36Z (but still inside the
  LookbackMonths window `run.py` evaluates) shows `NOT-OPERATING` — this
  is a placeholder artifact, not a claim about when Lube Flare was
  actually running.
