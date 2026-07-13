# 50 — Clerk v2 Golden Traps — Answer Key

**Purpose.** An **independent** evaluation set: ~10 adversarial scenarios, each with the expected output **hand-derived from the verbatim reg (Doc 14)**, not from the clerk's own tests. The clerk's tests assert what the code assumed correct; this asserts what the reg says. Where they disagree, **the clerk is presumed wrong.** Encode each as a synthetic fixture and diff the clerk's output against the "Expected" line.

**Authority of each trap.** Not every expected value comes from the literal validity paragraphs — some come from Appendix F plus site doctrine. The "clerk is presumed wrong on a mismatch" rule only carries full force where the reg text actually speaks.
- **Verbatim §60.13(h)(2):** T1 (iii)(A separation), T2 (paragraph by reason), T3 (operating-hour quadrants), T7 (paragraph iv / backdate), T9 (whole-hour counting).
- **Engineering / App-F / site doctrine (reg is silent on the mechanism):** T4 & T10 (diluent propagation — you can't correct a concentration without a valid diluent), T5 (monitor-vs-source rollup — App F availability + the per-obligation rule).
- **Approved-counts (workflow doctrine):** T6.

**Escape valve.** On any clerk-vs-key mismatch, the default is "the clerk is wrong" — but **re-derive the disputed value from Doc 14 verbatim (for reg-authority traps) or DOCTRINE.md (for engineering-authority traps) first, then decide.** Hand-derived keys can contain a slip (v1 had exactly one). Don't enshrine a key error by assuming the code always lost.

**Conventions.** All times site-local = UTC (synthetic; timezone alignment is a separate concern; DST is exercised in the fuzz/adversarial sets, Docs 51/52, not here). Dense sampling assumed (a data point exists arbitrarily close to any valid instant). Quadrants: Q1 = :00–:15, Q2 = :15–:30, Q3 = :30–:45, Q4 = :45–:00. "Down hour" = an hour the clerk marks invalid/down. "Dropped" = out of BOTH availability numerator and denominator, still recorded with reason.

**Synthetic plant (config for the traps).**
- **Source B15** (one CEMID): `NOx` (diluent-corrected → O2), `O2` (diluent, basis O2), `CO` (diluent-corrected → O2), `TEMP-degF` (not-diluent-corrected).
- **Source FCC** (one CEMID): `NOx-P` permanent, `NOx-T` temp, `O2-F` diluent.
- **Source SRU** (one CEMID): `SO2` (diluent-corrected → CO2), `CO2` (diluent, basis CO2). — exercises the CO2 path.

---

## T1 — (iii)(A) is 15-minute separation (kills the ≥30-min bug)
Hour 09:00–10:00, reason `QA-01` → paragraph (iii), unit operates all four quadrants. Four sub-cases, NOx analyzer, valid time as stated, remainder invalid:
- **T1a** valid only 09:05–09:18 (13-min span) → **DOWN** (13 < 15).
- **T1b** valid only 09:05–09:22 (17-min span) → **NOT DOWN** (17 ≥ 15).
- **T1c** valid only 09:05–09:19 (14-min span) → **DOWN** (14 < 15).
- **T1d** valid only 09:05–09:20 (exactly 15-min span) → **NOT DOWN** (15 ≥ 15).

**Expected down-hours:** T1a {09:00}, T1b {}, T1c {09:00}, T1d {}.
**Discriminator:** under the ≥30-min bug, T1b and T1d wrongly become DOWN. Correct build: they are NOT down.

## T2 — Paragraph selection by reason (same window, two reasons)
NOx invalid **08:22–10:40** (unchanged across both cases). Unit operating all hours.
- **T2a** reason `QA-01` → (iii): hr8 keeps valid 08:00–08:22 (22 min ≥15) → valid; hr9 all invalid → down; hr10 keeps valid 10:40–11:00 (20 min) → valid. **Expected down = {09:00}** (1 hour).
- **T2b** reason `MM-01` → (i): hr8 Q3+Q4 fully invalid → down; hr9 down; hr10 Q1+Q2 fully invalid → down. **Expected down = {08:00, 09:00, 10:00}** (3 hours).

**Discriminator:** a build that ignores reason and applies one fixed test gives the same count for both. Correct: 1 vs 3.

## T3 — Unit-offline gate
NOx signal invalid **09:00–11:00**, reason `MM-01` (i).
- **T3a** unit fully offline 09:00–10:00, online 10:00–11:00 → **hr9 DROPPED** (out of num+denom, recorded "unit offline"); **hr10 DOWN**.
- **T3b** unit offline only 10:00–10:30 (Q1,Q2 of hr10), online rest; NOx invalid all hr10 → mask Q1,Q2, evaluate Q3,Q4 (both invalid under (i)) → **hr10 DOWN** on the operating remainder.

**Expected:** T3a → down {10:00}, dropped {09:00}; T3b → down {10:00}, dropped {} (partial-offline hour is evaluated, not dropped).
**Discriminator:** a build that counts the offline hour as down inflates the numerator; one that drops the *partial*-offline hour loses a real down hour.

## T4 — Diluent propagation (O2), one-way
Source B15. O2 invalid **09:00–11:00** (reason `MM-01`). NOx and CO have **valid own signal all day**. Separately, NOx has its **own** outage **13:00–14:00**. TEMP-degF is not-diluent-corrected.
**Expected down-hours:**
- O2: {09:00, 10:00}
- NOx: {09:00, 10:00, 13:00}  (09–10 propagated from O2 despite valid own signal; 13 is its own)
- CO: {09:00, 10:00}  (propagated)
- TEMP-degF: {}  (not diluent-corrected — unaffected)
- **One-way check:** O2 is **NOT** down at 13:00 (NOx's own outage does not propagate back to O2).

**T4d — manual diluent outage propagates too.** Same source, but the O2 09:00–11:00 outage comes from a **manual log entry, no capsule** (D7 is source-agnostic). Expected identical: NOx {09,10,+own}, CO {09,10}. A build that propagates only *detected* diluent windows misses this and under-marks — a real live gap, not just a trap.

**Discriminator:** if propagation is missing, NOx/CO show only {13:00}/{} and the pollutant downtime is under-marked exactly where Seeq only reported O2. If propagation is two-way, O2 wrongly gains {13:00}. If propagation is detection-only, T4d is missed.

## T5 — Redundant temp coverage + coverage-window gating
Source FCC carries a **NOx obligation** {`NOx-P` permanent, `NOx-T` temp} and a **separate O2 obligation** {`O2-F`}. The source rollup below is for the **NOx obligation only** — `O2-F` has its own DAR and its own availability and **does not participate in the NOx intersection** (Doctrine D8: intersect within an obligation, not within a unit). O2-F is valid all period. Outages: **NOx-P down 10:00–14:00**, **NOx-T down 12:00–16:00**.

Monitor-level (reported separately, always): NOx-P down {10,11,12,13}; NOx-T down {12,13,14,15}.

**Per-obligation discriminator (the bug the first encode found):** a build that intersects **all** monitors on the unit — including the valid O2-F — computes NOx source-down = **{}** for every case (O2-F is always valid, so "every monitor down at once" never happens). That is wrong. The correct intersection is over the NOx obligation's monitors only. A valid monitor for one pollutant must never cover another pollutant's outage.

- **T5a** NOx-T in coverage the whole window (InService before 10:00): at each hour, source down iff every in-coverage monitor invalid.
  - 10,11: NOx-P down, NOx-T **valid** → covered.
  - 12,13: both down → down.
  - 14,15: NOx-P valid → covered.
  - **Expected source-down = {12:00, 13:00}.** (Matches Doc 03 FCC example.)
- **T5b** NOx-T `InServiceDate` = 12:00 (coverage gating):
  - 10,11: only NOx-P in coverage, it's down → **down**.
  - 12,13: both in coverage, both down → **down**.
  - 14,15: NOx-P valid → covered.
  - **Expected source-down = {10:00, 11:00, 12:00, 13:00}.**
- **T5c** identical to T5b but NOx-T's down window comes from a **manual (List A) entry, no capsule** → same result: **source-down = {10:00, 11:00, 12:00, 13:00}** (the temp participates without a Seeq capsule).

**Discriminator:** a build without coverage gating computes {12,13} for T5b (treating the not-yet-deployed temp as valid) — the temp-CEMS bug. Correct T5b/T5c = {10,11,12,13}.

## T6 — Approved-counts-only
NOx, one period, three extents: **E1 Approved (10:00–12:00, 2 hrs)**, **E2 Pending (13:00–16:00, 3 hrs)**, **E3 Rejected (20:00–21:00, 1 hr)**.
**Expected counted downtime = 2 hours** (E1 only). Availability reflects E1 only; E2 and E3 contribute **zero**.
**Discriminator:** a status-blind sum returns 6 hours. Correct: 2.

## T7 — Backdate to last passing validation event
Daily validation **passes Day1 06:00**. Data reads valid every hour through Day2 06:00. Daily validation **fails Day2 06:00**.
**Expected:** invalidate back to the **Day1 06:00 passing-validation event** → all hours **Day1 06:00 → Day2 06:00** invalid. Backdate anchor = **Day1 06:00**.
**Discriminator:** a build anchoring on the "last valid data cell" anchors near **Day2 05:00** and invalidates almost nothing — the W6 bug. Correct anchor = Day1 06:00.

## T8 — Start-only entry
A `filter change` logged at **09:15, start only, no end**, reason maintenance. No analyzer signal outage that hour.
**Expected:** the entry is recorded and produces **zero down-hours** by itself (no end → no window → no invalid hour). It must not crash the fold and must not be treated as an open-ended outage.
**Discriminator:** a build that treats a start-only entry as open-ended (infinite) downtime marks every subsequent hour down; a build that assumes every entry has an end **crashes** on it (this is one of the two bugs the first encode caught). Correct: zero down-hours, no crash. *(This trap asserts fold behavior only — it does not assume any A/B concurrence feature exists.)*

## T9 — Whole-hour counting, not minutes
NOx invalid **09:05–09:10** and **09:35–09:40** (two 5-min gaps, 10 invalid minutes total), reason `QA-01` → (iii). Valid the rest of the hour (e.g. valid points at 09:12 and 09:50, 38 min apart).
**Expected:** hour **NOT DOWN** (two valid points ≥15 min apart exist). Down-hours {} despite 10 invalid minutes.
**Discriminator:** a build summing invalid minutes or failing on any gap marks the hour down. Correct: not down — the reg counts whole hours, and (iii)(A) is satisfied.

## T10 — CO2 diluent path
Source SRU. CO2 invalid **09:00–10:00** (reason `MM-01`). SO2 has valid own signal, corrected to CO2.
**Expected:** SO2 down {09:00} (propagated from CO2); CO2 down {09:00}. Propagation uses the **CO2** basis, not O2.
**Discriminator:** a build hardwired to O2 misses the CO2 dependency and leaves SO2 not-down.

---

## Global invariants to assert across the whole set
- **No `not_assessed` hours** are produced anywhere in the set (every hour is assessed). If any appear, it's a failure to investigate (Fix 4).
- **Monitor-level downtime is always reported**, even when the source rolls up to covered (T5a: NOx-P and NOx-T each show their own down-hours while the source shows only {12,13}).
- **Report-vs-emissions divergence is expected**, not a bug: in T5a the source reads "covered" 10:00–11:00 while NOx-P's own emissions data is invalid those hours.
- **Source rollup is per-obligation.** A valid monitor for one pollutant never reduces another pollutant's source downtime. The intersection groups by obligation (pollutant), never by unit (D8).
- **Diluent propagation is source-agnostic.** A diluent outage propagates to its dependents whether it was logged manually or detected (D7).

## How to use
1. Encode each scenario as a synthetic fixture in the clerk's fixture format.
2. Run the clerk; capture per-analyzer down-hours, dropped hours, source-down hours, counted downtime, and backdate anchors.
3. Diff against the **Expected** lines above.
4. Any mismatch: the clerk is presumed wrong (these are hand-derived from eCFR). Fix code, not the expected value.
