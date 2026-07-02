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

## Grid module (Ryan, 2026-07-02)

- Union = live detections (`capsules.csv`) ∪ folded windows ∪ QA windows,
  minus signed-dismissal extents where a live capsule still matches
  (jitter tolerance) — subtracts exactly the *signed* interval, never the
  capsule's own wider one, so excess beyond it stays invalid automatically.
- Folded windows split by ORIGIN type: TechEntry → `manual_qa_windows`
  (branch-4 trigger, joined by `qa_windows.csv`); SeeqDetection →
  `detected_invalid_windows` (subtract-only, never a trigger).
- `contributing_observation_windows` is the direct, independently tested
  implementation of Guarantee A (fold.py handoff) — every status except
  Dismissed/Withdrawn/Superseded contributes identically; nothing
  downstream may re-narrow it to "only Confirmed counts."
- The signed-dismissal subtraction only ever touches
  `detected_invalid_windows` — folded windows are already Guarantee-A-
  filtered (Dismissed contributes nothing there already), but capsules are
  ticket-status-agnostic by design, so they need the explicit subtraction.
- **Grid's own correctness guard (not explicitly requested, added because
  its absence is a real bug):** only SeeqDetection-origin Dismissed
  observations feed the subtraction list — TechEntry-origin dismissals
  have no capsule counterpart and must not cancel an unrelated ticket's
  detected time at the same analyzer.
- **RESOLVED (Ryan, 2026-07-02):** capsule-match for the subtraction is now
  analyzer+DetectionClass. `fold.py`'s `Observation` gained a nullable
  `detection_class` field (sourced from the origin event's `DetectionClass`
  column, blank for TechEntry origins) so grid.py can key on it. A
  dismissal signed against one class can no longer be corroborated by an
  unrelated class's live capsule at the same analyzer — extends the
  existing TechEntry-never-subtracts test rather than replacing it.
- **Fixture correctness fix (same session):** `events.csv` E003 said
  `status-offline` but its actual corresponding capsule
  (`capsules.csv`, Jan 20 08:00–12:00) is `failed-daily-validation` — and
  E004's dismissal reason ("Instrument calibration in progress") confirms
  that's the right class. Fixed to match; same bug class as the earlier
  E005 target fix, surfaced by the stricter matching.
- **Flagged, not blocking:** no daily-calibration event source exists yet
  — branch (iv) never fires via `build_grid` until that ingestion path is
  defined.
- Operating gate confirmed source-agnostic per spec: `OperatingWindow`
  carries no "how do we know this" field, so Lube Flare's manual-capsule-
  sourced operating signal and a continuous-signal unit hit the identical
  code path. No fixture or code changes needed for this.
- Fixture sweep (Ryan's ask, same session): confirmed E005 was the only
  chain-walking violation in `events.csv` — every other `TargetEventID`
  already referenced its observation's origin directly.

## Diff module (Ryan, 2026-07-02) — gate G5

- **Provenance check requested before building the trace logic:**
  `RuleApplied` is branch-level only, not usable for flip attribution (and
  G5 doesn't need it — only Valid transitions matter). `ContributingEventIDs`
  IS sufficient for the traceable cases via `Observation.contributing_event_ids`.
- **FLAGGED, not blocking — a real gap found by the check:**
  `ContributingEventIDs` is sourced only from folded Observations, never
  from raw capsule-only contributions. An hour invalid purely from a live
  capsule with no ticket yet (plausible before `delta.py` runs) has EMPTY
  `ContributingEventIDs` even though it's genuinely invalid. diff.py
  handles this safely (empty list on a ✗→✓ flip → conservative
  `INTEGRITY ALERT`, never a silent pass) but not correctly — a real fix
  needs a per-capsule identifier grid.py doesn't have. Flag for Ryan.
- Every ✗→✓ flip resolves to exactly one of three outcomes, never a
  fourth: silent pass (Dismissed + signed extent covers the hour),
  machine-informational (Withdrawn or BoundaryUpdate, both gated on
  `Actor == delta.MACHINE_ACTOR`), or INTEGRITY ALERT.
- **This is where the unapproved-flip safety check fold.py deferred
  lives:** a Correction (human path) moving a boundary such that an hour
  flips valid, without going through the machine BoundaryUpdate path,
  falls through to INTEGRITY ALERT structurally — no special-casing of
  Correction's polarity needed.
- Multiple contributing tickets on one flipped cell: ALL must resolve to
  silent-pass for the overall flip to be silent; any mix of silent-pass +
  machine-informational rolls the whole flip up to informational (surfaced,
  not hidden); any single unexplained ticket taints the whole flip to
  INTEGRITY ALERT.
- Late-arrival check is independent of flip-tracing — a new invalid cell
  older than `LateXThresholdDays` (site-local calendar day, strictly older
  than the threshold) pings regardless of whether it's also part of a flip.
- `delta.MACHINE_ACTOR` ("clerk-delta") promoted from a literal to a named
  constant so diff.py doesn't duplicate the magic string.

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

## Fold module (Ryan, 2026-07-02)

- EventType grew four machine-authored values past spec Step 2's original
  text: `BoundaryUpdate`, `Withdrawn`, `Superseded`, `DismissalRejected`.
  Effects: `Confirmation`/`DismissalProposed`/`DismissalRejected`/`Reopen`/
  `Withdrawn`/`Superseded` each set a fixed status; `Approval` sets
  Dismissed and records **its own stated extent** as
  `signed_dismissal_extent` (ledger 17 — never the target's current
  extent); `BoundaryUpdate`/`Correction` update current extent only,
  status unchanged.
- **Grouping is flat:** `TargetEventID` always references the observation's
  ORIGIN EventID directly, never a chain — even for events that
  conceptually respond to an intermediate event (e.g. `Approval` targets
  origin, not the `DismissalProposed`). No chain-walking in fold.py.
- **Two governing principles:** (1) fold does not re-validate upstream
  business rules — replay honestly, trust the stream; a rule like
  "confirmed tickets never withdraw" is delta.py's job to enforce on
  write, not fold's to re-check. (2) Status never gates whether an
  observation's extent counts as invalid time downstream (Guarantee A) —
  fold returns every observation regardless of status; filtering is
  grid.py's job (Step 4). No "only Confirmed counts" filter anywhere.
- Reductive Corrections are folded without judgment — no gating/classifying
  in fold.py. The unapproved-flip safety check lives in diff.py (Step 7).
- **Flagged, not blocking:** TechEntry's initial status while
  `CorrectiveAction` is blank is a guess (Needs review until corrective
  action is present, then Confirmed) — low stakes since status never
  touches grid math. Flag for Ryan if the real capture flow differs.
- The spec lists `Corrected` as a possible status; no current event effect
  reaches it (`Correction` explicitly leaves status unchanged) — kept in
  the enum for spec fidelity, flagged as presently unreachable.

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
