"""The repository check, run on synthetic folders: no personal media, real secrets or real names are used."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.check_repo import audit, main

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


class FolderTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.write("README.md", "# Synthetic fixture\n")
        self.write("app.py", "print('fixture')\n")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, content="fixture"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def findings(self, deny=()):
        return audit(self.root, deny)[1]

    def rules_for(self, name):
        return {item["rule"] for item in self.findings() if item["path"] == name}

    def run_main(self, *arguments):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--root", str(self.root), *arguments])
        return code, out.getvalue()


class FolderChecks(FolderTestCase):
    def test_a_clean_folder_has_no_findings(self):
        self.assertEqual(audit(self.root), (["README.md", "app.py"], []))
        code, out = self.run_main()
        self.assertEqual((code, json.loads(out)), (0, {"files": 2, "findings": []}))

    def test_recordings_models_logs_programs_and_local_settings_are_found(self):
        for name in ("clip.wav", "checkpoint.gguf", "service.log", "tool.exe", "runtime.local.json", ".env", ".env.local"):
            with self.subTest(name=name):
                path = self.write(name)
                self.assertEqual(self.rules_for(name), {"private_or_binary_file"})
                path.unlink()
        self.write(".env.example", "KEY=\n")
        self.assertEqual(self.findings(), [])

    def test_a_file_in_a_private_runtime_folder_is_found_whatever_it_is(self):
        for folder in ("data", "out", "reference", "hf-cache", "dist", ".venv-breeze"):
            with self.subTest(folder=folder):
                name = f"{folder}/notes.txt"
                self.write(name)
                self.assertEqual(self.rules_for(name), {"private_folder"})
                (self.root / name).unlink()

    def test_images_are_allowed_only_under_docs_and_assets(self):
        for name in ("docs/shot.png", "docs/guide/shot.jpg", "assets/logo.png"):
            self.write(name)
        self.assertEqual(self.findings(), [])
        for name in ("shot.png", "tests/shot.gif"):
            with self.subTest(name=name):
                self.write(name)
                self.assertEqual(self.rules_for(name), {"image_outside_docs"})

    def test_a_file_over_two_megabytes_is_found(self):
        self.write("big.txt", b"x" * (2 * 1024 * 1024 + 1))
        self.assertEqual(self.rules_for("big.txt"), {"large_file"})
        self.write("big.txt", b"x" * (2 * 1024 * 1024))
        self.assertEqual(self.findings(), [])

    def test_a_linked_file_is_found(self):
        try:
            os.symlink(self.root / "README.md", self.root / "link.md")
        except (OSError, NotImplementedError):
            self.skipTest("this account cannot create file links")
        self.assertEqual(self.rules_for("link.md"), {"linked_file"})

    def test_an_obvious_secret_is_found_without_printing_its_value(self):
        secret = "hf_" + "A" * 35
        self.write("app.py", "token = '" + secret + "'\n")
        self.assertEqual(self.findings(), [{"path": "app.py", "rule": "provider_token", "line": 1}])
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertNotIn(secret, out)

    def test_a_private_key_header_is_found(self):
        self.write("key.txt", "-----BEGIN " + "RSA PRIVATE KEY-----\n")
        self.assertEqual(self.rules_for("key.txt"), {"private_key"})

    def test_a_personal_home_folder_is_found_but_placeholders_are_not(self):
        backslash = chr(92)
        home = "C:" + backslash + "Users" + backslash
        self.write("README.md", home + "private-person" + backslash + "clip.txt\n")
        self.assertEqual(self.findings(), [{"path": "README.md", "rule": "windows_user_home", "line": 1}])
        placeholders = [home + "Public" + backslash + "x", home + "<you>" + backslash + "x", home + "YOUR-NAME" + backslash + "x"]
        self.write("README.md", "\n".join(placeholders) + "\n")
        self.assertEqual(self.findings(), [])

    def test_a_denied_word_is_found_in_any_text_file_and_never_printed(self):
        self.write("README.md", "Written by Hidden-Name.\nAnd again hidden-name here.\n")
        self.assertEqual(self.findings(), [])
        with tempfile.TemporaryDirectory() as other:
            deny_file = Path(other) / "deny.txt"
            deny_file.write_text("# names\n\nHidden-Name\n", "utf-8")
            code, out = self.run_main("--deny-file", str(deny_file))
        self.assertEqual(code, 1)
        self.assertEqual([(item["path"], item["line"], item["entry"]) for item in json.loads(out)["findings"]],
                         [("README.md", 1, 0), ("README.md", 2, 0)])
        self.assertNotIn("hidden-name", out.lower())

    def test_a_relative_link_to_a_page_that_is_not_there_is_found(self):
        self.write("docs/GUIDE.md", "# Guide\n")
        self.write("README.md",
                   "See [the guide](docs/GUIDE.md#top), [the web](https://example.org/x), [here](#local),\n"
                   "[a missing page](docs/MISSING.md) and an image ![shot](docs/shot.png).\n"
                   "```\n[an example](docs/EXAMPLE.md)\n```\n"
                   "A folder link: [docs](docs/).\n")
        self.assertEqual(self.findings(), [{"path": "README.md", "rule": "dead_link", "line": 2, "target": "docs/MISSING.md"}])
        self.write("docs/MISSING.md", "# Found\n")
        self.assertEqual(self.findings(), [])

    def test_a_link_from_a_nested_page_is_resolved_from_that_page(self):
        self.write("docs/A.md", "[b](B.md) [up](../README.md) [gone](C.md)\n")
        self.write("docs/B.md", "# B\n")
        self.assertEqual([item["target"] for item in self.findings()], ["C.md"])


@unittest.skipUnless(GIT, "Git is not installed")
class GitViewChecks(FolderTestCase):
    def git(self, *arguments):
        subprocess.run(["git", "-C", str(self.root), *arguments], check=True, capture_output=True)

    def test_files_git_ignores_are_not_checked_but_a_forced_one_is(self):
        self.git("init", "-q")
        self.write(".gitignore", "out/\n.venv/\n")
        self.write("out/clip.wav")
        self.write(".venv/Scripts/tool.exe")
        self.assertEqual(audit(self.root), ([".gitignore", "README.md", "app.py"], []))
        self.git("add", "-f", "out/clip.wav")
        self.assertEqual(self.rules_for("out/clip.wav"), {"private_folder", "private_or_binary_file"})

    def test_a_tracked_file_deleted_from_the_folder_is_not_published(self):
        self.git("init", "-q")
        self.git("add", "README.md")
        (self.root / "README.md").unlink()
        self.assertEqual(audit(self.root), (["app.py"], []))


@unittest.skipUnless(GIT and (ROOT / ".git").exists(), "this folder is not a Git checkout")
class RepositoryChecks(unittest.TestCase):
    def test_the_repository_itself_has_no_findings(self):
        names, findings = audit(ROOT)
        self.assertGreater(len(names), 100)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
