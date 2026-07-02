"""
CEMS Clerk — schemas layer (the only I/O layer).

Dataclasses mirror every fixture CSV exactly.
A live-wiring swap replaces the reader functions here; nothing else changes.

All timestamps are UTC-aware (tzinfo set). A naive datetime anywhere is a bug.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    TechEntry          = "TechEntry"
    SeeqDetection      = "SeeqDetection"
    Confirmation       = "Confirmation"
    Correction         = "Correction"
    DismissalProposed  = "DismissalProposed"
    Approval           = "Approval"
    DismissalRejected  = "DismissalRejected"
    Reopen             = "Reopen"
    BoundaryUpdate     = "BoundaryUpdate"
    Withdrawn          = "Withdrawn"
    Superseded         = "Superseded"


# DetectionClass is an OPAQUE matching key, not an enum: the delta writer matches
# per analyzer + class and never interprets the value. Known values so far:
# "status-offline", "failed-daily-validation", "legacy-blended" (demo-seed export).
DetectionClass = str


class CellValid(str, Enum):
    valid        = "1"
    invalid      = "0"
    not_operating = "NOT-OPERATING"


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _dt(s: str) -> datetime:
    """Parse ISO-8601 UTC string → timezone-aware datetime."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _dt_opt(s: str) -> Optional[datetime]:
    return _dt(s) if s.strip() else None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Event:
    EventID: str
    EventType: EventType
    TargetEventID: Optional[str]
    ExtentStartUTC: Optional[datetime]
    ExtentEndUTC: Optional[datetime]
    AnalyzerCEMIDs: List[str]       # split from semicolon-delimited CSV field
    Category: str
    ReasonCode: str                 # blank on DismissalProposed / Approval by design
    Actor: str
    ActedAt: datetime
    Reason: str
    CorrectiveAction: str


@dataclass
class Capsule:
    Analyzer: str
    DetectionClass: DetectionClass
    CapsuleStartUTC: datetime
    CapsuleEndUTC: datetime


@dataclass
class OperatingWindow:
    Unit: str
    StartUTC: datetime
    EndUTC: datetime


@dataclass
class AnalyzerUnit:
    Analyzer: str
    Unit: str


@dataclass
class QAWindow:
    Analyzer: str
    StartUTC: datetime
    EndUTC: datetime
    SourceRef: str


@dataclass
class GridCell:
    Analyzer: str
    HourStartUTC: datetime
    HourLocalLabel: str
    OperatingFraction: float
    Valid: CellValid
    RuleApplied: str
    ContributingEventIDs: List[str]  # split from semicolon-delimited CSV field


@dataclass
class PullWindow:
    """The extent of tonight's spy.pull. Spec open item 7, made concrete by the
    real-data drift pair: Withdrawn may only be emitted for episodes fully
    INSIDE this window — absence of a capsule outside the pulled range is not
    evidence of anything."""
    Night: str
    PullStartUTC: datetime
    PullEndUTC: datetime


@dataclass
class SiteConfig:
    SiteTimeZoneIANA: str
    LookbackMonths: int
    LateXThresholdDays: int
    JitterToleranceMin: int
    PartialOperatingHourApplicability: Dict[str, bool]


# ---------------------------------------------------------------------------
# CSV readers
# ---------------------------------------------------------------------------

def read_events(path: Path) -> List[Event]:
    rows: List[Event] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(Event(
                EventID=r["EventID"],
                EventType=EventType(r["EventType"]),
                TargetEventID=r["TargetEventID"] or None,
                ExtentStartUTC=_dt_opt(r["ExtentStartUTC"]),
                ExtentEndUTC=_dt_opt(r["ExtentEndUTC"]),
                AnalyzerCEMIDs=[c.strip() for c in r["AnalyzerCEMIDs"].split(";") if c.strip()],
                Category=r["Category"],
                ReasonCode=r["ReasonCode"],
                Actor=r["Actor"],
                ActedAt=_dt(r["ActedAt"]),
                Reason=r["Reason"],
                CorrectiveAction=r["CorrectiveAction"],
            ))
    return rows


def read_capsules(path: Path) -> List[Capsule]:
    rows: List[Capsule] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(Capsule(
                Analyzer=r["Analyzer"],
                DetectionClass=r["DetectionClass"],
                CapsuleStartUTC=_dt(r["CapsuleStartUTC"]),
                CapsuleEndUTC=_dt(r["CapsuleEndUTC"]),
            ))
    return rows


def read_operating(path: Path) -> List[OperatingWindow]:
    rows: List[OperatingWindow] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(OperatingWindow(
                Unit=r["Unit"],
                StartUTC=_dt(r["StartUTC"]),
                EndUTC=_dt(r["EndUTC"]),
            ))
    return rows


def read_analyzer_units(path: Path) -> List[AnalyzerUnit]:
    rows: List[AnalyzerUnit] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(AnalyzerUnit(Analyzer=r["Analyzer"], Unit=r["Unit"]))
    return rows


def read_qa_windows(path: Path) -> List[QAWindow]:
    rows: List[QAWindow] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(QAWindow(
                Analyzer=r["Analyzer"],
                StartUTC=_dt(r["StartUTC"]),
                EndUTC=_dt(r["EndUTC"]),
                SourceRef=r["SourceRef"],
            ))
    return rows


def read_pull_windows(path: Path) -> List[PullWindow]:
    rows: List[PullWindow] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(PullWindow(
                Night=r["Night"],
                PullStartUTC=_dt(r["PullStartUTC"]),
                PullEndUTC=_dt(r["PullEndUTC"]),
            ))
    return rows


def read_config(path: Path) -> SiteConfig:
    kv: Dict[str, str] = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            kv[r["key"]] = r["value"]
    partial = {
        k.split(":", 1)[1]: v.strip().lower() == "true"
        for k, v in kv.items()
        if k.startswith("PartialOperatingHourApplicability:")
    }
    return SiteConfig(
        SiteTimeZoneIANA=kv["SiteTimeZoneIANA"],
        LookbackMonths=int(kv.get("LookbackMonths", "8")),
        LateXThresholdDays=int(kv.get("LateXThresholdDays", "7")),
        JitterToleranceMin=int(kv.get("JitterToleranceMin", "5")),
        PartialOperatingHourApplicability=partial,
    )


# ---------------------------------------------------------------------------
# Events CSV writer (delta writer output — new_events.csv)
# ---------------------------------------------------------------------------

_EVENT_HEADERS = [
    "EventID", "EventType", "TargetEventID", "ExtentStartUTC", "ExtentEndUTC",
    "AnalyzerCEMIDs", "Category", "ReasonCode", "Actor", "ActedAt", "Reason",
    "CorrectiveAction",
]


def write_events(path: Path, events: List[Event]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_EVENT_HEADERS)
        w.writeheader()
        for e in events:
            w.writerow({
                "EventID": e.EventID,
                "EventType": e.EventType.value,
                "TargetEventID": e.TargetEventID or "",
                "ExtentStartUTC": e.ExtentStartUTC.isoformat() if e.ExtentStartUTC else "",
                "ExtentEndUTC": e.ExtentEndUTC.isoformat() if e.ExtentEndUTC else "",
                "AnalyzerCEMIDs": ";".join(e.AnalyzerCEMIDs),
                "Category": e.Category,
                "ReasonCode": e.ReasonCode,
                "Actor": e.Actor,
                "ActedAt": e.ActedAt.isoformat(),
                "Reason": e.Reason,
                "CorrectiveAction": e.CorrectiveAction,
            })


# ---------------------------------------------------------------------------
# Grid CSV reader / writer
# ---------------------------------------------------------------------------

_GRID_HEADERS = [
    "Analyzer", "HourStartUTC", "HourLocalLabel", "OperatingFraction",
    "Valid", "RuleApplied", "ContributingEventIDs",
]


def read_grid(path: Path) -> List[GridCell]:
    """Returns [] when path does not exist (first run: no prior_grid.csv)."""
    if not path.exists():
        return []
    rows: List[GridCell] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(GridCell(
                Analyzer=r["Analyzer"],
                HourStartUTC=_dt(r["HourStartUTC"]),
                HourLocalLabel=r["HourLocalLabel"],
                OperatingFraction=float(r["OperatingFraction"]),
                Valid=CellValid(r["Valid"]),
                RuleApplied=r["RuleApplied"],
                ContributingEventIDs=[e.strip() for e in r["ContributingEventIDs"].split(";") if e.strip()],
            ))
    return rows


def write_grid(path: Path, cells: List[GridCell]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_GRID_HEADERS)
        w.writeheader()
        for c in cells:
            w.writerow({
                "Analyzer": c.Analyzer,
                "HourStartUTC": c.HourStartUTC.isoformat(),
                "HourLocalLabel": c.HourLocalLabel,
                "OperatingFraction": c.OperatingFraction,
                "Valid": c.Valid.value,
                "RuleApplied": c.RuleApplied,
                "ContributingEventIDs": ";".join(c.ContributingEventIDs),
            })
