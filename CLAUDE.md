# SeeQ Flare — CEMS Clerk

Build spec: `12_CEMS_Clerk_Spec_ClaudeCode.md` (uploaded to session; implements Architecture Rev 2)

Prior art: `list_c_recalc.py` — pure-fold spike (8/8 tests); extends into `clerk/clerk/fold.py` (Step 2).

## Build order

**schemas → rules → fold → grid → delta → diff/digest → run**

One module at a time. Tests green before moving to the next. Log the stopping point at each boundary.

## Run module (Ryan, 2026-07-02) — Step 6, full synthetic green

- The clerk never writes to the ledger (delta.py's own docstring):
  tonight's grid/diff/digest reflect the EXISTING `events.csv` only;
  `delta.new_events` are proposals for the external bridge to append,
  picked up on the NEXT run — never folded into tonight's own grid.
- Three flagged, non-blocking interpretive choices to hit tonight's
  green target (none touch a §5 hard-stop category):
  1. **Run window from LookbackMonths:** `_months_before` snaps to the 1st
     of the month N months back — not exact day-for-day, sidesteps
     end-of-month clamping. Used VERBATIM from config (8, in the synthetic
     fixture) rather than shrunk for convenience, even though most of the
     resulting window predates any `operating.csv` data and renders as a
     large block of correct-but-trivial NOT-OPERATING rows (~30k of the
     ~37k synthetic grid rows).
  2. **`adjudicated_condition.csv` shape:** the spec's field list has no
     explicit start/end, but "one capsule per episode" structurally
     implies bounds — added `ConditionStartUTC`/`ConditionEndUTC`.
     `RuleApplied` is the sorted set of DISTINCT branch labels observed
     across the episode's hours (an episode can span multiple branches),
     not one reduced value. Rows emitted for EVERY folded Observation
     regardless of status — read as the full adjudicated record, not
     filtered to active tickets.
  3. **`pi_payload.csv` Value encoding:** valid/invalid → 1.0/0.0.
     NOT-OPERATING and NOT-ASSESSED are left BLANK rather than assigned a
     number — NOT-ASSESSED's encoding is directly entangled with the
     open NOT-ASSESSED rollup hard stop; inventing a number here would
     silently resolve it. `Tag` = `Analyzer` 1:1 (no real PI tag-mapping
     table exists yet).
- See the SeeqCovered/NOT-ASSESSED section below for the material finding
  this run surfaced (Confirmed-but-uncovered → NOT-ASSESSED) — a new hard
  stop, not fixed here.
- `output_label`, when set, renders as a VISIBLE markdown heading + rule at
  the top of `digest.md` — deliberately not an HTML comment. A label
  nobody can see doesn't mark output as provisional.

### Real-data stretch smoke test (2026-07-02, after synthetic went green)

- Input set: `clerk/fixtures/real/smoke_test_provisional/` (own README.md
  inside — full real/placeholder breakdown per file). Chose the drift pair's
  **Night 2** (post-fix, 7 capsules) over `capsules_lubeflare_real.csv`,
  since the latter is documented as containing the phantom Jun 8–9 capsule
  and flagged "superseded as demo seed." `events.csv` is empty — no real
  Events ledger exists yet for Lube Flare.
- `operating.csv` is an explicit PLACEHOLDER per direction: `LUBE_FLARE`
  continuously operating for exactly Night 2's pull window
  (2026-06-08T04:57:13Z–2026-06-12T14:31:36Z), not sourced from any real
  Seeq operating condition (none identified yet — docs/13's "still
  missing" list).
- Output: `clerk/out/2026-06-13-lubeflare-real-smoke-test-PROVISIONAL/`,
  committed. `digest.md` carries the visible provisional banner described
  above. First run (no real `prior_grid.csv` exists) — diff/digest
  correctly render everything as new.
- Result sanity: 7 real capsules → 20 invalid grid cells (their exact
  hour-spans) + 301 valid cells inside the placeholder operating window;
  18,111 NOT-OPERATING cells outside it (LookbackMonths=8, used verbatim,
  same as the synthetic run — most of an 8-month window predates a
  4.4-day placeholder operating window). `adjudicated_condition.csv` is
  correctly empty (no Events to fold). Every invalid cell's
  `ContributingEventIDs` is capsule-provenance-only (`CAP:...`) — no
  tickets exist yet to tell an adjudication story.
- `test_real_smoke_test_fixtures_still_run_without_error` (test_run.py) is
  a lightweight regression check that this input set stays loadable — not
  a re-validation of the output content, which was hand-reviewed once.

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
- **RESOLVED (Ryan, 2026-07-02) — the stable-detection case of the gap
  above:** `grid.py` now gives every capsule a deterministic synthetic
  provenance id (`capsule_provenance_id`, prefixed `CAP:`) so
  `ContributingEventIDs` cites unticketed raw-capsule contributions too.
  A ✗→✓ flip whose ONLY contributors are unticketed capsules that are
  identity-unchanged (same analyzer+class+start+end) between the prior
  pull and tonight's capsule set resolves SILENT — the raw detection
  didn't change, nothing to approve. A capsule that's new, moved (any
  interval change → a different id), or gone still has no Observation to
  check and falls to `INTEGRITY ALERT` exactly as before — this closes
  only the "stable, still-unticketed" case, not the general gap of
  unticketed detections having no ledger trail at all.
- `DiffResult` gained `new_invalid_cells` (every newly-invalid cell, a
  superset of `late_arrivals`) so `digest.py`'s "new downtime since
  yesterday" section doesn't re-derive prior/current diffing itself.
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

## Digest module (2026-07-02)

- Pure renderer, no I/O — `render_digest(...) -> str`; run.py (not yet
  built) writes the result to `out/<run_date>/digest.md`.
- Sections in spec order: heartbeat → integrity alerts → new downtime
  since yesterday → late-arriving ✗ → pending dismissals (+ age, + any
  cancel-approval flag) → withdrawals & conflicts → summary counts.
- First run collapses the three diff-derived sections (alerts/new-downtime/
  late-arrivals) to a single explanatory note each, per spec ("digest says
  so") — no per-cell itemization on day one.
- Re-folds `events` itself to find Dismissal-pending observations —
  `DiffResult` doesn't carry the full Observation list, only flip/alert
  data, so this is the natural seam rather than widening diff.py's output.
- Withdrawals = tonight's `DeltaResult.new_events` filtered to
  `EventType.Withdrawn` (not all historically-Withdrawn observations,
  which would repeat every night). Conflicts = `DeltaResult.flags` entries
  prefixed `CONFLICT`. Cancel-approval flags matched to a pending
  dismissal by substring (`CANCEL-APPROVAL` + the origin id appearing in
  the flag text) — no new schema field for the association.
- **Flagged, not blocking, two choices without a spec-literal answer:**
  (1) pending-dismissal age uses UTC calendar days from the
  `DismissalProposed` event's `ActedAt`, not site-local like the
  late-arrival check — age here is coarse staleness, not an hour-precision
  compliance boundary; (2) first-run "new downtime" is a single count, not
  itemized (itemizing everything on day one is noise).

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
- **HARD STOP (new, surfaced by run.py's first full end-to-end pass,
  2026-07-02):** a legitimately CONFIRMED downtime episode on an uncovered
  analyzer renders as `NOT-ASSESSED`, not invalid. Concretely: CEMS-001
  (SeeqCovered=false) fixture rows E001 (SeeqDetection) → E002
  (Confirmation), Jan 15 14:00–18:00Z — a human directly confirmed this
  downtime — shows `NOT-ASSESSED` in `grid.csv` for all four hours,
  because Confirmation doesn't route through branch 4 (only TechEntry-
  origin windows feed `manual_qa_windows`; a Confirmed SeeqDetection
  observation feeds `detected_invalid_windows`, which never triggers, only
  subtracts) and branch 5 is coverage-gated. This is a faithful, consistent
  execution of the SeeqCovered ruling and the manual/detected split as
  written — not a code defect — but it is a real, material consequence
  that wasn't examined when those rulings were made in isolation. This is
  exactly a "fail-safe polarity vs. rule reading" conflict per §5: a
  human-confirmed fact arguably should never be able to render as
  "couldn't assess," regardless of coverage. NOT fixed here — no rule-
  engine or grid.py code was changed to address it; surfacing for Ryan's
  ruling alongside the existing NOT-ASSESSED rollup hard stop above (the
  two are likely resolved together).

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
