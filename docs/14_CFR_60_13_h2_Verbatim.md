# 14 — §60.13(h)(2) Verbatim Text (source-verified)

**Fetched:** 2026-07-01, directly from eCFR (not from training memory — per ledger 18)
**Source:** https://www.ecfr.gov/current/title-40/chapter-I/subchapter-C/part-60/subpart-A/section-60.13
**Currency:** eCFR displays Title 40 as up to date as of 6/23/2026. This specific section's own edit timeline shows no changes since 1/03/2017 — the text below is settled, not moving under the build.
**Use:** paste the relevant blockquote directly into each branch's `[VERBATIM]` docstring in `rules.py`. Text is a U.S. government work — not copyrighted, safe to reproduce exactly, and exact wording is required here (paraphrase changes what the rule permits).

---

## Chapeau — §60.13(h)(2)

> For continuous monitoring systems other than opacity, 1-hour averages shall be computed as follows, except that the provisions pertaining to the validation of partial operating hours are only applicable for affected facilities that are required by the applicable subpart to include partial hours in the emission calculations:

**Confirms:** the spec's `PartialOperatingHourApplicability:<obligation>` Config flag is correctly derived — this sentence is exactly why it must be per-obligation, not global.

---

## Branch (i) — full operating hour, normal

> Except as provided under paragraph (h)(2)(iii) of this section, for a full operating hour (any clock hour with 60 minutes of unit operation), at least four valid data points are required to calculate the hourly average, *i.e.*, one data point in each of the 15-minute quadrants of the hour.

## Branch (ii) — partial operating hour, normal

> Except as provided under paragraph (h)(2)(iii) of this section, for a partial operating hour (any clock hour with less than 60 minutes of unit operation), at least one valid data point in each 15-minute quadrant of the hour in which the unit operates is required to calculate the hourly average.

## Branch (iii) — maintenance/QA hour

> For any operating hour in which required maintenance or quality-assurance activities are performed:
>
> (A) If the unit operates in two or more quadrants of the hour, a minimum of two valid data points, **separated by at least 15 minutes**, is required to calculate the hourly average; or
>
> (B) If the unit operates in only one quadrant of the hour, at least one valid data point is required to calculate the hourly average.

**(iii)(B) — confirmed verbatim match** to the spec's paraphrase ("one valid data point suffices").

**(iii)(A) — the correction, confirmed real.** The rule is a **temporal-separation test on data points**, not a **quadrant-membership test**. "Two points in two different quadrants" is not sufficient — two points can be in different quadrants and only seconds apart (e.g., 0:14:59 and 0:15:01: different quadrants, ~2 minutes apart, fails the actual 15-minute requirement). The calc manual's "2 of 4 — one point in each of two quadrants" phrasing would incorrectly validate that hour. This is the false-valid edge Rev 2's traceability review flagged — now source-confirmed, not inferred.

**Implementation note on Rev 2's own restatement.** Rev 2 phrases (iii)(A) as `max(V) − min(V) ≥ 15 min`, where V is the remaining valid time after subtracting all invalid windows. This is a reasonable and — under the assumption that valid time is effectively continuous (dense sampling relative to the 15-minute test, so a data point exists arbitrarily close to any instant of validity) — **operationally equivalent** restatement of the literal test: if two data points are ≥15 min apart, both lie in V, so max(V)−min(V) ≥ 15; conversely if max(V)−min(V) ≥ 15, points near those extremes are ≥15 min apart. **Recommend:** encode the literal two-point-separation test as the canonical docstring statement (it's what's actually written), and treat the interval-algebra `max(V)−min(V)` form as a documented, deliberate implementation choice — not a silent substitution. Worth a unit test at the boundary where the continuity assumption could break (a valid window narrower than the sampling interval).

## Branch (iv) — failed daily calibration check

> If a daily calibration error check is failed during any operating hour, all data for that hour shall be invalidated, unless a subsequent calibration error test is passed in the same hour and the requirements of paragraph (h)(2)(iii) of this section are met, based solely on valid data recorded after the successful calibration.

**Confirmed verbatim match** to the spec's paraphrase.

---

## Adjacent provisions — flagged, not yet in scope (confirm before ignoring)

**(v) — all valid data used:**
> For each full or partial operating hour, all valid data points shall be used to calculate the hourly average.

**(vi)/(vii) — breakdown/repair data exclusion, with a reporting-mode exception:**
> (vi) Except as provided under paragraph (h)(2)(vii) of this section, data recorded during periods of continuous monitoring system breakdown, repair, calibration checks, and zero and span adjustments shall not be included in the data averages computed under this paragraph.
>
> (vii) Owners and operators complying with the requirements of § 60.7(f)(1) or (2) must include any data recorded during periods of monitor breakdown or malfunction in the data averages.

This governs which raw data points feed the **numeric pollutant average value** once an hour's validity is already decided — a different mechanism from the hour-validity grid the clerk builds. Likely EMP/PI-averaging-engine territory, not `rules.py`. **Confirm scope rather than assume** — if the clerk's grid ever needs to also flag *which* data points within a valid hour to exclude from the average, this is the citation.

**(viii) — subpart-specific partial-hour exclusion:**
> When specified in an applicable subpart, hourly averages for certain partial operating hours shall not be computed or included in the emission averages (*e.g.*, hours with < 30 minutes of unit operation under § 60.47b(d)).

Reinforces the per-obligation Config pattern — another subpart-conditional carve-out alongside the chapeau's partial-hour applicability flag. Candidate for the same Config mechanism, not a new one.

---

## Summary for rules.py

| Branch | Verbatim match to current spec paraphrase | Action |
|---|---|---|
| Chapeau | Confirmed | Paste as-is |
| (i) | Confirmed | Paste as-is |
| (ii) | Confirmed | Paste as-is |
| (iii)(A) | **Correction confirmed real** | Paste literal text; canonicalize the two-point test; note the interval-algebra equivalence as a deliberate choice |
| (iii)(B) | Confirmed | Paste as-is |
| (iv) | Confirmed | Paste as-is |
| (vi)/(vii)/(viii) | N/A — not in current branch set | Flag to Ryan; likely out of clerk scope |
