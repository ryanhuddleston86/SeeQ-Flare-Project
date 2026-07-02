"""
Step 6 run.py tests — orchestrator wiring. Most REGULATORY/business logic
is already exhaustively tested in the underlying modules; these tests
verify the SEQUENCING and I/O boundary: right data flows to right places,
files get written, outputs are internally consistent.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from clerk.fold import Observation, Status
from clerk.run import (
    RunResult,
    _adjudicated_condition_rows,
    _months_before,
    _pi_payload_rows,
    run,
)
from clerk.schemas import CellValid, Event, EventType, GridCell

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _months_before
# ---------------------------------------------------------------------------

def test_months_before_same_year():
    assert _months_before(date(2026, 7, 2), 3) == date(2026, 4, 1)


def test_months_before_crosses_year_boundary():
    assert _months_before(date(2026, 2, 15), 8) == date(2025, 6, 1)


def test_months_before_zero_months_is_first_of_run_month():
    assert _months_before(date(2026, 7, 2), 0) == date(2026, 7, 1)


def test_months_before_large_offset_crosses_multiple_years():
    assert _months_before(date(2026, 1, 15), 25) == date(2023, 12, 1)


# ---------------------------------------------------------------------------
# _pi_payload_rows — Value encoding
# ---------------------------------------------------------------------------

def _cell(analyzer, hour, valid, rule="(i)"):
    return GridCell(Analyzer=analyzer, HourStartUTC=hour, HourLocalLabel="x",
                    OperatingFraction=1.0, Valid=valid, RuleApplied=rule,
                    ContributingEventIDs=[])


def test_pi_payload_valid_is_one():
    rows = _pi_payload_rows([_cell("A1", _utc(2026, 1, 1), CellValid.valid)])
    assert rows[0].Value == 1.0


def test_pi_payload_invalid_is_zero():
    rows = _pi_payload_rows([_cell("A1", _utc(2026, 1, 1), CellValid.invalid)])
    assert rows[0].Value == 0.0


def test_pi_payload_not_operating_is_blank():
    rows = _pi_payload_rows([_cell("A1", _utc(2026, 1, 1), CellValid.not_operating)])
    assert rows[0].Value is None


def test_pi_payload_not_assessed_is_blank():
    rows = _pi_payload_rows([_cell("A1", _utc(2026, 1, 1), CellValid.not_assessed)])
    assert rows[0].Value is None


def test_pi_payload_tag_is_analyzer():
    rows = _pi_payload_rows([_cell("CEMS-001", _utc(2026, 1, 1), CellValid.valid)])
    assert rows[0].Tag == "CEMS-001"


# ---------------------------------------------------------------------------
# _adjudicated_condition_rows
# ---------------------------------------------------------------------------

def _obs(origin_id, status, start, end, analyzers, contributing):
    return Observation(
        origin_event_id=origin_id, status=status,
        extent_start_utc=start, extent_end_utc=end,
        signed_dismissal_extent=None, detection_class=None,
        has_corrective_action=False, analyzers=analyzers,
        latest_acted_at=start, contributing_event_ids=contributing,
    )


def _event(id, event_type, target, start, end, analyzer, acted_at, reason_code=""):
    return Event(
        EventID=id, EventType=event_type, TargetEventID=target,
        ExtentStartUTC=start, ExtentEndUTC=end, AnalyzerCEMIDs=[analyzer],
        Category="", ReasonCode=reason_code, Actor="test", ActedAt=acted_at,
        Reason="", CorrectiveAction="", DetectionClass="",
    )


def test_multi_analyzer_observation_produces_one_row_per_analyzer():
    obs = _obs("E9", Status.confirmed, _utc(2026, 2, 3, 9), _utc(2026, 2, 3, 15),
              ["A1", "A2", "A3"], ["E9"])
    rows = _adjudicated_condition_rows([obs], {}, [])
    assert {r.Analyzer for r in rows} == {"A1", "A2", "A3"}
    assert all(r.ConditionStartUTC == _utc(2026, 2, 3, 9) for r in rows)


def test_rule_applied_is_sorted_distinct_set_across_overlapping_hours():
    obs = _obs("E1", Status.needs_review, _utc(2026, 1, 1, 0), _utc(2026, 1, 1, 3),
              ["A1"], ["E1"])
    cells = [
        _cell("A1", _utc(2026, 1, 1, 0), CellValid.invalid, rule="(i)"),
        _cell("A1", _utc(2026, 1, 1, 1), CellValid.invalid, rule="(iii)(A)"),
        _cell("A1", _utc(2026, 1, 1, 2), CellValid.invalid, rule="(i)"),  # duplicate
        _cell("A1", _utc(2026, 1, 1, 5), CellValid.valid, rule="(ii)"),  # outside extent
    ]
    rows = _adjudicated_condition_rows([obs], {}, cells)
    assert rows[0].RuleApplied == ["(i)", "(iii)(A)"]


def test_reason_code_from_latest_event_with_one():
    events = [
        _event("E1", EventType.SeeqDetection, None, _utc(2026, 1, 1), _utc(2026, 1, 2),
              "A1", _utc(2026, 1, 1)),
        _event("E2", EventType.Confirmation, "E1", _utc(2026, 1, 1), _utc(2026, 1, 2),
              "A1", _utc(2026, 1, 1, 5), reason_code="BKD"),
    ]
    events_by_id = {e.EventID: e for e in events}
    obs = _obs("E1", Status.confirmed, _utc(2026, 1, 1), _utc(2026, 1, 2),
              ["A1"], ["E1", "E2"])
    rows = _adjudicated_condition_rows([obs], events_by_id, [])
    assert rows[0].ReasonCode == "BKD"


def test_reason_code_blank_when_none_present():
    obs = _obs("E1", Status.needs_review, _utc(2026, 1, 1), _utc(2026, 1, 2),
              ["A1"], ["E1"])
    rows = _adjudicated_condition_rows([obs], {}, [])
    assert rows[0].ReasonCode == ""


def test_source_event_ids_is_full_contributing_history():
    obs = _obs("E1", Status.dismissed, _utc(2026, 1, 1), _utc(2026, 1, 2),
              ["A1"], ["E1", "E2", "E3"])
    rows = _adjudicated_condition_rows([obs], {}, [])
    assert rows[0].SourceEventIDs == ["E1", "E2", "E3"]


@pytest.mark.parametrize("status", [
    Status.needs_review, Status.confirmed, Status.dismissal_pending,
    Status.dismissed, Status.withdrawn, Status.superseded,
])
def test_every_status_produces_a_row_not_filtered(status):
    """The push target is the full adjudicated record, not filtered to
    active tickets — every status must appear."""
    obs = _obs("E1", status, _utc(2026, 1, 1), _utc(2026, 1, 2), ["A1"], ["E1"])
    rows = _adjudicated_condition_rows([obs], {}, [])
    assert len(rows) == 1
    assert rows[0].Status == status.value


# ---------------------------------------------------------------------------
# Full synthetic pipeline (one slower end-to-end test)
# ---------------------------------------------------------------------------

RUN_DATE = date(2026, 2, 15)
RUN_AT = datetime(2026, 2, 15, 6, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def synthetic_run(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("run_out")
    result = run(FIXTURES, out_dir, RUN_DATE, RUN_AT)
    return result, out_dir


def test_all_output_files_exist_and_are_nonempty(synthetic_run):
    _, out_dir = synthetic_run
    for name in ["grid.csv", "new_events.csv", "adjudicated_condition.csv",
                "pi_payload.csv", "digest.md", "snapshot/events.csv"]:
        p = out_dir / name
        assert p.exists(), f"{name} missing"
        assert p.stat().st_size > 0, f"{name} is empty"


def test_snapshot_is_byte_identical_to_source_events_csv(synthetic_run):
    _, out_dir = synthetic_run
    assert (out_dir / "snapshot" / "events.csv").read_bytes() == \
        (FIXTURES / "events.csv").read_bytes()


def test_grid_row_count_matches_analyzers_times_hours(synthetic_run):
    result, _ = synthetic_run
    from clerk.schemas import read_analyzer_units, read_config
    config = read_config(FIXTURES / "config.csv")
    analyzer_units = read_analyzer_units(FIXTURES / "analyzer_units.csv")
    window_start = _months_before(RUN_DATE, config.LookbackMonths)
    hours = (RUN_DATE + timedelta(days=1) - window_start).days * 24
    assert len(result.grid_cells) == hours * len(analyzer_units)


def test_first_run_no_prior_grid_fixture(synthetic_run):
    result, _ = synthetic_run
    assert result.diff_result.is_first_run is True


def test_digest_starts_with_heartbeat(synthetic_run):
    result, _ = synthetic_run
    assert result.digest_text.startswith("# Clerk run 2026-02-15T06:00:00")


def test_new_events_include_born_and_withdrawn(synthetic_run):
    result, _ = synthetic_run
    types = {e.EventType for e in result.delta_result.new_events}
    assert EventType.SeeqDetection in types
    assert EventType.Withdrawn in types


def test_output_label_not_set_for_synthetic_run(synthetic_run):
    result, _ = synthetic_run
    assert not result.digest_text.startswith("<!--")


def test_run_creates_missing_out_dir(tmp_path):
    out_dir = tmp_path / "nested" / "out"
    assert not out_dir.exists()
    run(FIXTURES, out_dir, RUN_DATE, RUN_AT)
    assert out_dir.exists()
    assert (out_dir / "grid.csv").exists()


def test_run_is_deterministic_across_two_invocations(tmp_path):
    """Weak echo of G1's spirit: same inputs, same outputs. NOT the full
    G1 loop (that requires the bridge appending new_events.csv back into
    events.csv between two consecutive nights) — just confirms this run
    itself is a pure function of its inputs."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    run(FIXTURES, out_a, RUN_DATE, RUN_AT)
    run(FIXTURES, out_b, RUN_DATE, RUN_AT)
    assert (out_a / "grid.csv").read_bytes() == (out_b / "grid.csv").read_bytes()
    assert (out_a / "pi_payload.csv").read_bytes() == (out_b / "pi_payload.csv").read_bytes()
    assert (out_a / "adjudicated_condition.csv").read_bytes() == \
        (out_b / "adjudicated_condition.csv").read_bytes()


def test_output_label_is_written_as_leading_marker(tmp_path):
    result = run(FIXTURES, tmp_path, RUN_DATE, RUN_AT, output_label="TEST LABEL")
    assert result.digest_text.startswith("<!-- TEST LABEL -->")
