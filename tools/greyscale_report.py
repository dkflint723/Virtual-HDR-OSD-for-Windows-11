"""Read the meter log and say how well each run tracked PQ.

Usage:

    python tools/greyscale_report.py            # every run in the default log
    python tools/greyscale_report.py <path>     # a log kept somewhere else

A measurement run is only worth anything next to what it asked for, so this pairs each
greyscale patch with its target from the ``start`` record and prints the miss. Two runs
either side of an Apply is the test that matters: the second should track far closer than
the first, and a third should barely differ from the second. A second run that is *worse*
than the first is the signature of a correction being applied twice.

Nothing here reads the display or the profile. It only reads the log, so it can be run
long after the fact and on a different machine.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sdr_hdr_profile_creator.measure import D65_XY  # noqa: E402

DEFAULT_LOG = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    / "Virtual_HDR_OSD_for_Windows"
    / "meter_log.jsonl"
)


def runs(records):
    """Split the log into runs. A run is a ``start`` and everything up to the next one."""
    current = None
    for record in records:
        if record.get("event") == "start":
            if current is not None:
                yield current
            current = {"start": record, "readings": {}, "outcome": None}
        elif current is None:
            continue
        elif record.get("event") == "reading":
            current["readings"][record.get("patch", "")] = record
        elif record.get("event") in ("accepted", "refused", "cancelled", "response",
                                     "response-unavailable"):
            current.setdefault("events", []).append(record)
            if record["event"] in ("accepted", "refused", "cancelled"):
                current["outcome"] = record["event"]
    if current is not None:
        yield current


def describe(index, run):
    start = run["start"]
    plan = start.get("plan") or {}
    at = start.get("at", "?")[:19].replace("T", " ")
    print(f"\nRun {index}  {at}  {start.get('display', '?')}  ({start.get('instrument', '?')})")

    if not plan:
        print("  No plan recorded -- this run predates the log carrying its targets.")
        return

    grey = sorted(key for key in plan if key.startswith("grey-"))
    paired = [
        (plan[key], run["readings"][key])
        for key in grey
        if key in run["readings"] and plan[key] > 0
    ]
    if not paired:
        print(f"  No greyscale readings ({run['outcome'] or 'run did not finish'}).")
        return

    errors = [abs(reading["Y"] - target) / target for target, reading in paired]
    drifts = [math.dist((reading["x"], reading["y"]), D65_XY) for _t, reading in paired]
    worst = max(range(len(errors)), key=lambda i: errors[i])

    print(f"  {len(paired)} of {len(grey)} ramp points read"
          f"   outcome: {run['outcome'] or 'incomplete'}")
    print(f"  luminance error   median {_pct(_median(errors))}   worst {_pct(max(errors))}"
          f"  (at {paired[worst][0]:.4g} nits, read {paired[worst][1]['Y']:.4g})")
    print(f"  drift from D65    median {_median(drifts):.4f}   worst {max(drifts):.4f}")

    # The bottom of the range is where a display is usually worst and where the ramp is
    # deliberately densest, so it is reported on its own rather than hidden in a median.
    low = [error for (target, _r), error in zip(paired, errors) if target <= 10.0]
    if low:
        print(f"  below 10 nits     {len(low)} points, median {_pct(_median(low))}"
              f"   worst {_pct(max(low))}")

    for record in run.get("events", []):
        if record["event"] == "response":
            weights = ", ".join(f"{value:.4f}" for value in record.get("weights", ()))
            print(f"  stored a correction from {record.get('points', '?')} points"
                  f"   white split ({weights})")
        elif record["event"] == "response-unavailable":
            print(f"  NO correction stored (kept previous: "
                  f"{record.get('kept_previous')}) -- {record.get('ramp_points')} ramp points")


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _pct(value):
    return f"{value * 100:6.2f}%"


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_LOG
    if not path.is_file():
        print(f"No meter log at {path}")
        return 1

    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    found = list(runs(records))
    if not found:
        print(f"{path} has no measurement runs in it yet.")
        return 0

    print(f"{path}\n{len(found)} run(s)")
    for index, run in enumerate(found, start=1):
        describe(index, run)

    print("\nWhat to look for: the run after an Apply should track far closer than the one")
    print("before it, and a third should barely differ from the second. A second run that")
    print("is worse than the first means a correction is landing twice.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
