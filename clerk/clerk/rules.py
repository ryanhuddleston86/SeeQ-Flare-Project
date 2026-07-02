"""
Step 5 — the rule engine (pure, no I/O). SKELETON ONLY.

STATUS 2026-07-02: 40 CFR 60.13(h)(2) verbatim text could NOT be fetched —
this environment's network policy denies www.ecfr.gov (proxy CONNECT 403).
Per the verbatim-text-before-code rule (spec §0.5, ledger 18), every branch
verdict below is a stub that raises NotImplementedError. Filling a [VERBATIM]
block and implementing its branch requires: paste the eCFR text, re-derive
the vectors from it, PAUSE for Ryan's review, then code.

Branch PRECEDENCE below is PROVISIONAL — carried from the spec's paraphrase
and itself marked [VERIFY vs. the (h)(2) chapeau]. It only affects hours
where multiple branches trigger; the live paths in this skeleton (branch 1
and the NOT-ASSESSED fallthrough) are unaffected.

SeeqCovered gate (Ryan, 2026-07-02): branch 5 — the normal-hour quadrant
test, (i)/(ii) — may ONLY fire when SeeqCovered=true for the analyzer.
When SeeqCovered=false and no branch 2/3/4 claims the hour, the cell is
NOT-ASSESSED: detection-based assessment is impossible there, and fabricating
a verdict would violate fail-safe polarity in the flattering direction.
Branches 2–4 never depended on Seeq detection and ignore coverage.

HARD STOP (§5, flagged, awaiting Ryan): how NOT-ASSESSED rolls into
availability% or the DAR denominator is deliberately UNDECIDED here. The
engine only emits the state; no downstream aggregation may interpret it
without an explicit ruling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Set, Tuple

from clerk.schemas import CellValid

Interval = Tuple[datetime, datetime]

_VERBATIM_BLOCKED = (
    "40 CFR 60.13(h)(2) verbatim text not yet pasted — eCFR fetch is blocked "
    "by this environment's network policy (www.ecfr.gov CONNECT 403). "
    "Branch verdicts are frozen until the text is pasted and Ryan reviews "
    "the re-derived vectors."
)


@dataclass
class HourContext:
    """Everything the engine may consult for one analyzer × UTC hour."""
    analyzer: str
    hour_start: datetime
    seeq_covered: bool
    # unit operating intervals clipped to the hour
    operating: List[Interval] = field(default_factory=list)
    # tech/QA/manual-sourced invalid windows clipped to the hour (branch 4 triggers)
    manual_qa_windows: List[Interval] = field(default_factory=list)
    # failed daily calibration instant within the hour, if any (branch 2 triggers)
    failed_cal_at: Optional[datetime] = None
    # passing calibration instant within the hour, if any (branch 2 verdict input)
    passing_cal_at: Optional[datetime] = None


def quadrants_operated(ctx: HourContext) -> Set[int]:
    """Which 15-min quadrants (0–3) of the hour contain any operating time.
    Pure interval arithmetic — not a regulatory interpretation."""
    out: Set[int] = set()
    for q in range(4):
        q_start = ctx.hour_start + timedelta(minutes=15 * q)
        q_end = q_start + timedelta(minutes=15)
        for s, e in ctx.operating:
            if s < q_end and q_start < e:
                out.add(q)
                break
    return out


# ---------------------------------------------------------------------------
# Branch verdicts — every one is a hard stop until its [VERBATIM] is filled
# ---------------------------------------------------------------------------

def _branch_iv(ctx: HourContext) -> Tuple[CellValid, str]:
    """[VERBATIM: paste 40 CFR 60.13(h)(2)(iv) from eCFR here — FETCH BLOCKED,
    see module docstring. Build blocks until filled.]"""
    raise NotImplementedError(f"branch (iv): {_VERBATIM_BLOCKED}")


def _branch_iii_b(ctx: HourContext) -> Tuple[CellValid, str]:
    """[VERBATIM: paste 40 CFR 60.13(h)(2)(iii)(B) from eCFR here — FETCH
    BLOCKED, see module docstring. Build blocks until filled.]"""
    raise NotImplementedError(f"branch (iii)(B): {_VERBATIM_BLOCKED}")


def _branch_iii_a(ctx: HourContext) -> Tuple[CellValid, str]:
    """[VERBATIM: paste 40 CFR 60.13(h)(2)(iii)(A) from eCFR here — FETCH
    BLOCKED, see module docstring. Build blocks until filled.]"""
    raise NotImplementedError(f"branch (iii)(A): {_VERBATIM_BLOCKED}")


def _branch_normal(ctx: HourContext) -> Tuple[CellValid, str]:
    """[VERBATIM: paste 40 CFR 60.13(h)(2)(i)/(ii) from eCFR here — FETCH
    BLOCKED, see module docstring. Build blocks until filled.]

    Coverage gate: this branch is only reachable when ctx.seeq_covered is
    true — evaluate_hour enforces it."""
    raise NotImplementedError(f"branch (i)/(ii): {_VERBATIM_BLOCKED}")


# ---------------------------------------------------------------------------
# Branch selection — PROVISIONAL precedence, [VERIFY vs. the (h)(2) chapeau]
# ---------------------------------------------------------------------------

def evaluate_hour(ctx: HourContext) -> Tuple[CellValid, str]:
    """Route one analyzer × hour to a branch. Trigger detection is interval
    arithmetic; verdicts are regulatory and stubbed pending verbatim review."""
    # 1. Unit not operating at all in the hour — operational bookkeeping,
    #    not an (h)(2) reading: excluded from numerator and denominator both.
    if not ctx.operating:
        return CellValid.not_operating, "not-operating"

    # 2. Failed daily cal in the hour → branch (iv).
    if ctx.failed_cal_at is not None:
        return _branch_iv(ctx)

    # 3. Operates in exactly one quadrant → branch (iii)(B).
    if len(quadrants_operated(ctx)) == 1:
        return _branch_iii_b(ctx)

    # 4. Any tech/QA invalid window intersects the hour → branch (iii)(A).
    #    Coverage-independent: manual evidence never needed Seeq detection.
    if ctx.manual_qa_windows:
        return _branch_iii_a(ctx)

    # 5. Normal hour — gated on Seeq coverage.
    if ctx.seeq_covered:
        return _branch_normal(ctx)
    return CellValid.not_assessed, "not-assessed:no-seeq-coverage"
