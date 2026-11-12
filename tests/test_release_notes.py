"""tools/release_notes.py: a release's notes are its changelog section, with the repository's links made absolute at its tag."""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_version import REPOSITORY, VERSION  # noqa: E402
from tools import release_notes  # noqa: E402
from tools.release_notes import NotesError  # noqa: E402

TOOL = ROOT / "tools" / "release_notes.py"
CHANGELOG = """# Changelog

## Unreleased

Not out yet.

## 1.2.0 — 2026-12-01

What 1.2.0 brings, in [setup](docs/SETUP.md#updating-the-lab).

### Fixes

- A fix — with an em dash.

## 1.1.0 — 2026-11-20

The one before.
"""


def blob(path, tag="v1.2.0"):
    return f"https://github.com/{REPOSITORY}/blob/{tag}/{path}"


class SectionTests(unittest.TestCase):
    def test_the_section_runs_to_the_next_release_and_keeps_its_own_headings(self):
        self.assertEqual(
            release_notes.section(CHANGELOG, "1.2.0"),
            "What 1.2.0 brings, in [setup](docs/SETUP.md#updating-the-lab).\n\n### Fixes\n\n- A fix — with an em dash.\n",
        )
        self.assertEqual(release_notes.section(CHANGELOG, "1.1.0"), "The one before.\n")

    def test_a_version_with_no_section_is_refused(self):
        with self.assertRaisesRegex(NotesError, r"no section for 1\.3\.0.*## 1\.3\.0 — YYYY-MM-DD"):
            release_notes.section(CHANGELOG, "1.3.0")

    def test_a_heading_with_no_date_is_refused(self):
        text = CHANGELOG.replace("## Unreleased", "## 1.3.0")
        with self.assertRaisesRegex(NotesError, "has no date"):
            release_notes.section(text, "1.3.0")

    def test_a_date_that_is_not_a_day_is_refused(self):
        for date in ("2026-13-01", "1 Dec 2026", "2026-12-1"):
            with self.subTest(date=date), self.assertRaisesRegex(NotesError, "for its date"):
                release_notes.section(CHANGELOG.replace("2026-12-01", date), "1.2.0")

    def test_an_empty_section_is_refused(self):
        text = "## 1.2.0 — 2026-12-01\n\n\n## 1.1.0 — 2026-11-20\n\nThe one before.\n"
        with self.assertRaisesRegex(NotesError, "empty"):
            release_notes.section(text, "1.2.0")

    def test_two_sections_for_one_version_are_refused(self):
        with self.assertRaisesRegex(NotesError, "2 sections"):
            release_notes.section(CHANGELOG.replace("## 1.1.0", "## 1.2.0"), "1.2.0")


class LinkTests(unittest.TestCase):
    def link(self, markdown):
        return release_notes.absolute_links(markdown, REPOSITORY, "v1.2.0")

    def test_a_file_in_the_repository_is_linked_at_the_tag_with_its_anchor(self):
        self.assertEqual(self.link("[a](docs/SETUP.md#removing-the-lab)"), f"[a]({blob('docs/SETUP.md#removing-the-lab')})")
        self.assertEqual(self.link("[a](./MODEL_CAPABILITIES.md)"), f"[a]({blob('MODEL_CAPABILITIES.md')})")
        self.assertEqual(self.link("[a](/docs/API.md)"), f"[a]({blob('docs/API.md')})")

    def test_a_link_to_one_of_the_changelogs_own_headings_points_at_the_changelog(self):
        self.assertEqual(self.link("[fixes](#fixes)"), f"[fixes]({blob('CHANGELOG.md#fixes')})")

    def test_an_image_points_at_the_files_bytes_and_a_title_is_kept(self):
        self.assertEqual(
            self.link('![The Scenes page](docs/images/scene.png "Scenes")'),
            f'![The Scenes page](https://github.com/{REPOSITORY}/raw/v1.2.0/docs/images/scene.png "Scenes")',
        )

    def test_links_that_already_lead_somewhere_are_left_alone(self):
        text = "[a](https://example.com/x) [b](mailto:someone@example.com) `code` [not a link] (docs/SETUP.md)"
        self.assertEqual(self.link(text), text)

    def test_a_link_out_of_the_repository_is_refused(self):
        with self.assertRaisesRegex(NotesError, "out of the repository"):
            self.link("[a](../elsewhere/README.md)")


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.changelog = Path(self.temp.name) / "CHANGELOG.md"
        self.changelog.write_text(CHANGELOG, encoding="utf-8")

    def run_tool(self, *args):
        return subprocess.run(
            [sys.executable, str(TOOL), "--changelog", str(self.changelog), *args], capture_output=True, timeout=60, cwd=ROOT
        )

    def test_a_tag_must_be_v_and_the_version(self):
        self.assertIn(blob("docs/SETUP.md#updating-the-lab"), release_notes.notes("1.2.0", changelog=self.changelog))
        for tag in ("v1.2.1", "1.2.0", "V1.2.0", "v1.2.0-rc1"):
            with self.subTest(tag=tag), self.assertRaisesRegex(NotesError, "update.bat refuses"):
                release_notes.notes("1.2.0", changelog=self.changelog, tag=tag)

    def test_a_version_must_be_three_plain_numbers(self):
        for version in ("Unreleased", "1.2", "1.2.0-rc1"):
            with self.subTest(version=version), self.assertRaisesRegex(NotesError, "three plain numbers"):
                release_notes.notes(version, changelog=self.changelog)

    def test_the_command_writes_the_notes_in_utf8_and_a_refusal_writes_nothing(self):
        output = Path(self.temp.name) / "notes.md"
        done = self.run_tool("1.2.0", "--tag", "v1.2.0", "--output", str(output))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            output.read_bytes(),
            f"What 1.2.0 brings, in [setup]({blob('docs/SETUP.md#updating-the-lab')}).\n\n### Fixes\n\n"
            "- A fix — with an em dash.\n".encode("utf-8"),
        )
        self.assertEqual(self.run_tool("1.2.0").stdout, output.read_bytes())

        output.unlink()
        for args in (("1.2.0", "--tag", "v1.2.1"), ("1.3.0",)):
            with self.subTest(args=args):
                done = self.run_tool(*args, "--output", str(output))
                self.assertEqual(done.returncode, 1)
                self.assertFalse(output.exists())
                self.assertTrue(done.stderr.decode("utf-8").strip())
        self.assertIn("## 1.3.0 — YYYY-MM-DD", self.run_tool("1.3.0").stderr.decode("utf-8"))


class TheRealChangelogTests(unittest.TestCase):
    def test_the_version_in_app_version_has_a_dated_section_so_its_tag_can_be_released(self):
        self.assertTrue(release_notes.notes(VERSION).strip())

    def test_no_released_section_keeps_a_link_that_would_lead_nowhere_on_its_release_page(self):
        # tools/check_repo.py already checks that the changelog's links lead to files in the repository.
        text = release_notes.CHANGELOG.read_text(encoding="utf-8")
        versions = re.findall(r"^## (\d+\.\d+\.\d+) — ", text, flags=re.MULTILINE)
        self.assertIn(VERSION, versions)
        for version in versions:
            with self.subTest(version=version):
                self.assertNotRegex(release_notes.notes(version), r"\]\((?!https://|mailto:)")


if __name__ == "__main__":
    unittest.main()
