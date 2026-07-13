"""Tests for the SharePoint Events adapter (clerk/adapters.py)."""
from datetime import datetime, timezone
from pathlib import Path

from clerk.adapters import (analyzer_unit_obligation, is_failed_validation,
                            parse_us_timestamp, read_events_sharepoint)
from clerk.schemas import EventType

SEED = Path(__file__).parent.parent / "samples_sharepoint" / "Events_seed_SRU_Boiler15.csv"


def test_parse_us_timestamp_is_utc():
    assert parse_us_timestamp("04/02/2026 09:00:00") == \
        datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc)
    assert parse_us_timestamp("") is None


def test_analyzer_unit_obligation_split():
    assert analyzer_unit_obligation("Boiler_15 - NOx") == ("Boiler_15", "NOx")
    assert analyzer_unit_obligation("SRU - O2") == ("SRU", "O2")


def test_failed_validation_prefix_detected():
    assert is_failed_validation("[Failed validation - 2x CD limit] drift") is True
    assert is_failed_validation("normal reason") is False


def test_multi_value_analyzers_split_into_list():
    ev = {e.EventID: e for e in read_events_sharepoint(SEED)}
    assert ev["EV3"].AnalyzerCEMIDs == ["Boiler_15 - NOx", "Boiler_15 - CO"], \
        "semicolon-separated AnalyzerCEMIDs split into the Event's list"
    assert ev["EV1"].AnalyzerCEMIDs == ["Boiler_15 - O2"]


def test_timestamps_and_types_parsed():
    ev = {e.EventID: e for e in read_events_sharepoint(SEED)}
    assert ev["EV1"].ExtentStartUTC == datetime(2026, 4, 2, 9, tzinfo=timezone.utc)
    assert ev["EV5"].EventType is EventType.Correction
    assert ev["EV5"].TargetEventID == "EV4", "amendment points at the original"
    assert ev["EV8"].ExtentEndUTC is None, "blank EndUTC -> start-only marker"


def test_failed_validation_entry_flagged_from_seed():
    ev = {e.EventID: e for e in read_events_sharepoint(SEED)}
    assert is_failed_validation(ev["EV4"].Reason)
    assert not is_failed_validation(ev["EV1"].Reason)
