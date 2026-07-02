# SeeQ Flare — CEMS Clerk

Build spec: `12_CEMS_Clerk_Spec_ClaudeCode.md` (uploaded to session; implements Architecture Rev 2)

Prior art: `list_c_recalc.py` — pure-fold spike (8/8 tests); extends into `clerk/clerk/fold.py` (Step 2).

## Build order

**schemas → rules → fold → grid → delta → diff/digest → run**

One module at a time. Tests green before moving to the next. Log the stopping point at each boundary.

## Hard stops (§5) — surface to Ryan and wait

- Any conflict between pasted eCFR verbatim text and spec paraphrase
- Any change to rule-branch precedence
- Any case where fail-safe polarity and a rule reading disagree
- Any temptation to add a status field, edit path, or Seeq-side state

## Rules module

Fetch 40 CFR 60.13(h)(2) verbatim from eCFR before implementing any branch.
Paste the text into each branch docstring — then **pause for Ryan to review** before writing the branch logic.
Each `[VERIFY]` marker in the spec is a hard stop: paste text, re-derive vectors, reconcile before coding.

## Domain facts — lube flare (from `docs/13_LubeFlare_Seeq_Condition_Notes.md`)

- One GC instrument feeds all three lube flare channels (LUBEFLR-NHV-BTU,
  LUBEFLR-H2S-PCT, LUBEFLR-H2S-PPM). Maintenance on one usually downs all three.
- Per-channel windows still diverge for the same physical event — that is
  **correct**; each analyzer's grid is its own compliance fact.
  **NEVER correlate or merge capsules across analyzers.** One physical event may
  produce multiple tickets; the delta writer stays per analyzer + class.
- A manual event listing multiple CEMIDs contributes its window to every listed
  analyzer's union (spec Step 4).
- `DetectionClass` is an **opaque matching key**, not an enum — real exports use
  `legacy-blended`; never branch on its value.
- Upstream max capsule duration is 2 h — long outages arrive as abutting
  fragments. The delta writer's merge path runs on night one, not as an edge case.
- Real data (`clerk/fixtures/real/`) feeds the §6 smoke test only. Synthetic
  fixtures remain the test basis; never fit code to the real file's quirks.
  Exception by Ryan's direction: the drift pair
  (`capsules_lubeflare_drift_night{1,2}.csv` + `drift_pull_windows.csv`) is the
  concrete integration test for withdrawal-window scoping.
- **Pull-window scoping (spec open item 7, now concrete):** the delta writer
  takes a pull window per run; Withdrawn may only be emitted for episodes fully
  INSIDE that window. Absence outside the pulled range is not evidence.
- **Abutting capsules: strict overlap = two tickets until Ryan rules
  otherwise** — every abutment is flagged, never silently decided.
- `capsules_lubeflare_real.csv` contains the phantom Jun 8–9 capsule
  (pre-fix export) — superseded as demo seed; regenerate post-fix.

## Repository shape

```
clerk/
  fixtures/          synthetic input CSVs — schema mirrors production exactly
  clerk/
    schemas.py       dataclasses + CSV readers/writers (the only I/O layer)
    fold.py          Step 2 — extends list_c_recalc
    rules.py         Step 5 — rule engine (pure, no I/O)
    grid.py          Steps 4–5 — union, provenance, cell evaluation
    delta.py         Step 3a — delta writer
    diff.py          Step 7 — grid diff + alarm taxonomy
    digest.py        Step 7 — morning digest renderer
    run.py           orchestrator: steps 1→7 against a fixture directory
  tests/
  out/<run_date>/    all outputs, one dir per run; inputs snapshotted here too
```
