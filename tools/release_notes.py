"""Write a release's notes: that version's section of CHANGELOG.md, with the repository's own links made absolute at its tag.

    .venv\\Scripts\\python.exe tools\\release_notes.py                  the notes for the version in app_version.py
    .venv\\Scripts\\python.exe tools\\release_notes.py 0.2.0            the notes for another version
    .venv\\Scripts\\python.exe tools\\release_notes.py --tag v0.3.0 --output notes.md

A release's notes are shown on GitHub's release page, where a link such as [setup](docs/SETUP.md) leads nowhere, so each one
is pointed at the file as it is at the tag. The release workflow (.github/workflows/release.yml) passes --tag, which also
refuses a tag that is not "v" and the version, because update.bat refuses such a release. Exit status 1, with the reason, when
the version has no section in the changelog, its heading has no date, or its section is empty.
"""

import argparse
import datetime
import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app_version  # noqa: E402

CHANGELOG = ROOT / "CHANGELOG.md"
HEADING = re.compile(r"## (?P<version>\S+)(?:\s+—\s+(?P<date>.*?))?\s*")
# An inline link or image whose target has no spaces, with an optional title. The changelog uses no other kind.
LINK = re.compile(r"(?P<label>!?\[[^\]]*\])\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


class NotesError(Exception):
    """The notes cannot be written; the message says why and what to change."""


def check_version(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise NotesError(f"{version!r} is not a version such as 0.3.0: Check for updates reads only three plain numbers.")


def check_tag(tag, version):
    if tag != f"v{version}":
        raise NotesError(
            f"The tag {tag} is not v{version}, the version in app_version.py. update.bat refuses a release whose tag and "
            "version differ, so tag the commit that sets this version."
        )


def section(text, version):
    """The text under the version's heading, up to the next heading of the same level, without its blank edges."""
    lines = text.splitlines()
    found = [index for index, line in enumerate(lines) if (match := HEADING.fullmatch(line)) and match["version"] == version]
    if not found:
        raise NotesError(f'CHANGELOG.md has no section for {version}. Give it the heading "## {version} — YYYY-MM-DD".')
    if len(found) > 1:
        raise NotesError(f"CHANGELOG.md has {len(found)} sections for {version}; keep one.")
    start = found[0]
    date = HEADING.fullmatch(lines[start])["date"]
    if not date:
        raise NotesError(f'The heading for {version} in CHANGELOG.md has no date. Write it as "## {version} — YYYY-MM-DD".')
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError
        datetime.date.fromisoformat(date)
    except ValueError:
        raise NotesError(
            f"The heading for {version} in CHANGELOG.md has {date!r} for its date, not one such as 2026-11-20."
        ) from None
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    body = "\n".join(lines[start + 1 : end]).strip("\n")
    if not body.strip():
        raise NotesError(f"The section for {version} in CHANGELOG.md is empty.")
    return body + "\n"


def absolute_links(text, repository, tag):
    """The text with every link into the repository pointed at the file as it is at the tag. A link to a heading of the
    changelog itself points at CHANGELOG.md, and an image at the file's raw bytes so that it still shows."""

    def point(match):
        target = match["target"]
        if SCHEME.match(target):
            return match[0]
        path, hash_, anchor = target.partition("#")
        path = posixpath.normpath(path.lstrip("/")) if path else "CHANGELOG.md"
        if path == ".." or path.startswith("../"):
            raise NotesError(f"The link to {target} leads out of the repository.")
        kind = "raw" if match["label"].startswith("!") else "blob"
        url = f"https://github.com/{repository}/{kind}/{tag}/{path}{hash_}{anchor}"
        return f"{match['label']}({url}{match['title'] or ''})"

    return LINK.sub(point, text)


def notes(version, *, changelog=CHANGELOG, repository=app_version.REPOSITORY, tag=None):
    check_version(version)
    tag = tag or f"v{version}"
    check_tag(tag, version)
    return absolute_links(section(Path(changelog).read_text(encoding="utf-8"), version), repository, tag)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", nargs="?", default=app_version.VERSION, help="default: the version in app_version.py")
    parser.add_argument("--tag", help="the tag being released; refused unless it is v and the version")
    parser.add_argument("--output", type=Path, help="write the notes to this file (UTF-8) instead of standard output")
    parser.add_argument("--changelog", type=Path, default=CHANGELOG, help=argparse.SUPPRESS)
    parser.add_argument("--repository", default=app_version.REPOSITORY, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        text = notes(args.version, changelog=args.changelog, repository=args.repository, tag=args.tag)
    except (NotesError, OSError) as exc:
        # A workflow's log reads UTF-8, and the advice holds an em dash; a console takes UTF-8 too.
        sys.stderr.reconfigure(encoding="utf-8")
        print(exc, file=sys.stderr)
        return 1
    # Written whole after every check, so a refusal leaves no file behind, and in UTF-8 whatever the console's code page.
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
