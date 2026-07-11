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

from clerk.schemas import CellValid, SiteConfig

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
    # Pre-resolved CFR paragraph from the config reason→paragraph map (W4).
    # Set by grid.py via reason_to_paragraph(); None means no override —
    # _select_paragraph falls through to the auto-selection chain.
    # FLAG: provisional — mapping table not yet confirmed by Ryan.
    resolved_paragraph: Optional[str] = None


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
    (B) when it operates in only one.

    W5 — real ≥15-min temporal separation (D5):
    (iii)(A)'s requirement is "two valid data points, separated by at least
    15 minutes." The implementation uses interval_span(V) >= 15 min — the
    interval-algebra equivalent under the dense-sampling assumption (a data
    point exists arbitrarily close to any instant of validity). A
    quadrant-membership proxy ("points in two different quadrants") would
    accept data from :14:30 and :15:00 as satisfying the test; the span
    form correctly rejects that case (span = 30 s < 15 min).
    """
    if operated_quadrants == 1:
        return bool(valid)
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

def _branch_not_assessed(ctx: HourContext) -> Tuple[CellValid, str]:
    """SeeqCovered=false with no manual window — detection-based assessment
    is impossible; verdict is deferred until coverage is established."""
    return CellValid.not_assessed, "not-assessed:no-seeq-coverage"


# W4: paragraph-label → branch function (used by _select_paragraph for
# config-driven reason→paragraph overrides). FLAG: provisional.
_PARAGRAPH_BRANCH = {
    "(iv)":     _branch_iv,
    "(iii)(B)": _branch_iii_b,
    "(iii)(A)": _branch_iii_a,
    "(i)":      _branch_normal,
    "(ii)":     _branch_normal,
}


def reason_to_paragraph(reason: str, config: SiteConfig) -> Optional[str]:
    """Look up the CFR paragraph label for a reason code from the site config.

    Returns the paragraph string (e.g. "(iii)(A)") if the reason code is
    mapped, or None if no mapping exists (fall through to auto-selection).
    FLAG: provisional — ReasonParagraphMap entries not yet confirmed by Ryan.
    """
    return config.ReasonParagraphMap.get(reason)


def _unit_offline(ctx: HourContext) -> bool:
    """Step 1 — Unit-offline mask: true when no operating intervals exist.
    This is operational bookkeeping, not a §(h)(2) reading — an offline unit
    is excluded from both the numerator and denominator of the data average."""
    return not ctx.operating


def _select_paragraph(ctx: HourContext):
    """Step 2 — Paragraph selection: return the branch callable that governs
    this hour. Precedence confirmed against the §60.13(h)(2) chapeau.

    When ctx.resolved_paragraph is set (pre-resolved via reason_to_paragraph
    by the caller), that paragraph overrides the auto-selection chain.
    FLAG: provisional — override mapping not yet confirmed by Ryan.
    """
    # W4 config-driven override: resolved_paragraph wins when it maps to a branch.
    if ctx.resolved_paragraph:
        branch = _PARAGRAPH_BRANCH.get(ctx.resolved_paragraph)
        if branch is not None:
            return branch
    if ctx.failed_cal_at is not None:
        return _branch_iv
    if len(quadrants_operated(ctx)) == 1:
        return _branch_iii_b
    # Coverage-independent: manual/QA evidence never required Seeq detection.
    if ctx.manual_qa_windows:
        return _branch_iii_a
    if ctx.seeq_covered:
        return _branch_normal
    return _branch_not_assessed


def evaluate_hour(ctx: HourContext) -> Tuple[CellValid, str]:
    """Route one analyzer × hour through three explicit steps and return its verdict.

    Step 1 — unit-offline mask  →  Step 2 — paragraph selection  →  Step 3 — verdict fold.
    """
    # Step 1: offline mask fires before any §(h)(2) paragraph is consulted.
    if _unit_offline(ctx):
        return CellValid.not_operating, "not-operating"
    # Step 2: choose the governing CFR paragraph.
    branch = _select_paragraph(ctx)
    # Step 3: apply the selected branch to obtain (validity, rule).
    return branch(ctx)
