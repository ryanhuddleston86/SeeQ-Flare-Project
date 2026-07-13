"""
adapters.py — read the REAL SharePoint "Events" list export into the clerk's
Event objects (pure, no clerk-logic changes).

Production reads the SharePoint Events list, whose shape differs from the
simplified sample format the runner/notebook use:

  * 22 columns (Title, EventID, EventType, TargetEventID, ExtentStartUTC,
    ExtentEndUTC, AnalyzerCEMIDs, Category, DetectionClass, ReasonCode,
    Actor, ActedAt, Reason, CorrectiveAction, EntryID, AnalyzerID,
    TargetEntryID, AmendmentReason, AmendedBy, Snapshot, Created, Created By).
    The clerk consumes a known subset; the rest are carried for audit only.
  * Timestamps are US format "MM/DD/YYYY HH:MM:SS", assumed UTC — NOT ISO-8601.
  * AnalyzerCEMIDs is multi-value, ";"-separated, each "Unit - Pollutant"
    (e.g. "Boiler_15 - NOx;Boiler_15 - CO"). The clerk's Event already
    carries AnalyzerCEMIDs as a LIST and the fold contributes an event to
    every listed analyzer, so "split into per-analyzer rows" == parse the
    ";"-list into that list (no row duplication needed).
  * EventType is TechEntry / Correction (amendments carry TargetEventID).
  * Reason may carry a "[Failed validation - ...]" prefix marking a
    failed-validation entry.

The analyzer identity used downstream is the full "Unit - Pollutant" string
exactly as it appears in the export — the roster must key on the same
identity (roster_sru_boiler15.csv does). analyzer_unit_obligation() derives
(unit, pollutant) from that identity for validation/labeling.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from clerk.schemas import Event, EventType

# The SharePoint ReasonCodes list (code -> human label), for reference/labels.
REASON_CODES = {
    "MM-01": "Monitor Malfunction",
    "QA-01": "QA/Calibration",
    "NM-01": "Non-monitor",
    "OK-01": "Other Known",
    "UK-01": "Unknown",
}

_FAILED_VALIDATION_PREFIX = "[Failed validation"


def parse_us_timestamp(s: str) -> Optional[datetime]:
    """'MM/DD/YYYY HH:MM:SS' (assumed UTC) -> tz-aware datetime. '' -> None."""
    s = (s or "").strip()
    if not s:
        return None
    return datetime.strptime(s, "%m/%d/%Y %H:%M:%S").replace(tzinfo=timezone.utc)


def analyzer_unit_obligation(analyzer_cemid: str) -> Tuple[str, str]:
    """'Boiler_15 - NOx' -> ('Boiler_15', 'NOx'). Splits on the LAST ' - '
    so a unit name containing a hyphen is tolerated."""
    a = analyzer_cemid.strip()
    if " - " in a:
        unit, pollutant = a.rsplit(" - ", 1)
        return unit.strip(), pollutant.strip()
    return a, ""


def is_failed_validation(reason: str) -> bool:
    """True when a Reason carries the '[Failed validation - ...]' prefix."""
    return (reason or "").lstrip().startswith(_FAILED_VALIDATION_PREFIX)


def read_events_sharepoint(path: Path) -> List[Event]:
    """Parse the SharePoint Events export into clerk Event objects.

    - MM/DD/YYYY timestamps -> UTC datetimes.
    - AnalyzerCEMIDs ';'-list -> Event.AnalyzerCEMIDs list (fold contributes
      to every listed analyzer — the multi-analyzer 'split').
    - Unknown/extra columns are ignored (carried only in the source file).
    Blank ExtentEndUTC stays None (a start-only marker)."""
    rows: List[Event] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            analyzers = [a.strip() for a in (r.get("AnalyzerCEMIDs") or "").split(";")
                         if a.strip()]
            rows.append(Event(
                EventID=r["EventID"].strip(),
                EventType=EventType(r["EventType"].strip()),
                TargetEventID=(r.get("TargetEventID") or "").strip() or None,
                ExtentStartUTC=parse_us_timestamp(r.get("ExtentStartUTC", "")),
                ExtentEndUTC=parse_us_timestamp(r.get("ExtentEndUTC", "")),
                AnalyzerCEMIDs=analyzers,
                Category=(r.get("Category") or "").strip(),
                ReasonCode=(r.get("ReasonCode") or "").strip(),
                Actor=(r.get("Actor") or "").strip(),
                ActedAt=parse_us_timestamp(r.get("ActedAt", "")),
                Reason=(r.get("Reason") or "").strip(),
                CorrectiveAction=(r.get("CorrectiveAction") or "").strip(),
                DetectionClass=(r.get("DetectionClass") or "").strip(),
            ))
    return rows
