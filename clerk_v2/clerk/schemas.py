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
    # SeeqCovered=false and no branch 2/3/4 claims the hour: detection-based
    # assessment is impossible, and pretending otherwise would fabricate
    # validity. How NOT-ASSESSED rolls into availability% / the DAR
    # denominator is UNDECIDED — hard stop per §5; flagged, awaiting Ryan.
    not_assessed = "NOT-ASSESSED"


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
    # Dedicated column for machine-authored events (Ryan, 2026-07-02): blank
    # on human-authored EventTypes. Category is unused for now — do not
    # repurpose it.
    DetectionClass: str = ""


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


# F5: the explicit three-value diluent role vocabulary.
DILUENT_ROLES = frozenset({"diluent", "diluent-corrected", "not-diluent-corrected"})
DILUENT_SPECIES = frozenset({"O2", "CO2", ""})


@dataclass
class AnalyzerUnit:
    Analyzer: str
    Unit: str
    # Whether Seeq detection covers this analyzer. Default is false — only an
    # explicit "true" in the fixture enables the rule engine's Seeq branch.
    SeeqCovered: bool = False
    # W8/F5: three-value role — "diluent" (this analyzer IS the O2/CO2
    # monitor), "diluent-corrected" (a pollutant analyzer whose reading is
    # corrected using a diluent monitor), or "not-diluent-corrected"
    # (no diluent involvement; the default).
    DiluentsRole: str = "not-diluent-corrected"
    # F5: the diluent species as an explicit {O2, CO2} label. On a diluent
    # monitor: its own species. On a diluent-corrected analyzer: the species
    # of the monitor it depends on. "" for not-diluent-corrected.
    DiluentSpecies: str = ""
    # W8: diluent-basis — Analyzer ID of the diluent monitor this pollutant
    # analyzer depends on (the direct dependency pointer, kept per F5).
    # Empty for diluent monitors themselves and for analyzers that have no
    # diluent dependency.
    DiluentBasis: str = ""
    # F2: coverage window. A monitor counts toward its source's rollup only
    # for hours inside [InServiceDateUTC, OOSDateUTC). None = unbounded on
    # that side (a permanent monitor has both None). Outside the window the
    # monitor is ABSENT from the intersection — neither valid nor down.
    InServiceDateUTC: Optional[datetime] = None
    OOSDateUTC: Optional[datetime] = None

    def in_coverage(self, hour_start: datetime) -> bool:
        """True when this monitor's coverage window contains the hour."""
        if self.InServiceDateUTC is not None and hour_start < self.InServiceDateUTC:
            return False
        if self.OOSDateUTC is not None and hour_start >= self.OOSDateUTC:
            return False
        return True


@dataclass
class ValidationEvent:
    """F3: one daily-validation (calibration error check) outcome. This is
    the event stream the backdate anchor works from — the anchor for a
    failure is the most recent PASSING validation event, never the last
    hour whose data merely read valid."""
    Analyzer: str
    ValidatedAtUTC: datetime
    Passed: bool


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
class AdjudicatedCondition:
    """Step 6 output: adjudicated_condition.csv — the push target,
    structurally distinct from the pull source (the detection condition).
    One row per folded episode (fold.Observation), regardless of status —
    the push target is the full adjudicated record, not filtered to
    currently-active tickets (run.py assembles this; see its module
    docstring for the flagged scope/shape choices)."""
    Analyzer: str
    ConditionStartUTC: datetime
    ConditionEndUTC: datetime
    Status: str
    ReasonCode: str
    RuleApplied: List[str]      # distinct branch labels observed across the episode's hours
    SourceEventIDs: List[str]


@dataclass
class PIPayloadRow:
    """Step 6 output: pi_payload.csv — dense hourly, full window every run,
    replace-in-place semantics. Value is nullable: valid/invalid map to
    1.0/0.0, but NOT-OPERATING and NOT-ASSESSED have no agreed numeric
    encoding yet (tangled with the open NOT-ASSESSED rollup hard stop) —
    left blank rather than guessing. See run.py."""
    Tag: str
    HourStartUTC: datetime
    Value: Optional[float]


@dataclass
class SiteConfig:
    SiteTimeZoneIANA: str
    LookbackMonths: int
    LateXThresholdDays: int
    JitterToleranceMin: int
    PartialOperatingHourApplicability: Dict[str, bool]
    # FLAG: provisional — reason→paragraph mapping not yet confirmed by Ryan.
    # Keys are ReasonCode strings from events.csv; values are CFR paragraph
    # labels ("(iii)(A)", "(iv)", etc.). Built from ReasonParagraph: rows in
    # config.csv. An absent reason code produces no override (falls through to
    # auto-selection in _select_paragraph).
    ReasonParagraphMap: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.ReasonParagraphMap is None:
            self.ReasonParagraphMap = {}


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
                DetectionClass=(r.get("DetectionClass") or "").strip(),
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
            # F5: blank/absent role means no diluent involvement — normalize
            # to the explicit vocabulary value; reject anything else loudly.
            role = (r.get("DiluentsRole") or "").strip() or "not-diluent-corrected"
            if role not in DILUENT_ROLES:
                raise ValueError(
                    f"analyzer_units.csv: unknown DiluentsRole {role!r} for "
                    f"{r['Analyzer']} — expected one of {sorted(DILUENT_ROLES)}")
            species = (r.get("DiluentSpecies") or "").strip()
            if species not in DILUENT_SPECIES:
                raise ValueError(
                    f"analyzer_units.csv: unknown DiluentSpecies {species!r} for "
                    f"{r['Analyzer']} — expected O2, CO2, or blank")
            rows.append(AnalyzerUnit(
                Analyzer=r["Analyzer"],
                Unit=r["Unit"],
                # default false unless explicitly marked true — missing column
                # or blank cell means NOT covered
                SeeqCovered=(r.get("SeeqCovered") or "").strip().lower() == "true",
                DiluentsRole=role,
                DiluentSpecies=species,
                # W8: dependency pointer — blank/absent column defaults to ""
                DiluentBasis=(r.get("DiluentBasis") or "").strip(),
                # F2: coverage window — blank/absent means unbounded
                InServiceDateUTC=_dt_opt(r.get("InServiceDateUTC") or ""),
                OOSDateUTC=_dt_opt(r.get("OOSDateUTC") or ""),
            ))
    return rows


def read_validations(path: Path) -> List[ValidationEvent]:
    """F3: daily-validation outcomes. Returns [] when the file does not
    exist — a site with no validation feed yet simply contributes no
    validation-driven invalidation."""
    if not path.exists():
        return []
    rows: List[ValidationEvent] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            result = r["Result"].strip().lower()
            if result not in ("pass", "fail"):
                raise ValueError(
                    f"validations.csv: unknown Result {r['Result']!r} for "
                    f"{r['Analyzer']} — expected pass or fail")
            rows.append(ValidationEvent(
                Analyzer=r["Analyzer"],
                ValidatedAtUTC=_dt(r["ValidatedAtUTC"]),
                Passed=result == "pass",
            ))
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
    # FLAG: provisional — read reason→paragraph defaults from config rows.
    reason_paragraph = {
        k.split(":", 1)[1]: v.strip()
        for k, v in kv.items()
        if k.startswith("ReasonParagraph:")
    }
    return SiteConfig(
        SiteTimeZoneIANA=kv["SiteTimeZoneIANA"],
        LookbackMonths=int(kv.get("LookbackMonths", "8")),
        LateXThresholdDays=int(kv.get("LateXThresholdDays", "7")),
        JitterToleranceMin=int(kv.get("JitterToleranceMin", "5")),
        PartialOperatingHourApplicability=partial,
        ReasonParagraphMap=reason_paragraph,
    )


# ---------------------------------------------------------------------------
# Events CSV writer (delta writer output — new_events.csv)
# ---------------------------------------------------------------------------

_EVENT_HEADERS = [
    "EventID", "EventType", "TargetEventID", "ExtentStartUTC", "ExtentEndUTC",
    "AnalyzerCEMIDs", "Category", "ReasonCode", "Actor", "ActedAt", "Reason",
    "CorrectiveAction", "DetectionClass",
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
                "DetectionClass": e.DetectionClass,
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


# ---------------------------------------------------------------------------
# Step 6 output writers
# ---------------------------------------------------------------------------

_ADJUDICATED_CONDITION_HEADERS = [
    "Analyzer", "ConditionStartUTC", "ConditionEndUTC", "Status",
    "ReasonCode", "RuleApplied", "SourceEventIDs",
]


def write_adjudicated_condition(path: Path, rows: List[AdjudicatedCondition]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_ADJUDICATED_CONDITION_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "Analyzer": r.Analyzer,
                "ConditionStartUTC": r.ConditionStartUTC.isoformat(),
                "ConditionEndUTC": r.ConditionEndUTC.isoformat(),
                "Status": r.Status,
                "ReasonCode": r.ReasonCode,
                "RuleApplied": ";".join(r.RuleApplied),
                "SourceEventIDs": ";".join(r.SourceEventIDs),
            })


_PI_PAYLOAD_HEADERS = ["Tag", "HourStartUTC", "Value"]


def write_pi_payload(path: Path, rows: List[PIPayloadRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_PI_PAYLOAD_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "Tag": r.Tag,
                "HourStartUTC": r.HourStartUTC.isoformat(),
                "Value": "" if r.Value is None else r.Value,
            })
