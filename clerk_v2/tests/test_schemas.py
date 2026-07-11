"""
Tests for clerk/clerk/schemas.py — dataclasses, CSV readers, and grid writer.
"""
from datetime import datetime, timezone as tz
from pathlib import Path

import pytest

from clerk.schemas import (
    AnalyzerUnit,
    CellValid,
    EventType,
    GridCell,
    read_analyzer_units,
    read_capsules,
    read_config,
    read_events,
    read_grid,
    read_operating,
    read_pull_windows,
    read_qa_windows,
    write_grid,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures (pytest)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def events():
    return read_events(FIXTURES / "events.csv")


@pytest.fixture(scope="module")
def capsules():
    return read_capsules(FIXTURES / "capsules.csv")


# ---------------------------------------------------------------------------
# Enum exhaustiveness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "TechEntry", "SeeqDetection", "Confirmation", "Correction",
    "DismissalProposed", "Approval", "DismissalRejected", "Reopen",
    "BoundaryUpdate", "Withdrawn", "Superseded",
])
def test_event_type_enum_all_values(value):
    assert EventType(value).value == value


def test_detection_class_is_opaque_string(capsules):
    """DetectionClass is a matching key, not an enum — unknown values must parse."""
    classes = {c.DetectionClass for c in capsules}
    assert "legacy-blended" in classes, "opaque class from real exports must survive parsing"
    for c in capsules:
        assert isinstance(c.DetectionClass, str)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def test_read_events_count(events):
    assert len(events) == 17


def test_read_events_types_correct(events):
    types = [e.EventType for e in events]
    assert EventType.SeeqDetection in types
    assert EventType.Confirmation in types
    assert EventType.DismissalProposed in types
    assert EventType.Approval in types
    assert EventType.TechEntry in types
    assert EventType.Correction in types


def test_read_events_timestamps_are_utc_aware(events):
    for e in events:
        assert e.ActedAt.tzinfo is not None, f"{e.EventID}: ActedAt must be UTC-aware"
        if e.ExtentStartUTC is not None:
            assert e.ExtentStartUTC.tzinfo is not None
        if e.ExtentEndUTC is not None:
            assert e.ExtentEndUTC.tzinfo is not None


def test_read_events_actedAt_is_datetime(events):
    for e in events:
        assert isinstance(e.ActedAt, datetime)


def test_read_events_target_event_id_none_for_root_events(events):
    root_types = {EventType.SeeqDetection, EventType.TechEntry}
    for e in events:
        if e.EventType in root_types:
            assert e.TargetEventID is None, f"{e.EventID} should have no TargetEventID"


def test_read_events_target_event_id_present_on_derived(events):
    derived_types = {EventType.Confirmation, EventType.Correction,
                     EventType.DismissalProposed, EventType.Approval}
    for e in events:
        if e.EventType in derived_types:
            assert e.TargetEventID is not None, f"{e.EventID} should reference a TargetEventID"


def test_dismissal_events_have_blank_reason_code(events):
    dismissal_types = {EventType.DismissalProposed, EventType.Approval}
    for e in events:
        if e.EventType in dismissal_types:
            assert e.ReasonCode == "", (
                f"{e.EventID} ({e.EventType}) must carry blank ReasonCode by design"
            )


def test_analyzer_cemids_is_list(events):
    for e in events:
        assert isinstance(e.AnalyzerCEMIDs, list)
        assert len(e.AnalyzerCEMIDs) >= 1


def test_multi_analyzer_cemids_split_correctly(events):
    multi = [e for e in events if len(e.AnalyzerCEMIDs) > 1]
    assert len(multi) >= 1, "fixture must contain at least one multi-CEMID event (E008)"
    for e in multi:
        assert all(";" not in cid for cid in e.AnalyzerCEMIDs), "semicolons must not survive into list items"


def test_single_analyzer_cemid_is_list_of_one(events):
    single = [e for e in events if len(e.AnalyzerCEMIDs) == 1]
    assert len(single) >= 1
    for e in single:
        assert ";" not in e.AnalyzerCEMIDs[0]


# ---------------------------------------------------------------------------
# Capsules
# ---------------------------------------------------------------------------

def test_read_capsules_count(capsules):
    assert len(capsules) == 10


def test_read_capsules_timestamps_utc_aware(capsules):
    for c in capsules:
        assert c.CapsuleStartUTC.tzinfo is not None
        assert c.CapsuleEndUTC.tzinfo is not None
        assert c.CapsuleStartUTC < c.CapsuleEndUTC


def test_read_capsules_known_detection_classes_present(capsules):
    classes = {c.DetectionClass for c in capsules}
    assert "status-offline" in classes
    assert "failed-daily-validation" in classes


# ---------------------------------------------------------------------------
# Operating windows + analyzer units
# ---------------------------------------------------------------------------

def test_read_operating():
    windows = read_operating(FIXTURES / "operating.csv")
    assert len(windows) == 3
    for w in windows:
        assert w.StartUTC < w.EndUTC
        assert w.StartUTC.tzinfo is not None


def test_read_analyzer_units():
    units = read_analyzer_units(FIXTURES / "analyzer_units.csv")
    assert len(units) == 6
    analyzers = {u.Analyzer for u in units}
    assert {"CEMS-001", "CEMS-002", "CEMS-003",
            "LUBEFLR-NHV-BTU", "LUBEFLR-H2S-PCT", "LUBEFLR-H2S-PPM"} == analyzers


def test_analyzer_units_all_units_present():
    units = read_analyzer_units(FIXTURES / "analyzer_units.csv")
    unit_names = {u.Unit for u in units}
    assert {"UNIT-A", "UNIT-B", "LUBE_FLARE"} == unit_names


def test_all_three_lube_channels_map_to_lube_flare_unit():
    units = read_analyzer_units(FIXTURES / "analyzer_units.csv")
    lube = {u.Analyzer for u in units if u.Unit == "LUBE_FLARE"}
    assert lube == {"LUBEFLR-NHV-BTU", "LUBEFLR-H2S-PCT", "LUBEFLR-H2S-PPM"}


def test_seeq_covered_true_only_for_lube_channels():
    units = read_analyzer_units(FIXTURES / "analyzer_units.csv")
    covered = {u.Analyzer for u in units if u.SeeqCovered}
    assert covered == {"LUBEFLR-NHV-BTU", "LUBEFLR-H2S-PCT", "LUBEFLR-H2S-PPM"}


def test_seeq_covered_defaults_false_when_column_absent(tmp_path):
    """Absent SeeqCovered column means NOT covered by default."""
    p = tmp_path / "analyzer_units.csv"
    p.write_text("Analyzer,Unit\nX-1,U-1\n")
    units = read_analyzer_units(p)
    assert units[0].SeeqCovered is False


def test_seeq_covered_blank_and_junk_are_false(tmp_path):
    p = tmp_path / "analyzer_units.csv"
    p.write_text("Analyzer,Unit,SeeqCovered\nX-1,U-1,\nX-2,U-1,yes\nX-3,U-1,TRUE\n")
    units = {u.Analyzer: u.SeeqCovered for u in read_analyzer_units(p)}
    assert units == {"X-1": False, "X-2": False, "X-3": True}, \
        "only an explicit true (case-insensitive) enables coverage"


# ---------------------------------------------------------------------------
# QA windows
# ---------------------------------------------------------------------------

def test_read_qa_windows():
    windows = read_qa_windows(FIXTURES / "qa_windows.csv")
    assert len(windows) == 1
    w = windows[0]
    assert w.Analyzer == "CEMS-001"
    assert w.SourceRef == "CGA-2026-001"
    assert w.StartUTC < w.EndUTC
    assert w.StartUTC.tzinfo is not None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_read_config_scalar_fields():
    cfg = read_config(FIXTURES / "config.csv")
    assert cfg.SiteTimeZoneIANA == "America/New_York"
    assert cfg.LookbackMonths == 8
    assert cfg.LateXThresholdDays == 7
    assert cfg.JitterToleranceMin == 5


def test_read_config_partial_operating_hour_applicability():
    cfg = read_config(FIXTURES / "config.csv")
    assert cfg.PartialOperatingHourApplicability["MATS"] is False
    assert cfg.PartialOperatingHourApplicability["NSPS"] is True


# ---------------------------------------------------------------------------
# Grid — absent prior_grid and round-trip
# ---------------------------------------------------------------------------

def test_absent_prior_grid_returns_empty_list(tmp_path):
    assert read_grid(tmp_path / "no_such_file.csv") == []


def _sample_cells():
    return [
        GridCell(
            Analyzer="CEMS-001",
            HourStartUTC=datetime(2026, 1, 15, 14, 0, 0, tzinfo=tz.utc),
            HourLocalLabel="2026-01-15 09:00 EST",
            OperatingFraction=1.0,
            Valid=CellValid.invalid,
            RuleApplied="(iii)(A)",
            ContributingEventIDs=["E001", "E002"],
        ),
        GridCell(
            Analyzer="CEMS-002",
            HourStartUTC=datetime(2026, 1, 15, 15, 0, 0, tzinfo=tz.utc),
            HourLocalLabel="2026-01-15 10:00 EST",
            OperatingFraction=0.75,
            Valid=CellValid.valid,
            RuleApplied="(i)",
            ContributingEventIDs=[],
        ),
        GridCell(
            Analyzer="CEMS-001",
            HourStartUTC=datetime(2026, 1, 15, 16, 0, 0, tzinfo=tz.utc),
            HourLocalLabel="2026-01-15 11:00 EST",
            OperatingFraction=0.0,
            Valid=CellValid.not_operating,
            RuleApplied="not-operating",
            ContributingEventIDs=[],
        ),
    ]


def test_grid_round_trip(tmp_path):
    cells = _sample_cells()
    out = tmp_path / "grid.csv"
    write_grid(out, cells)
    reloaded = read_grid(out)

    assert len(reloaded) == len(cells)
    for orig, loaded in zip(cells, reloaded):
        assert loaded.Analyzer == orig.Analyzer
        assert loaded.HourStartUTC == orig.HourStartUTC
        assert loaded.HourLocalLabel == orig.HourLocalLabel
        assert loaded.OperatingFraction == pytest.approx(orig.OperatingFraction)
        assert loaded.Valid == orig.Valid
        assert loaded.RuleApplied == orig.RuleApplied
        assert loaded.ContributingEventIDs == orig.ContributingEventIDs


def test_grid_round_trip_empty_contributing_ids(tmp_path):
    """ContributingEventIDs=[] must survive a write/read cycle as []."""
    cells = [_sample_cells()[1]]  # the cell with no contributing events
    out = tmp_path / "grid_empty_ids.csv"
    write_grid(out, cells)
    reloaded = read_grid(out)
    assert reloaded[0].ContributingEventIDs == []


def test_grid_write_twice_byte_identical(tmp_path):
    """Gate G1 prerequisite: deterministic serialization."""
    cells = _sample_cells()
    out1 = tmp_path / "g1.csv"
    out2 = tmp_path / "g2.csv"
    write_grid(out1, cells)
    write_grid(out2, cells)
    assert out1.read_bytes() == out2.read_bytes()


def test_grid_write_creates_parent_dirs(tmp_path):
    cells = _sample_cells()[:1]
    deep = tmp_path / "out" / "2026-01-15" / "grid.csv"
    write_grid(deep, cells)
    assert deep.exists()


# ---------------------------------------------------------------------------
# Pull windows (spec open item 7 — withdrawal scoping input)
# ---------------------------------------------------------------------------

def test_not_assessed_round_trips_through_grid_csv(tmp_path):
    cell = GridCell(
        Analyzer="CEMS-001",
        HourStartUTC=datetime(2026, 1, 15, 14, 0, 0, tzinfo=tz.utc),
        HourLocalLabel="2026-01-15 09:00 EST",
        OperatingFraction=1.0,
        Valid=CellValid.not_assessed,
        RuleApplied="not-assessed:no-seeq-coverage",
        ContributingEventIDs=[],
    )
    p = tmp_path / "grid.csv"
    write_grid(p, [cell])
    back = read_grid(p)
    assert back[0].Valid is CellValid.not_assessed


def test_read_pull_windows():
    windows = read_pull_windows(FIXTURES / "pull_windows.csv")
    assert len(windows) == 1
    w = windows[0]
    assert w.Night == "1"
    assert w.PullStartUTC.tzinfo is not None
    assert w.PullEndUTC.tzinfo is not None
    assert w.PullStartUTC < w.PullEndUTC


# ---------------------------------------------------------------------------
# Lube flare pattern — one physical event, three per-channel compliance facts
# ---------------------------------------------------------------------------

LUBE_CHANNELS = {"LUBEFLR-NHV-BTU", "LUBEFLR-H2S-PCT", "LUBEFLR-H2S-PPM"}


def _lube_capsules(capsules):
    return [c for c in capsules if c.Analyzer in LUBE_CHANNELS]


def test_three_channel_outage_windows_diverge(capsules):
    """Same physical event, different per-channel totals — this is correct and
    must never be 'fixed' by cross-analyzer correlation or merging."""
    def total_hours(analyzer):
        return sum(
            (c.CapsuleEndUTC - c.CapsuleStartUTC).total_seconds() / 3600
            for c in _lube_capsules(capsules) if c.Analyzer == analyzer
        )
    assert total_hours("LUBEFLR-NHV-BTU") == pytest.approx(5.0)
    assert total_hours("LUBEFLR-H2S-PCT") == pytest.approx(17.0)
    assert total_hours("LUBEFLR-H2S-PPM") == pytest.approx(17.0)


def test_abutting_fragments_present(capsules):
    """Upstream max capsule duration is 2 h — abutting fragments (end == next
    start) are the norm. The fixture must contain at least one abutting pair."""
    h2s = sorted(
        (c for c in _lube_capsules(capsules) if c.Analyzer == "LUBEFLR-H2S-PCT"),
        key=lambda c: c.CapsuleStartUTC,
    )
    abutting = any(
        a.CapsuleEndUTC == b.CapsuleStartUTC for a, b in zip(h2s, h2s[1:])
    )
    assert abutting, "fixture must contain abutting fragments for the merge path"


def test_btu_fragments_have_a_gap_not_abutment(capsules):
    """BTU's two fragments are separated — divergence is per-channel, not shared."""
    btu = sorted(
        (c for c in _lube_capsules(capsules) if c.Analyzer == "LUBEFLR-NHV-BTU"),
        key=lambda c: c.CapsuleStartUTC,
    )
    assert len(btu) == 2
    assert btu[0].CapsuleEndUTC < btu[1].CapsuleStartUTC


def test_tech_entry_lists_all_three_lube_cemids(events):
    multi = [
        e for e in events
        if e.EventType == EventType.TechEntry and set(e.AnalyzerCEMIDs) == LUBE_CHANNELS
    ]
    assert len(multi) == 1, "exactly one TechEntry must list all three lube CEMIDs"
    e = multi[0]
    assert e.ExtentStartUTC is not None and e.ExtentEndUTC is not None
    assert e.ExtentStartUTC < e.ExtentEndUTC
