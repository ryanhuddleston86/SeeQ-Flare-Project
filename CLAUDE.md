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

## SeeqCovered + NOT-ASSESSED (Ryan, 2026-07-02)

- `SeeqCovered` (bool) per analyzer in `analyzer_units.csv` — default **false**
  unless explicitly `true`. Lube Flare's three channels are true; all other
  fixture analyzers false unless a test needs otherwise.
- Rule-engine branch 5 (normal-hour quadrant test) may ONLY fire when
  `SeeqCovered=true`. Uncovered + no branch 2/3/4 claim → `NOT-ASSESSED`
  (fourth Valid state alongside 0/1/NOT-OPERATING). Branches 2–4 ignore
  coverage — they never depended on Seeq detection.
- **HARD STOP (open):** how NOT-ASSESSED rolls into availability% or the DAR
  denominator is undecided — flag and wait for Ryan; nothing downstream may
  interpret the state.

## Rules module

Verbatim source: `docs/14_CFR_60_13_h2_Verbatim.md` (source-verified from
eCFR 2026-07-01; section unchanged since 1/03/2017). Branch verdicts
implemented 2026-07-02; precedence confirmed, no longer provisional for
(i)–(iv). G4/G9/G10 vectors re-derived from the verbatim text — one spec
arithmetic slip corrected (G4 second-window span is 60 min via the
separation test, not 25; verdict unchanged).

- (iii)(A) canonical statement is the literal two-point-separation test;
  `max(V)−min(V) ≥ 15 min` is the documented implementation form (dense-
  sampling equivalence, docs/14). Boundary vector pinned in tests.
- Provisions (v)–(viii) are informational, OUT OF SCOPE for rules.py —
  (vi)/(vii) likely EMP/PI averaging territory (see docs/14 before assuming).

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
- **Abutment RULED (Ryan, 2026-07-02):** at capsule ingestion — before
  matching against history — same-analyzer + same-class capsules with zero
  gap coalesce into one candidate episode. Episode-vs-capsule abutment across
  the history boundary remains strict no-match, flagged not decided.
- **Machine-authored events carry their class in the Events ledger's dedicated
  `DetectionClass` column** (blank for human-authored EventTypes). Category is
  unused — do not repurpose it.
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
