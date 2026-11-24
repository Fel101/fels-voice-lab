"""Write openapi.json, the reference part of docs/API.md and the suite API's snapshot from the route table in api_spec.py.

    .venv\\Scripts\\python.exe tools\\build_api_docs.py            write the files
    .venv\\Scripts\\python.exe tools\\build_api_docs.py --check    change nothing; say which file is out of date; exit status 1

docs/API.md keeps its own text above and below the reference: only what lies between the two marker lines is replaced.

tests/suite_api_<N>.json holds the routes of the Lab's suite API number N (app_version.SUITE_API), which the other apps of
Fel's Studio rely on. A new route or field is added to it. A change that would break a program written for it (a field
renamed, removed, retyped or newly required) is refused: keep the old shape, or raise SUITE_API, which writes a new file
beside the old one.

tests/test_api_docs.py and tests/test_suite_api.py run the same comparisons, so a route table that was changed without running
this fails the suite.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api_spec  # noqa: E402

OPENAPI = ROOT / "openapi.json"
GUIDE = ROOT / "docs" / "API.md"
SNAPSHOTS = ROOT / "tests"  # where tests/suite_api_<N>.json are kept
BEGIN = "<!-- BEGIN GENERATED REFERENCE: edit api_spec.py, then run tools/build_api_docs.py -->"
END = "<!-- END GENERATED REFERENCE -->"


def snapshot_path(number=None):
    """The snapshot of suite API `number`, the current one by default."""
    return SNAPSHOTS / f"suite_api_{api_spec.SUITE_API if number is None else number}.json"


def snapshot_text():
    return json.dumps(api_spec.suite_snapshot(), indent=2, ensure_ascii=False) + "\n"


def guide_text(current):
    """The text of docs/API.md with its reference replaced by the one api_spec.py describes."""
    start, stop = current.find(BEGIN), current.find(END)
    if start < 0 or stop < start:
        raise ValueError(f"{GUIDE.name} has lost its BEGIN and END GENERATED REFERENCE lines.")
    return current[:start] + BEGIN + "\n\n" + api_spec.render_reference() + "\n" + current[stop:]


def expected():
    """Each generated file and the text it should hold."""
    return {OPENAPI: api_spec.openapi_text(), GUIDE: guide_text(GUIDE.read_text(encoding="utf-8")),
            snapshot_path(): snapshot_text()}


def current_text(path):
    return path.read_text(encoding="utf-8") if path.is_file() else None


def label(path):
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def stale():
    """The generated files whose text is not what the route table gives."""
    return [path for path, text in expected().items() if current_text(path) != text]


def breaks_suite(path, text):
    """The sentences saying how `text` would break the snapshot kept at `path`; empty when it only adds, or is new."""
    kept = current_text(path)
    if kept is None:
        return []
    return api_spec.suite_changes(json.loads(kept), json.loads(text))[0]


def refusal(path, breaking):
    number = api_spec.SUITE_API
    return (f"{label(path)} keeps suite API {number}, which the other apps of Fel's Studio rely on, and the route table "
            f"would break it:\n" + "".join(f"  - {line}\n" for line in breaking)
            + f"Keep the old names and types, or raise SUITE_API in app_version.py to {number + 1}, with a CHANGELOG "
            f"line, and run this again: it writes {label(snapshot_path(number + 1))} beside the old file.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report files that are out of date instead of writing them")
    args = parser.parse_args(argv)
    wanted = expected()
    behind = [path for path, text in wanted.items() if current_text(path) != text]
    snapshot = snapshot_path()
    breaking = breaks_suite(snapshot, wanted[snapshot]) if snapshot in behind else []
    if args.check:
        for path in behind:
            print(refusal(path, breaking) if path == snapshot and breaking else
                  f"{label(path)} is out of date; run tools/build_api_docs.py")
        if not behind:
            print("openapi.json, docs/API.md and the suite API's snapshot match the route table.")
        return 1 if behind else 0
    for path in behind:
        if path == snapshot and breaking:
            print(refusal(path, breaking))
            continue
        path.write_text(wanted[path], encoding="utf-8", newline="\n")
        print(f"wrote {label(path)}")
    if not behind:
        print("openapi.json, docs/API.md and the suite API's snapshot were already up to date.")
    return 1 if breaking else 0


if __name__ == "__main__":
    sys.exit(main())
