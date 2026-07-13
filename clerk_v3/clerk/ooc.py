"""
ooc.py — Out-Of-Control (OOC) invalidation, 40 CFR Part 60 Appendix F
§4.3.1 (pure, no I/O).

This is its OWN mechanism, distinct from fault/maintenance downtime. It is
driven by a per-analyzer VALIDATION CAPSULE stream — each capsule is
(start, end, status) with status in {Pass, 2x_Fail, 4x_Fail}. Any failure
invalidates the whole analyzer (high vs. low does not matter). The output
is a set of OOC windows per analyzer; the whole window is wholesale invalid
end-to-end (grid.py scores the boundary hours — this module only finds the
windows).

Entrance (asymmetric by level):
  * 2x: on the 5th CONSECUTIVE 2x_Fail, the window starts at the START of
    that 5th capsule. Forward from the 5th only — the four priors are NOT
    retroactively invalid. Any Pass resets the consecutive count to 0. A
    4x_Fail does not increment the 2x count.
  * 4x: on ANY 4x_Fail, the window starts at the START of the capsule
    IMMEDIATELY PRECEDING the 4x (whatever its status). One 4x is enough.

Exit (both): the window ends at the END of the next Pass capsule after
corrective action.

Flagged edge cases (NOT invented — surfaced for confirmation):
  (1) a 4x_Fail with no preceding capsule (4x on the very first validation):
      the entrance is undefined; no window is opened, an OOCFlag is emitted.
  (2) a window still open at the end of the stream (no closing Pass): its
      end is undefined. If `open_tail_end` is supplied the window is closed
      there CONSERVATIVELY (OOC until proven back in control) and an OOCFlag
      is emitted; if not supplied, no window is emitted and the flag still
      fires. Either way the caller is told.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

Interval = Tuple[datetime, datetime]

PASS = "Pass"
FAIL_2X = "2x_Fail"
FAIL_4X = "4x_Fail"
_STATUSES = frozenset({PASS, FAIL_2X, FAIL_4X})

_TWO_X_RUN = 5  # 5th consecutive 2x_Fail triggers entrance


@dataclass
class ValidationCapsule:
    Analyzer: str
    StartUTC: datetime
    EndUTC: datetime
    Status: str  # Pass | 2x_Fail | 4x_Fail


@dataclass
class OOCFlag:
    Analyzer: str
    kind: str      # "4x-no-preceding-capsule" | "open-tail-no-closing-pass"
    detail: str


def compute_ooc_windows(
    capsules: List[ValidationCapsule],
    open_tail_end: Optional[datetime] = None,
) -> Tuple[Dict[str, List[Interval]], List[OOCFlag]]:
    """Return ({analyzer: [ooc_window, ...]}, [flags]).

    Windows are closed [start, end) intervals. `open_tail_end`, if given,
    conservatively closes a still-open window at that instant (and flags it).
    """
    for c in capsules:
        if c.Status not in _STATUSES:
            raise ValueError(f"unknown validation status {c.Status!r} for "
                             f"{c.Analyzer} — expected {sorted(_STATUSES)}")

    by_analyzer: Dict[str, List[ValidationCapsule]] = {}
    for c in capsules:
        by_analyzer.setdefault(c.Analyzer, []).append(c)

    windows: Dict[str, List[Interval]] = {}
    flags: List[OOCFlag] = []

    for analyzer, caps in by_analyzer.items():
        caps = sorted(caps, key=lambda c: c.StartUTC)
        out: List[Interval] = []
        consec_2x = 0
        entrance: Optional[datetime] = None   # set while a window is open

        for i, cap in enumerate(caps):
            if entrance is None:
                # NOT in OOC — watch for an entrance.
                if cap.Status == PASS:
                    consec_2x = 0
                elif cap.Status == FAIL_2X:
                    consec_2x += 1
                    if consec_2x >= _TWO_X_RUN:
                        entrance = cap.StartUTC        # forward from the 5th
                        consec_2x = 0
                elif cap.Status == FAIL_4X:
                    if i == 0:
                        # (1) no preceding capsule — do NOT guess the entrance.
                        flags.append(OOCFlag(
                            analyzer, "4x-no-preceding-capsule",
                            f"4x_Fail at {cap.StartUTC.isoformat()} is the first "
                            f"validation for this analyzer; entrance (start of the "
                            f"preceding capsule) is undefined — no window opened."))
                    else:
                        entrance = caps[i - 1].StartUTC   # start of the preceding
                    # a 4x does not increment the 2x count
            else:
                # In OOC — the next Pass closes the window.
                if cap.Status == PASS:
                    out.append((entrance, cap.EndUTC))
                    entrance = None
                    consec_2x = 0
                # fails while OOC: stay OOC, no new entrance

        if entrance is not None:
            # (2) open tail — no closing Pass in the stream.
            if open_tail_end is not None and open_tail_end > entrance:
                out.append((entrance, open_tail_end))
                tail = f"closed CONSERVATIVELY at {open_tail_end.isoformat()}"
            else:
                tail = "no window emitted (open_tail_end not supplied)"
            flags.append(OOCFlag(
                analyzer, "open-tail-no-closing-pass",
                f"OOC window opened at {entrance.isoformat()} has no closing Pass "
                f"in the stream; {tail}."))

        if out:
            windows[analyzer] = out

    return windows, flags
