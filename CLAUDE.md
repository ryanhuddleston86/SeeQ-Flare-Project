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
