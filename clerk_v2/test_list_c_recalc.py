"""
Test scenarios for List C recalc-triggering logic spike.

Four scenarios:
  1. Full backfill from empty state.
  2. Idempotency: same List C recalculated twice → identical result.
  3. Record corrected (shortened) → modified calc updates, raw calc unaffected.
  4. Record added → modified calc picks it up, raw calc unaffected.
"""

from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from list_c_recalc import DowntimeRecord, compute_modified_calc, take_raw_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_record(
    id: str,
    start: datetime,
    hours: float,
    status: str = "Approved",
    last_modified: datetime | None = None,
) -> DowntimeRecord:
    end = start + timedelta(hours=hours)
    return DowntimeRecord(
        id=id,
        start=start,
        end=end,
        status=status,
        last_modified=last_modified or start,
    )


JAN = datetime(2025, 1, 10, 8, 0)   # anchor: Jan 10 2025 08:00
FEB = datetime(2025, 2, 5, 12, 0)   # anchor: Feb  5 2025 12:00


# ---------------------------------------------------------------------------
# Scenario 1: Full backfill from empty state
# ---------------------------------------------------------------------------

def test_empty_list_c_produces_empty_calc():
    assert compute_modified_calc([]) == {}


def test_first_ever_recalc_produces_correct_totals():
    records = [
        make_record("r1", JAN, hours=4.0),
        make_record("r2", JAN, hours=2.5),
        make_record("r3", FEB, hours=8.0),
    ]
    result = compute_modified_calc(records)

    assert result[(2025, 1)] == pytest.approx(6.5)
    assert result[(2025, 2)] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Scenario 2: Idempotency — same List C recalculated twice
# ---------------------------------------------------------------------------

def test_recalc_twice_is_idempotent():
    records = [
        make_record("r1", JAN, hours=3.0),
        make_record("r2", FEB, hours=5.0),
    ]

    first  = compute_modified_calc(records)
    second = compute_modified_calc(records)

    assert first == second


def test_recalc_does_not_mutate_input():
    records = [make_record("r1", JAN, hours=3.0)]
    original_end = records[0].end

    compute_modified_calc(records)

    assert records[0].end == original_end


# ---------------------------------------------------------------------------
# Scenario 3: Record corrected (shortened) between two recalcs
# ---------------------------------------------------------------------------

def test_correction_updates_modified_calc_and_leaves_raw_untouched():
    records = [
        make_record("r1", JAN, hours=10.0),
        make_record("r2", FEB, hours=6.0),
    ]

    # RAW snapshot taken once before any correction.
    raw = take_raw_snapshot(records)

    # Before correction.
    before = compute_modified_calc(records)
    assert before[(2025, 1)] == pytest.approx(10.0)

    # Simulate an approved reductive edit: r1 shortened from 10 h → 6 h.
    records[0] = make_record(
        "r1",
        JAN,
        hours=6.0,
        status="Approved",
        last_modified=JAN + timedelta(days=1),
    )

    # Modified calc reflects the correction.
    after = compute_modified_calc(records)
    assert after[(2025, 1)] == pytest.approx(6.0), "modified calc must show shortened total"
    assert after[(2025, 2)] == pytest.approx(6.0), "unaffected month must be unchanged"

    # Raw calc is completely unaffected.
    assert raw[(2025, 1)] == pytest.approx(10.0), "raw calc must retain original value"
    assert raw[(2025, 2)] == pytest.approx(6.0)


def test_old_value_does_not_leak_into_modified_calc_after_correction():
    """No trace of the pre-correction duration in the modified calc result."""
    records = [make_record("r1", JAN, hours=10.0)]
    compute_modified_calc(records)  # first recalc

    records[0] = make_record("r1", JAN, hours=6.0)
    after = compute_modified_calc(records)

    # Must be exactly 6, not 16 (double-count) or 10 (stale).
    assert after[(2025, 1)] == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# Scenario 4: New record added between two recalcs
# ---------------------------------------------------------------------------

def test_new_record_added_is_reflected_in_modified_calc():
    records = [make_record("r1", JAN, hours=4.0)]

    raw    = take_raw_snapshot(records)
    before = compute_modified_calc(records)
    assert before[(2025, 1)] == pytest.approx(4.0)
    assert (2025, 2) not in before

    # New downtime event logged in February.
    records.append(make_record("r2", FEB, hours=3.0))

    after = compute_modified_calc(records)
    assert after[(2025, 1)] == pytest.approx(4.0), "existing month unchanged"
    assert after[(2025, 2)] == pytest.approx(3.0), "new record picked up"

    # Raw calc was taken before the addition — must not include r2.
    assert (2025, 2) not in raw, "raw calc must not reflect post-snapshot additions"


# ---------------------------------------------------------------------------
# D2: Only Approved records contribute; Pending and Rejected contribute zero
# ---------------------------------------------------------------------------

def test_only_approved_records_counted():
    records = [
        make_record("r1", JAN, hours=2.0, status="Approved"),
        make_record("r2", JAN, hours=3.0, status="Pending"),
        make_record("r3", JAN, hours=1.0, status="Rejected"),
    ]
    result = compute_modified_calc(records)
    assert result[(2025, 1)] == pytest.approx(2.0), "only Approved hours count"


def test_pending_only_produces_no_output():
    records = [make_record("r1", JAN, hours=5.0, status="Pending")]
    result = compute_modified_calc(records)
    assert result == {}, "pending-only month must not appear in calc output"


def test_rejected_only_produces_no_output():
    records = [make_record("r1", JAN, hours=4.0, status="Rejected")]
    result = compute_modified_calc(records)
    assert result == {}, "rejected-only month must not appear in calc output"
