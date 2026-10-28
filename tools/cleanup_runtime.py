"""List, and with --apply remove, the stale runtime folders the service leaves in out/.

    .venv\\Scripts\\python.exe tools\\cleanup_runtime.py            list what would go, change nothing
    .venv\\Scripts\\python.exe tools\\cleanup_runtime.py --apply    remove it

Stopped workers' empty temporary-audio folders, desktop sessions beyond the newest ten and service logs beyond the
newest thirty runs, each only once it is old enough (housekeeping.py has the exact rules). The service runs the same
sweep when it starts, unless started with --no-cleanup.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import housekeeping  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="remove what is listed")
    parser.add_argument("--out", type=Path, default=ROOT / "out", help="the out folder to sweep (default: this project's)")
    args = parser.parse_args(argv)
    items = housekeeping.plan(args.out)
    totals = housekeeping.summarize(items)
    for kind, label in (("worker", "worker folders"), ("desktop", "desktop sessions"), ("log", "service log files")):
        row = totals["by_kind"][kind]
        print(f"{label}: {row['count']} ({row['bytes'] / 1024:.0f} KB)")
    print(f"total: {totals['count']} items, {totals['bytes'] / 1024:.0f} KB")
    if not args.apply:
        print("Nothing was removed. Run again with --apply to remove these.")
        return 0
    result = housekeeping.apply(items, args.out)
    print(f"removed {result['removed']} items ({result['bytes'] / 1024:.0f} KB)")
    for error in result["errors"]:
        print(f"could not remove {error}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
