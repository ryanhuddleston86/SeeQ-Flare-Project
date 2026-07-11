"""
Step 5 — the rule engine (pure, no I/O).

VERBATIM SOURCE: docs/14_CFR_60_13_h2_Verbatim.md — fetched directly from
eCFR 2026-07-01 (Title 40 current as of 6/23/2026; section unchanged since
1/03/2017), checked against the spec branch-by-branch. Every branch docstring
below quotes that file. Branch verdicts UNFROZEN 2026-07-02; the precedence
chain is no longer provisional for branches (i)–(iv) (Ryan, same date).

Chapeau — §60.13(h)(2):

    "For continuous monitoring systems other than opacity, 1-hour averages
    shall be computed as follows, except that the provisions pertaining to
    the validation of partial operating hours are only applicable for
    affected facilities that are required by the applicable subpart to
    include partial hours in the emission calculations:"

The chapeau's partial-hour applicability gate is per-obligation config
(`PartialOperatingHourApplicability:<obligation>`) applied by grid.py — the
engine computes the (ii) verdict; the grid decides whether it applies.

Adjacent provisions (v)–(viii) are informational only, out of current scope
(see docs/14 — (vi)/(vii) govern which points feed the numeric average once
validity is decided, likely EMP/PI territory; (viii) is another subpart-
conditional config candidate). Do not implement here.

SeeqCovered gate (Ryan, 2026-07-02): branch 5 — the normal-hour quadrant
test, (i)/(ii) — may ONLY fire when SeeqCovered=true for the analyzer.
When SeeqCovered=false and no branch 2/3/4 claims the hour, the cell is
NOT-ASSESSED. Branches 2–4 ignore coverage.

HARD STOP (§5, still open): how NOT-ASSESSED rolls into availability% or the
DAR denominator is undecided; no downstream aggregation may interpret it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Set, Tuple

from clerk.schemas import CellValid

Interval = Tuple[datetime, datetime]

_FIFTEEN_MIN = timedelta(minutes=15)


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
    # detection-sourced invalid windows clipped to the hour (never a trigger —
    # they subtract from valid time in whichever branch fires)
    detected_invalid_windows: List[Interval] = field(default_factory=list)
    # failed daily calibration instant within the hour, if any (branch 2 triggers)
    failed_cal_at: Optional[datetime] = None
    # passing calibration instant within the hour, if any (branch 2 verdict input)
    passing_cal_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Interval arithmetic (pure math, no regulatory content)
# ---------------------------------------------------------------------------

def merge_intervals(intervals: List[Interval]) -> List[Interval]:
    out: List[Interval] = []
    for s, e in sorted(i for i in intervals if i[0] < i[1]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract_intervals(base: List[Interval], minus: List[Interval]) -> List[Interval]:
    out: List[Interval] = []
    minus = merge_intervals(minus)
    for s, e in merge_intervals(base):
        cur = s
        for ms, me in minus:
            if me <= cur or ms >= e:
                continue
            if ms > cur:
                out.append((cur, ms))
            cur = max(cur, me)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return out


def interval_span(intervals: List[Interval]) -> timedelta:
    if not intervals:
        return timedelta(0)
    return max(e for _, e in intervals) - min(s for s, _ in intervals)


def intervals_overlap(intervals: List[Interval], lo: datetime, hi: datetime) -> bool:
    return any(s < hi and lo < e for s, e in intervals)


def quadrants_operated(ctx: HourContext) -> Set[int]:
    """Which 15-min quadrants (0–3) of the hour contain any operating time."""
    out: Set[int] = set()
    for q in range(4):
        q_start = ctx.hour_start + timedelta(minutes=15 * q)
        if intervals_overlap(ctx.operating, q_start, q_start + _FIFTEEN_MIN):
            out.add(q)
    return out


def _valid_time(ctx: HourContext, after: Optional[datetime] = None) -> List[Interval]:
    """V: operating time in the hour minus the union of all invalid windows
    from all sources — the separation test composes across multiple windows.
    `after` clips V to data recorded after that instant (branch (iv) exception)."""
    base = ctx.operating
    if after is not None:
        base = [(max(s, after), e) for s, e in base if e > after]
    return subtract_intervals(base, ctx.manual_qa_windows + ctx.detected_invalid_windows)


# ---------------------------------------------------------------------------
# Branch verdicts — each docstring quotes docs/14_CFR_60_13_h2_Verbatim.md
# ---------------------------------------------------------------------------

def _iii_requirements(valid: List[Interval], operated_quadrants: int) -> bool:
    """The (h)(2)(iii) test, parameterized so branch (iv)'s exception can
    apply it 'based solely on valid data recorded after the successful
    calibration'. (A) when the unit operates in two or more quadrants,
    (B) when it operates in only one."""
    if operated_quadrants == 1:
        return bool(valid)
    # Interval-algebra form of (iii)(A)'s literal two-point separation test:
    # span(V) >= 15 min. Equivalent under the dense-sampling assumption (a
    # data point exists arbitrarily close to any instant of validity) — a
    # deliberate implementation choice, not a silent substitution; see the
    # equivalence note in docs/14 and the narrow-sliver boundary test.
    return interval_span(valid) >= _FIFTEEN_MIN


def _branch_iv(ctx: HourContext) -> Tuple[CellValid, str]:
    """[VERBATIM 40 CFR 60.13(h)(2)(iv), from docs/14]:

    "If a daily calibration error check is failed during any operating hour,
    all data for that hour shall be invalidated, unless a subsequent
    calibration error test is passed in the same hour and the requirements
    of paragraph (h)(2)(iii) of this section are met, based solely on valid
    data recorded after the successful calibration."
    """
    if ctx.passing_cal_at is not None and ctx.passing_cal_at > ctx.failed_cal_at:
        post_cal_valid = _valid_time(ctx, after=ctx.passing_cal_at)
        if _iii_requirements(post_cal_valid, len(quadrants_operated(ctx))):
            return CellValid.valid, "(iv)"
    return CellValid.invalid, "(iv)"


def _branch_iii_b(ctx: HourContext) -> Tuple[CellValid, str]:
    """[VERBATIM 40 CFR 60.13(h)(2)(iii)(B), from docs/14]:

    "(B) If the unit operates in only one quadrant of the hour, at least one
    valid data point is required to calculate the hourly average."
    """
    ok = bool(_valid_time(ctx))
    return (CellValid.valid if ok else CellValid.invalid), "(iii)(B)"


def _branch_iii_a(ctx: HourContext) -> Tuple[CellValid, str]:
    """[VERBATIM 40 CFR 60.13(h)(2)(iii) chapeau + (A), from docs/14]:

    "For any operating hour in which required maintenance or quality-
    assurance activities are performed:

    (A) If the unit operates in two or more quadrants of the hour, a minimum
    of two valid data points, separated by at least 15 minutes, is required
    to calculate the hourly average; or"

    CANONICAL statement: the literal TWO-POINT TEMPORAL-SEPARATION test —
    not a quadrant-membership test (two points seconds apart across a
    quadrant boundary do NOT satisfy it), and not Rev 2's max(V)−min(V)
    paraphrase, which is the implementation form only (see _iii_requirements).
    """
    ok = _iii_requirements(_valid_time(ctx), operated_quadrants=2)
    return (CellValid.valid if ok else CellValid.invalid), "(iii)(A)"


def _branch_normal(ctx: HourContext) -> Tuple[CellValid, str]:
    """[VERBATIM 40 CFR 60.13(h)(2)(i) and (ii), from docs/14]:

    "(i) Except as provided under paragraph (h)(2)(iii) of this section, for
    a full operating hour (any clock hour with 60 minutes of unit
    operation), at least four valid data points are required to calculate
    the hourly average, i.e., one data point in each of the 15-minute
    quadrants of the hour."

    "(ii) Except as provided under paragraph (h)(2)(iii) of this section,
    for a partial operating hour (any clock hour with less than 60 minutes
    of unit operation), at least one valid data point in each 15-minute
    quadrant of the hour in which the unit operates is required to
    calculate the hourly average."

    Coverage gate: only reachable when ctx.seeq_covered is true — the
    chapeau's partial-hour applicability is applied per obligation by grid.py.
    """
    valid = _valid_time(ctx)
    quads = quadrants_operated(ctx)
    # "full operating hour (any clock hour with 60 minutes of unit operation)"
    operated = sum((e - s for s, e in merge_intervals(ctx.operating)), timedelta(0))
    full_hour = operated >= timedelta(minutes=60)
    for q in quads:
        q_start = ctx.hour_start + timedelta(minutes=15 * q)
        if not intervals_overlap(valid, q_start, q_start + _FIFTEEN_MIN):
            return CellValid.invalid, "(i)" if full_hour else "(ii)"
    return CellValid.valid, "(i)" if full_hour else "(ii)"


# ---------------------------------------------------------------------------
# Branch selection — precedence confirmed against the (h)(2) chapeau
# (Ryan, 2026-07-02); no longer provisional for branches (i)–(iv)
# ---------------------------------------------------------------------------

def evaluate_hour(ctx: HourContext) -> Tuple[CellValid, str]:
    """Route one analyzer × hour to a branch and return its verdict."""
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
