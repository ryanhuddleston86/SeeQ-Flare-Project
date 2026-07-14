# Fix: passing daily validations no longer count as monitor downtime

## Symptom (real run)
On a 3-week run with daily validations, every passing validation window
(≈06:40–07:00) was being counted as monitor downtime — piling roughly one
phantom down-hour per analyzer per day into the QA downtime bucket and pushing
several analyzers over the 5% DAR threshold with no real outage.

## Root cause
A passing daily validation is a ~20-minute cal-gas check inside a **full**
operating hour (the unit runs the whole hour). Seeq flags the analyzer offline
during the cal gas (a `status-offline` capsule). Pre-fix, the validation
(`mqaqc`) windows only set an internal `mqaqc` flag — they never entered the
hour's QA/maintenance windows. So a full-hour validation fell through to
**§60.13(h)(2)(i)**, which requires a valid data point in **all four** 15-minute
quadrants, and the cal-gas span killed the overlapping quadrant → the hour
scored **invalid** → phantom downtime.

A **manual** QA window in the same spot already routed to (iii)(A) and produced
zero downtime; validations simply weren't given the same treatment.

## Fix
A validation/cal window **is** a QA activity per **40 CFR 60.13(h)(2)(iii)**, so
it now joins the hour's `manual_qa_windows` (`clerk/grid.py`). That:
1. routes a full operating hour to **(iii)(A)** — two valid data points ≥15 min
   apart — instead of (i); and
2. subtracts the ~20-min cal-gas span from valid time (no valid stack data while
   on cal gas).

With valid data before and after the check, a routine **passing** validation now
contributes **zero** downtime. A **failed** validation is unaffected: it enters
through its own Out-Of-Control window (Appendix F §4.3.1), scored by the OOC
rule, not this branch.

## Before / after
21 days, per analyzer, one passing daily validation, cal gas also surfaced as a
`status-offline` detection (the real-run shape):

| | down hours | downtime % |
|---|---|---|
| before | 21 (1/day) | 4.167% |
| after  | 0          | 0.0%   |

Genuine outages still score down — the fix only reclassifies the QA hour, it
does not mask real downtime.

## How to verify in this bundle
- `python demo_passing_validation.py` — self-contained proof (prints DAR = 0%
  downtime and asserts zero phantom down-hours).
- `python -m pytest tests/test_appendixf.py -k passing_daily_validations -q`
  — the golden regression trap.
- `python -m pytest -q` — full v3 suite (354 passing).

### Reproduce through the production SharePoint runner
```
python run_sharepoint.py \
    samples_sharepoint/Events_seed_SRU_Boiler15.csv \
    samples_sharepoint/roster_sru_boiler15.csv \
    out_passing \
    samples_sharepoint/list_b_passing.csv \
    samples_sharepoint/validations_passing.csv
```
`list_b_passing.csv` supplies the coincident `status-offline` cal-gas capsules
and `validations_passing.csv` the passing daily validations. With the fix, the
06:00 validation hours resolve `valid [(iii)(A)]`; any genuine seed outage in the
same window stays `invalid [(i)]`.
