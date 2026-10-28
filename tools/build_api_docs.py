"""Write openapi.json and the reference part of docs/API.md from the route table in api_spec.py.

    .venv\\Scripts\\python.exe tools\\build_api_docs.py            write both files
    .venv\\Scripts\\python.exe tools\\build_api_docs.py --check    change nothing; say which file is out of date; exit status 1

docs/API.md keeps its own text above and below the reference: only what lies between the two marker lines is replaced.
tests/test_api_docs.py runs the same comparison, so a route table that was changed without running this fails the suite.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api_spec  # noqa: E402

OPENAPI = ROOT / "openapi.json"
GUIDE = ROOT / "docs" / "API.md"
BEGIN = "<!-- BEGIN GENERATED REFERENCE: edit api_spec.py, then run tools/build_api_docs.py -->"
END = "<!-- END GENERATED REFERENCE -->"


def guide_text(current):
    """The text of docs/API.md with its reference replaced by the one api_spec.py describes."""
    start, stop = current.find(BEGIN), current.find(END)
    if start < 0 or stop < start:
        raise ValueError(f"{GUIDE.name} has lost its BEGIN and END GENERATED REFERENCE lines.")
    return current[:start] + BEGIN + "\n\n" + api_spec.render_reference() + "\n" + current[stop:]


def expected():
    """Each generated file and the text it should hold."""
    return {OPENAPI: api_spec.openapi_text(), GUIDE: guide_text(GUIDE.read_text(encoding="utf-8"))}


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report files that are out of date instead of writing them")
    args = parser.parse_args(argv)
    wanted = expected()
    behind = [path for path, text in wanted.items() if current_text(path) != text]
    if args.check:
        for path in behind:
            print(f"{label(path)} is out of date; run tools/build_api_docs.py")
        if not behind:
            print("openapi.json and docs/API.md match the route table.")
        return 1 if behind else 0
    for path in behind:
        path.write_text(wanted[path], encoding="utf-8", newline="\n")
        print(f"wrote {label(path)}")
    if not behind:
        print("openapi.json and docs/API.md were already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
