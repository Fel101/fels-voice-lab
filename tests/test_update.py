"""Check for updates (updates.py, and the service's /api/updates) and update.bat (tools/update.py). GitHub, the downloads,
the processes and pip are stand-ins; the Git tests make their own repositories in a temporary folder."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import updates  # noqa: E402
from app_version import REPOSITORY, VERSION  # noqa: E402
from tools import install_drama3, install_kokoro, install_voxcpm, update  # noqa: E402

GIT = shutil.which("git")
# The version of the stand-in Lab folders that the update tests update to 0.2.0, whatever version this copy is itself.
CURRENT = "0.1.0"


def release(tag="v0.2.0", body="Fixes the RTX 50 setup.\nAdds Check for updates."):
    return {
        "tag_name": tag,
        "name": f"Fel's Voice Lab {tag[1:]}",
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
        "published_at": "2026-11-20T10:00:00Z",
        "body": body,
    }


def answers(found):
    """A stand-in for updates.fetch_json that gives GitHub's answer, or raises it when it is an exception."""

    def fetch(url):
        if isinstance(found, BaseException):
            raise found
        return found

    return fetch


def http_error(code, reason="Not Found"):
    return urllib.error.HTTPError(f"{updates.API}/repos/{REPOSITORY}/releases/latest", code, reason, {}, None)


class VersionCheckTests(unittest.TestCase):
    def test_versions_compare_as_numbers(self):
        self.assertEqual(updates.parse_version("v0.2.0"), (0, 2, 0))
        self.assertEqual(updates.parse_version("0.10"), (0, 10, 0))
        self.assertGreater(updates.parse_version("v0.10.0"), updates.parse_version("0.9.9"))
        for odd in (None, "", "latest", "v1.0.0-beta.1", "1..2"):
            self.assertIsNone(updates.parse_version(odd), odd)

    def test_a_newer_release_is_named_with_its_page_and_notes(self):
        result = updates.check(current="0.1.0", fetch=answers(release()))
        self.assertTrue(result["newer"])
        self.assertEqual(result["latest"], "0.2.0")
        self.assertEqual(result["message"], "Version 0.2.0 is available. This is version 0.1.0.")
        self.assertEqual(result["release"]["url"], f"https://github.com/{REPOSITORY}/releases/tag/v0.2.0")
        self.assertIn("Adds Check for updates.", result["release"]["notes"])

    def test_the_same_or_an_older_release_is_not_offered(self):
        same = updates.check(current="0.2.0", fetch=answers(release()))
        self.assertEqual((same["newer"], same["message"]), (False, "This is the newest version, 0.2.0."))
        older = updates.check(current="0.3.0", fetch=answers(release()))
        self.assertFalse(older["newer"])
        self.assertIn("newer than the newest release, 0.2.0", older["message"])

    def test_no_release_yet_is_an_answer_and_not_an_error(self):
        result = updates.check(current="0.1.0", fetch=answers(http_error(404)))
        self.assertEqual((result["latest"], result["newer"], result["release"]), (None, False, None))
        self.assertIn("No release of the Lab has been published on GitHub yet", result["message"])

    def test_refusals_and_odd_answers_are_explained(self):
        for found, words in (
            (http_error(403, "Forbidden"), "60 times an hour"),
            (http_error(500, "Server Error"), "500"),
            ({"tag_name": "nightly"}, "not a version number"),
            ([], "named no release"),
        ):
            with self.subTest(words=words), self.assertRaisesRegex(updates.UpdateError, words):
                updates.check(current="0.1.0", fetch=answers(found))

    def test_a_network_failure_says_github_could_not_be_reached(self):
        opener = types.SimpleNamespace(open=mock.Mock(side_effect=urllib.error.URLError("getaddrinfo failed")))
        with self.assertRaisesRegex(updates.UpdateError, r"GitHub could not be reached \(getaddrinfo failed\)"):
            updates.fetch_json(f"{updates.API}/repos/{REPOSITORY}/releases/latest", opener=opener)

    def test_only_github_over_https_is_ever_asked_and_redirects_cannot_leave_it(self):
        for url in ("http://api.github.com/x", "https://example.com/x", "https://github.com.example.com/x", "file:///C:/x"):
            with self.subTest(url=url), self.assertRaises(updates.UpdateError):
                updates.check_url(url)
        self.assertEqual(updates.source_zip_url("v0.2.0"), f"https://codeload.github.com/{REPOSITORY}/zip/refs/tags/v0.2.0")
        handler = updates._GitHubOnly()
        request = urllib.request.Request(f"https://github.com/{REPOSITORY}/archive/refs/tags/v0.2.0.zip")
        with self.assertRaises(updates.UpdateError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://example.com/v0.2.0.zip")
        followed = handler.redirect_request(request, None, 302, "Found", {}, updates.source_zip_url("v0.2.0"))
        self.assertEqual(followed.full_url, updates.source_zip_url("v0.2.0"))


class ServiceCheckTests(unittest.TestCase):
    def checks(self, folder, run, clock):
        return updates.UpdateChecks(folder, run=run, clock=lambda: clock[0])

    def test_the_last_answer_is_kept_and_a_check_is_due_once_a_day_when_switched_on(self):
        with tempfile.TemporaryDirectory() as temp:
            clock = [1_000_000.0]
            checks = self.checks(temp, lambda: updates.check(current=VERSION, fetch=answers(release("v99.0.0"))), clock)
            self.assertEqual(checks.state(True)["due"], True)
            self.assertEqual(checks.state(False)["due"], False)
            result = checks.check_now()
            self.assertTrue(result["newer"])
            reopened = self.checks(temp, None, clock)
            self.assertEqual(reopened.state(True)["last"]["latest"], "99.0.0")
            self.assertFalse(reopened.state(True)["due"])
            clock[0] += updates.DAY
            self.assertTrue(reopened.state(True)["due"])

    def test_a_failed_check_is_kept_and_tried_again_after_an_hour(self):
        with tempfile.TemporaryDirectory() as temp:
            clock = [1_000_000.0]
            checks = self.checks(
                temp, mock.Mock(side_effect=updates.UpdateError("GitHub could not be reached (offline).")), clock
            )
            with self.assertRaises(updates.UpdateError):
                checks.check_now()
            self.assertIn("offline", checks.state(True)["last"]["error"])
            self.assertFalse(checks.state(True)["due"])
            clock[0] += updates.HOUR
            self.assertTrue(checks.state(True)["due"])

    def test_an_answer_about_another_version_of_the_lab_is_not_shown(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "last-check.json").write_text(json.dumps({"current": "0.0.1", "checked": 1, "newer": True}), "utf-8")
            self.assertIsNone(updates.UpdateChecks(temp).last())
            Path(temp, "last-check.json").write_text("{", "utf-8")
            self.assertIsNone(updates.UpdateChecks(temp).state(True)["last"])

    def test_the_service_answers_from_the_last_check_and_asks_github_only_when_told(self):
        import serve
        from cast_store import CastStore

        class Checks:
            calls = 0

            def state(self, check_on_open):
                return {"current": VERSION, "check_on_open": check_on_open, "due": False, "last": None}

            def check_now(self):
                Checks.calls += 1
                if Checks.calls > 1:
                    raise updates.UpdateError("GitHub could not be reached (offline).")
                return {"current": VERSION, "latest": "9.0.0", "newer": True}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
            server.RequestHandlerClass = serve.make_handler(
                types.SimpleNamespace(entries={}), server.server_address[1], CastStore(Path(temp)), update_checks=Checks()
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base + "/api/updates", timeout=10) as response:
                self.assertEqual(json.load(response), {"current": VERSION, "check_on_open": False, "due": False, "last": None})
            self.assertEqual(Checks.calls, 0)
            post = urllib.request.Request(base + "/api/updates/check", data=b"{}", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(post, timeout=10) as response:
                self.assertEqual(json.load(response)["latest"], "9.0.0")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(post, timeout=10)
            self.assertEqual(caught.exception.code, 502)
            self.assertIn("offline", json.load(caught.exception)["error"])
            preference = urllib.request.Request(
                base + "/api/preferences", data=b'{"check_updates": true}', headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(preference, timeout=10) as response:
                self.assertTrue(json.load(response)["check_updates"])
            with urllib.request.urlopen(base + "/api/updates", timeout=10) as response:
                self.assertTrue(json.load(response)["check_on_open"])


def make_zip(path, files, top="fels-voice-lab-0.2.0", link=None):
    """A source ZIP as GitHub packs one: every file below one folder."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{top}/", "")
        for name, data in files.items():
            archive.writestr(f"{top}/{name}", data)
        if link:
            entry = zipfile.ZipInfo(f"{top}/{link}")
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, "../../outside")
    return path


OLD = {
    "app_version.py": b'VERSION = "0.1.0"\n',
    "serve.py": b"old serve",
    "README.md": b"readme",
    "catalog/engines.json": b"{}",
    "tools/gone.py": b"gone",
    "tools/edited_gone.py": b"as shipped",
    "requirements/core.txt": b"numpy==1.26.4\n",
}
NEW = {
    "app_version.py": b'VERSION = "0.2.0"\n',
    "serve.py": b"new serve",
    "README.md": b"readme",
    "catalog/engines.json": b'{"new": 1}',
    "tools/added.py": b"added",
    "requirements/core.txt": b"numpy==2.0.0\n",
    "out/evil.txt": b"never",
    ".venv/evil.txt": b"never",
    "runtime.local.json": b"{}",
}
YOURS = {
    "data/cast/cast.sqlite3": b"library",
    "out/kokoro/installation.json": b"{}",
    "runtime.local.json": b'{"device": "cpu"}',
    ".venv/Scripts/python.exe": b"python",
    "hf-cache/model.bin": b"weights",
    "my-notes.txt": b"notes",
}


class Lab:
    """A Lab folder that came from version 0.1.0's ZIP, with a shipped file changed, another one changed that 0.2.0 drops,
    and files of its own."""

    def __init__(self, temp):
        self.root = Path(temp) / "lab"
        for name, data in {**OLD, **YOURS}.items():
            self.write(name, data)
        self.write("catalog/engines.json", b'{"mine": 1}')
        self.write("tools/edited_gone.py", b"changed here")
        self.before = self.files()

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, name):
        return (self.root / name).read_bytes()

    def files(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.relative_to(self.root).as_posix().startswith("out/update/")
        }


class ArchiveTests(unittest.TestCase):
    def test_a_source_zip_is_read_below_its_one_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            files = update.read_archive(make_zip(Path(temp, "v0.2.0.zip"), NEW))
            self.assertEqual(files, NEW)
            self.assertEqual(update.archive_version(files), "0.2.0")

    def test_links_paths_that_climb_out_and_odd_zips_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            link = make_zip(Path(temp, "link.zip"), OLD, link="docs/link")
            climb = make_zip(Path(temp, "climb.zip"), {"../outside.py": b"x"})
            two = make_zip(Path(temp, "two.zip"), OLD)
            with zipfile.ZipFile(two, "a") as archive:
                archive.writestr("other/file.py", b"x")
            broken = Path(temp, "broken.zip")
            broken.write_bytes(b"not a zip")
            for path, words in (
                (link, "holds a link"),
                (climb, "will not write"),
                (two, "does not hold one folder"),
                (broken, "could not be read"),
            ):
                with self.subTest(path.name), self.assertRaisesRegex(update.UpdateStopped, words):
                    update.read_archive(path)

    def test_nothing_an_update_never_touches_is_tracked_by_the_repository(self):
        for path in (
            "out/x",
            ".venv-kokoro/x",
            ".git/config",
            "hf-cache/x",
            "data/cast/x",
            "runtime.local.json",
            "breeze/models/x",
            ".env",
            "reference/a.wav",
        ):
            self.assertTrue(update.protected(path), path)
        for path in ("serve.py", "docs/demo/kokoro-demo.mp3", "breeze/README.md", ".env.example", "tools/update.py"):
            self.assertFalse(update.protected(path), path)
        if GIT is None:
            self.skipTest("git is not installed")
        listed = subprocess.run([GIT, "-C", str(ROOT), "ls-files"], capture_output=True, text=True, encoding="utf-8")
        if listed.returncode:
            self.skipTest("this copy is not a git checkout")
        self.assertEqual([path for path in listed.stdout.splitlines() if update.protected(path)], [])


class ZipUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.lab = Lab(self.temp)
        self.finished = []
        self.enterContext(mock.patch.object(update, "VERSION", CURRENT))  # the updater runs from the folder it updates

    def plan(self, old=OLD):
        return update.zip_plan(self.lab.root, NEW, old)

    def test_the_plan_replaces_shipped_files_and_leaves_yours(self):
        plan = self.plan()
        self.assertEqual(plan["replace"], ["app_version.py", "catalog/engines.json", "requirements/core.txt", "serve.py"])
        self.assertEqual(plan["add"], ["tools/added.py"])
        self.assertEqual(plan["remove"], ["tools/gone.py"])
        self.assertEqual(plan["keep"], ["tools/edited_gone.py"])
        self.assertEqual(plan["yours"], ["catalog/engines.json"])
        self.assertEqual(plan["protected"], [".venv/evil.txt", "out/evil.txt", "runtime.local.json"])
        self.assertEqual(plan["same"], 1)
        self.assertEqual(
            plan["changed"],
            [
                "app_version.py",
                "catalog/engines.json",
                "requirements/core.txt",
                "serve.py",
                "tools/added.py",
                "tools/edited_gone.py",
                "tools/gone.py",
            ],
        )

    def test_without_the_old_release_nothing_is_removed(self):
        plan = self.plan(old=None)
        self.assertEqual((plan["remove"], plan["keep"], plan["yours"]), ([], [], []))
        self.assertEqual(plan["changed"], plan["add"] + plan["replace"])

    def runner(self, command, **kwargs):
        self.finished.append(command)
        return subprocess.CompletedProcess(command, 0)

    def fetch(self, url, destination):
        tag = url.rsplit("/", 1)[1]
        files = {"v0.2.0": NEW, f"v{CURRENT}": OLD}.get(tag)
        return make_zip(Path(destination), files, top=f"fels-voice-lab-{tag[1:]}") if files is not None else None

    def update(self, *argv, running=(), check=None, fetch=None):
        args = update.build_parser().parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = update.update(
                self.lab.root,
                args=args,
                check=check or (lambda: updates.check(current=CURRENT, fetch=answers(release()))),
                runner=self.runner,
                processes=lambda root: list(running),
                fetch=fetch or self.fetch,
                interactive=False,
                say=print,
            )
        return status, " ".join(out.getvalue().split())

    def test_an_update_replaces_the_shipped_files_keeps_a_copy_and_hands_over_to_the_new_version(self):
        status, out = self.update("--yes")
        self.assertEqual(status, 0)
        for name in ("app_version.py", "serve.py", "catalog/engines.json", "tools/added.py", "requirements/core.txt"):
            self.assertEqual(self.lab.read(name), NEW[name], name)
        self.assertFalse((self.lab.root / "tools/gone.py").exists())
        self.assertEqual(self.lab.read("tools/edited_gone.py"), b"changed here")
        for name, data in YOURS.items():
            self.assertEqual(self.lab.read(name), data, name)
        self.assertFalse((self.lab.root / "out/evil.txt").exists() or (self.lab.root / ".venv/evil.txt").exists())
        backup = self.lab.root / "out/update/0.1.0-to-0.2.0/files"
        self.assertEqual((backup / "catalog/engines.json").read_bytes(), b'{"mine": 1}')
        self.assertEqual((backup / "tools/gone.py").read_bytes(), b"gone")
        self.assertFalse((self.lab.root / "out/update/downloads").exists())
        record = update.read_record(self.lab.root)
        self.assertEqual(
            (record["state"], record["from"], record["to"], record["method"]), ("applied", CURRENT, "0.2.0", "zip")
        )
        self.assertEqual(self.finished, [[sys.executable, str(self.lab.root / "tools" / "update.py"), "--finish"]])
        for words in (
            "update from 0.1.0 to 0.2.0",
            "Adds Check for updates.",
            "4 to replace, 1 new, 1 to remove",
            "out\\update\\0.1.0-to-0.2.0",
            "your copy goes in the backup and the new one replaces it: catalog/engines.json",
            "left in place: tools/edited_gone.py",
        ):
            self.assertIn(words, out)

    def test_undo_puts_the_previous_version_back(self):
        self.update("--yes")
        args = update.build_parser().parse_args(["--undo", "--yes"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                update.undo(self.lab.root, args=args, runner=self.runner, processes=lambda root: [], interactive=False), 0
            )
        self.assertEqual(self.lab.files(), self.lab.before)
        self.assertEqual(update.read_record(self.lab.root)["state"], "undone")
        self.assertEqual(len(self.finished), 2)
        with self.assertRaisesRegex(update.UpdateStopped, "no update to undo"):
            update.undo(self.lab.root, args=args, runner=self.runner, processes=lambda root: [], interactive=False)

    def test_a_dry_run_or_a_check_changes_nothing(self):
        status, out = self.update("--dry-run", running=[(42, str(self.lab.root / ".venv/Scripts/python.exe"))])
        self.assertEqual(status, 0)
        self.assertIn("This was a dry run, so nothing was changed.", out)
        self.assertIn("The Lab is running from this folder", out)
        status, out = self.update("--check")
        self.assertEqual((status, out), (0, "Version 0.2.0 is available. This is version 0.1.0."))
        self.assertEqual(self.lab.files(), self.lab.before)
        self.assertIsNone(update.read_record(self.lab.root))
        self.assertEqual(self.finished, [])

    def test_it_refuses_while_the_lab_runs(self):
        python = str(self.lab.root / ".venv-kokoro" / "Scripts" / "python.exe")
        with self.assertRaisesRegex(update.UpdateStopped, r"The Lab is running from this folder: .*python.exe \(process 42\)"):
            self.update("--yes", running=[(42, python)])
        self.assertEqual(self.lab.files(), self.lab.before)

    def test_nothing_happens_when_this_is_the_newest_version(self):
        status, out = self.update("--yes", check=lambda: updates.check(current=CURRENT, fetch=answers(release(f"v{CURRENT}"))))
        self.assertEqual((status, out), (0, f"This is the newest version, {CURRENT}."))
        self.assertEqual(self.lab.files(), self.lab.before)

    def test_a_release_whose_files_name_another_version_is_not_used(self):
        def fetch(url, destination):
            files = {**NEW, "app_version.py": b'VERSION = "0.1.9"\n'} if "v0.2.0" in url else OLD
            return make_zip(Path(destination), files)

        with self.assertRaisesRegex(update.UpdateStopped, "says it is version 0.1.9, not 0.2.0"):
            self.update("--yes", fetch=fetch)
        self.assertEqual(self.lab.files(), self.lab.before)

    def test_a_file_that_cannot_be_written_puts_back_everything_already_changed(self):
        self.lab.write("zz", b"a file where the release has a folder")
        before = self.lab.files()
        files = {**NEW, "zz/inside.py": b"x"}
        plan = update.zip_plan(self.lab.root, files, OLD)
        self.assertEqual(plan["add"], ["tools/added.py", "zz/inside.py"])
        record = {
            "schema": update.SCHEMA,
            "from": CURRENT,
            "to": "0.2.0",
            "method": "zip",
            "backup": "out/update/0.1.0-to-0.2.0",
        }
        with self.assertRaisesRegex(update.UpdateStopped, "Everything the update had changed was put back"):
            update.apply_zip(self.lab.root, plan, files, record)
        self.assertEqual(self.lab.files(), before)
        self.assertEqual(update.read_record(self.lab.root)["state"], "rolled back")


@unittest.skipIf(GIT is None, "git is not installed")
class GitUpdateTests(unittest.TestCase):
    def git(self, folder, *args):
        done = subprocess.run(
            [
                GIT,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                "-C",
                str(folder),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def commit(self, folder, files, tag):
        for name, data in files.items():
            path = Path(folder) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git(folder, "add", "-A")
        self.git(folder, "commit", "-q", "-m", tag)
        self.git(folder, "tag", tag)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-q")
        self.commit(self.source, {"app_version.py": b'VERSION = "0.1.0"\n', "serve.py": b"old"}, "v0.1.0")
        self.commit(
            self.source,
            {"app_version.py": b'VERSION = "0.2.0"\n', "serve.py": b"new", "requirements/core.txt": b"numpy\n"},
            "v0.2.0",
        )
        self.lab = Path(temp.name) / "lab"
        self.git(self.source.parent, "clone", "-q", "--no-tags", str(self.source), str(self.lab))
        self.git(self.lab, "reset", "-q", "--hard", self.git(self.source, "rev-parse", "v0.1.0"))
        self.calls = []
        self.enterContext(mock.patch.object(update, "VERSION", CURRENT))

    def runner(self, command, **kwargs):
        if command[0] == "git":
            return subprocess.run([GIT, *command[1:]], **kwargs)
        self.calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    def test_a_checkout_moves_to_the_release_tag_and_back(self):
        plan = update.git_plan(self.lab, "v0.2.0", source=str(self.source), runner=self.runner)
        self.assertEqual(plan["changed"], ["app_version.py", "requirements/core.txt", "serve.py"])
        self.assertEqual(plan["to"], self.git(self.source, "rev-parse", "v0.2.0"))
        with mock.patch.object(update, "git_plan", lambda root, tag, runner: plan), contextlib.redirect_stdout(io.StringIO()):
            status = update.update(
                self.lab,
                args=update.build_parser().parse_args(["--yes"]),
                check=lambda: updates.check(current=CURRENT, fetch=answers(release())),
                runner=self.runner,
                processes=lambda root: [],
                interactive=False,
            )
        self.assertEqual(status, 0)
        self.assertEqual((self.lab / "serve.py").read_bytes(), b"new")
        self.assertEqual(update.read_record(self.lab)["git"], {"from": plan["from"], "to": plan["to"]})
        args = update.build_parser().parse_args(["--undo", "--yes"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                update.undo(self.lab, args=args, runner=self.runner, processes=lambda root: [], interactive=False), 0
            )
        self.assertEqual((self.lab / "serve.py").read_bytes(), b"old")
        self.assertEqual(len(self.calls), 2)  # --finish after the update, and again after the undo

    def test_changed_tracked_files_or_commits_of_its_own_are_refused(self):
        (self.lab / "serve.py").write_bytes(b"edited")
        with self.assertRaisesRegex(update.UpdateStopped, "Files git tracks were changed here: serve.py"):
            update.git_plan(self.lab, "v0.2.0", source=str(self.source), runner=self.runner)
        self.git(self.lab, "commit", "-q", "-am", "mine")
        with self.assertRaisesRegex(update.UpdateStopped, "has commits that v0.2.0 does not"):
            update.git_plan(self.lab, "v0.2.0", source=str(self.source), runner=self.runner)


# The name of the dist-info folder of each build of PyTorch, as pip leaves it in an environment.
TORCH = {"cpu": "torch-2.12.0", "cuda": "torch-2.12.0+cu130", "rocm": "torch-2.12.0+rocm7.14.0"}
KEPT = "The model files you already have are kept, so only the packages download again."


def content(path, revision):
    """What a stand-in model file holds: its name and revision, so the same file at another revision has another size."""
    return f"{path} at {revision}\n".encode()


def pinned(entry):
    """A shipped list of a model's files (or one repository of it) with stand-ins of a few bytes for its files."""
    revision = entry.get("revision") or entry["commit"]
    files = [
        {
            "path": item["path"],
            "size": len(content(item["path"], revision)),
            "sha256": hashlib.sha256(content(item["path"], revision)).hexdigest(),
        }
        for item in entry["files"]
    ]
    return {**entry, "files": files}


DRAMA_ASSETS = tuple(pinned({"commit": "drama", "files": [{"path": "LICENSE"}, {"path": "NOTICE"}]})["files"])


class ModelLab:
    """A Lab folder for --finish: the shipped lists of the models' files in catalog/, with stand-ins of a few bytes for the
    files, and the models a test installs: an environment holding a build of PyTorch, and the model's files at the pinned
    revision or an older one, where its installer puts them. Drama 3 is installed as its receipt says."""

    def __init__(self, temp):
        self.root = Path(temp)
        for _, _, listings in update.MODELS.values():
            for listing in listings:
                if listing.startswith("catalog/"):
                    shipped = json.loads((ROOT / listing).read_text(encoding="utf-8"))
                    if "repos" in shipped:
                        shipped["repos"] = [pinned(repo) for repo in shipped["repos"]]
                    self.write(listing, json.dumps(shipped if "repos" in shipped else pinned(shipped)).encode())
        self.text = ""

    def write(self, path, data):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def environment(self, environment, build):
        """Make an environment, or change the build of PyTorch it holds (None: none)."""
        site = self.root / environment / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        for old in site.glob("torch-*.dist-info"):
            old.rmdir()
        if build:
            (site / f"{TORCH[build]}.dist-info").mkdir()

    def install(self, name, build=None, *, old=False):
        """Install a model as its installer would: its environment, and every file of its lists at the pinned revision (of
        Kokoro's voices, those its installer fetches by default), or with old=True at the revision before (Breeze's, in a
        folder of its own, then hold other content)."""
        environment, _, listings = update.MODELS[name]
        self.environment(environment, build)
        for listing in listings:
            manifest = json.loads((self.root / listing).read_text(encoding="utf-8"))
            if listing == update.VOXCPM_SOURCE:
                folder = self.root / install_voxcpm.SOURCE
                self.put(folder, manifest["files"], "old" if old else manifest["commit"])
                continue
            for entry in manifest.get("repos", [manifest]):
                revision = "old" if old else entry["revision"]
                folder = install_kokoro.snapshot_dir(self.root, entry if "folder" in entry else {**entry, "revision": revision})
                chosen = install_kokoro.choose_voices(entry, ["supported"])
                self.put(
                    folder, entry["files"] if "repos" in manifest else install_kokoro.checkpoint_files(entry, chosen), revision
                )

    @staticmethod
    def put(folder, files, revision):
        for item in files:
            (folder / item["path"]).parent.mkdir(parents=True, exist_ok=True)
            (folder / item["path"]).write_bytes(content(item["path"], revision))

    def drama(self, model_name=install_drama3.MODEL, **receipt):
        """Install Drama 3 as its receipt says: the model and the runtime it names, the small files, and the receipt, with the
        pins of its installer unless a test names others."""
        folder = self.root / "out/drama3"
        model, runtime = folder / "models" / model_name, folder / "runtime" / "speech.exe"
        for path in (model, runtime):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stand-in")
        self.put(folder / "models", DRAMA_ASSETS, "drama")
        receipt = {
            "model_path": str(model),
            "model_sha256": install_drama3.MODEL_SHA256,
            "runtime_path": str(runtime),
            "runtime_archive_sha256": install_drama3.RUNTIME_SHA256,
            **receipt,
        }
        (folder / "installation.json").write_text(json.dumps(receipt), encoding="utf-8")

    def finish(self, *changed):
        """Run --finish after an update that changed these files: the advice it printed, one sentence per item (all of what
        it printed, on one line, is in self.text)."""
        record = {"schema": update.SCHEMA, "from": "0.1.0", "to": "0.2.0", "method": "git", "state": "applied"}
        update.save_record(self.root, {**record, "changed": list(changed)})
        lines = []
        update.finish(self.root, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0), say=lines.append)
        self.text = " ".join("\n".join(lines).split())
        advice = []
        for line in "\n".join(lines).splitlines():
            if line.startswith("  - "):
                advice.append(line[4:])
            elif line.startswith("    ") and advice:
                advice[-1] += " " + line.strip()
        return advice


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(install_drama3, "ASSETS", DRAMA_ASSETS))
        self.lab = ModelLab(self.enterContext(tempfile.TemporaryDirectory()))

    def test_finish_updates_changed_package_lists_and_names_the_models_to_reinstall(self):
        root = self.lab.root
        for folder in (".venv/Scripts", ".venv-desktop/Scripts"):
            (root / folder).mkdir(parents=True)
            (root / folder / "python.exe").write_bytes(b"")
        self.lab.install("Kokoro", "cpu")
        self.lab.install("Phonon", old=True)
        update.save_record(
            root,
            {
                "schema": update.SCHEMA,
                "from": "0.1.0",
                "to": "0.2.0",
                "method": "zip",
                "state": "applied",
                "backup": "out/update/0.1.0-to-0.2.0",
                "yours": ["catalog/engines.json"],
                "changed": [
                    "requirements/core.txt",
                    "requirements/kokoro-cpu.txt",
                    "catalog/phonon_snapshot.json",
                    "catalog/chatterbox_turbo_snapshot.json",
                ],
            },
        )
        calls, out = [], io.StringIO()
        runner = lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0)  # noqa: E731
        with contextlib.redirect_stdout(out):
            self.assertEqual(update.finish(root, runner=runner), 0)
        text = " ".join(out.getvalue().split())
        self.assertEqual(
            calls,
            [
                [
                    str(root / ".venv/Scripts/python.exe"),
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "--disable-pip-version-check",
                    "-r",
                    str(root / "requirements/core.txt"),
                ]
            ],
        )
        self.assertIn(
            "- Kokoro's pinned packages changed (requirements\\kokoro-cpu.txt). Delete the .venv-kokoro folder and install it "
            f"again: press Install on its card in Models & features, or run setup-kokoro.bat. {KEPT}",
            text,
        )
        self.assertIn("- Phonon's model files changed. Run setup-phonon.bat to fetch them.", text)
        self.assertNotIn("Chatterbox", text)  # not installed here
        self.assertIn("Updated to version 0.2.0.", text)
        self.assertIn("out\\update\\0.1.0-to-0.2.0\\files: catalog/engines.json", text)

    def test_only_the_qwen3_tts_models_installed_here_are_named(self):
        self.lab.install("Qwen3-TTS 1.7B VoiceDesign", "rocm", old=True)
        self.lab.install("Qwen3-TTS 1.7B CustomVoice", "rocm")
        self.assertEqual(
            self.lab.finish("requirements/qwen-nvidia.txt"),
            [
                "The pinned packages of Qwen3-TTS 1.7B VoiceDesign and Qwen3-TTS 1.7B CustomVoice changed "
                "(requirements\\qwen-nvidia.txt). Delete the .venv-fqwen folder, which they share, and install them again: "
                "press Install on their cards in Models & features, or run setup-qwen.bat --model 1.7b-voice-design and "
                f"setup-qwen.bat --model 1.7b-custom-voice. {KEPT}"
            ],
        )
        # All four lists of model files changed: VoiceDesign's revision is an older one, CustomVoice's files are already in
        # place, and the Base models are not installed.
        qwen = [listings[0] for name, (_, _, listings) in update.MODELS.items() if name.startswith("Qwen3-TTS")]
        self.assertEqual(len(qwen), 4)
        self.assertEqual(
            self.lab.finish(*qwen),
            ["Qwen3-TTS 1.7B VoiceDesign's model files changed. Run setup-qwen.bat --model 1.7b-voice-design to fetch them."],
        )
        # Installing again fetches the new files too, so they are named with the packages and not on their own.
        advice = self.lab.finish("requirements/qwen-nvidia.txt", *qwen)
        self.assertEqual(len(advice), 1)
        self.assertTrue(
            advice[0].endswith("so only the packages download again, and the new model files of Qwen3-TTS 1.7B VoiceDesign."),
            advice[0],
        )

    def test_chatterbox_500m_and_voice_conversion_are_named_only_where_their_checkpoint_is(self):
        self.lab.install("Chatterbox Turbo", "cpu")
        self.assertEqual(self.lab.finish("catalog/chatterbox_snapshot.json"), [])
        self.lab.install("Chatterbox 500M and Voice Conversion", "cpu", old=True)
        fetch = "Chatterbox 500M and Voice Conversion's model files changed. Run setup-chatterbox.bat --original"
        self.assertEqual(self.lab.finish("catalog/chatterbox_snapshot.json"), [f"{fetch} to fetch them."])
        self.lab.environment(".venv-chatterbox", "rocm")  # the GPU build, which its installer must be asked for again
        self.assertEqual(self.lab.finish("catalog/chatterbox_snapshot.json"), [f"{fetch} --gpu to fetch them."])
        self.assertEqual(self.lab.finish("catalog/chatterbox_turbo_snapshot.json"), [])  # Turbo's files are in place

    def test_drama_3_is_named_only_when_its_installer_would_fetch_a_file(self):
        self.assertEqual(self.lab.finish("tools/install_drama3.py"), [])  # not installed
        self.lab.drama()
        # The pins its receipt names are its installer's, as after the update from 0.1.0, which rewrote the installer.
        self.assertEqual(self.lab.finish("tools/install_drama3.py"), [])
        fetch = "Drama 3's pinned files changed. Run setup-drama3.bat to fetch them."
        for receipt in ({"model_sha256": "0" * 64}, {"runtime_archive_sha256": "0" * 64}):
            self.lab.drama(**receipt)
            self.assertEqual(self.lab.finish("tools/install_drama3.py"), [fetch])
        self.lab.drama()
        (self.lab.root / "out/drama3/models/NOTICE").write_bytes(b"another notice")
        self.assertEqual(self.lab.finish("tools/install_drama3.py"), [fetch])
        self.lab.drama("Drama-older.gguf")  # a model of another name: the new one goes beside it
        old = self.lab.root / "out/drama3/models/Drama-older.gguf"
        self.assertEqual(
            self.lab.finish("tools/install_drama3.py"),
            [f"{fetch} It then no longer uses {old}, which you can delete to free 0.0 GB."],
        )
        self.assertEqual(self.lab.finish("requirements/core.txt"), [])  # its installer did not change

    def test_amd_rocm_txt_reaches_every_model_installed_with_amds_build_and_no_other(self):
        gpu = ("Kokoro", "Chatterbox Turbo", "Chatterbox 500M and Voice Conversion", "Qwen3-TTS 0.6B Base", "VoxCPM2")
        for name in (*gpu, "Breeze TTS 2"):
            self.lab.install(name, "rocm")
        self.lab.install("Phonon")
        advice = self.lab.finish(update.AMD_LOCK)
        self.assertEqual(
            advice[:2],
            [
                "Kokoro's pinned packages changed (requirements\\amd-rocm.txt). Delete the .venv-kokoro folder and install it "
                "again: press Install on its card in Models & features with its switch on GPU, or run setup-kokoro.bat --gpu. "
                f"{KEPT}",
                "The pinned packages of Chatterbox Turbo and Chatterbox 500M and Voice Conversion changed "
                "(requirements\\amd-rocm.txt). Delete the .venv-chatterbox folder, which they share, and install them again: "
                "press Install on their cards in Models & features with their switches on GPU, or run setup-chatterbox.bat "
                f"--gpu and setup-chatterbox.bat --original --gpu. {KEPT}",
            ],
        )
        # The models that run only on a graphics card always get its build: their installers need no --gpu.
        for text, (environment, command) in zip(
            advice[2:],
            [
                (".venv-fqwen", "setup-qwen.bat --model 0.6b-base"),
                (".venv-voxcpm", "setup-voxcpm.bat"),
                (".venv-breeze", "setup-breeze.bat"),
            ],
        ):
            self.assertIn(f"(requirements\\amd-rocm.txt). Delete the {environment} folder and install it again", text)
            self.assertIn(f"press Install on its card in Models & features, or run {command}. {KEPT}", text)
        self.assertEqual(len(advice), 5)  # not Phonon
        # NVIDIA's builds, and Kokoro's CPU build, are not installed from it.
        for name in (*gpu, "Breeze TTS 2"):
            self.lab.install(name, "cuda")
        self.lab.environment(".venv-kokoro", "cpu")
        self.assertEqual(self.lab.finish(update.AMD_LOCK), [])

    def test_a_gpu_build_is_asked_for_with_gpu_and_nvidias_alone_never(self):
        self.lab.install("Kokoro", "cuda", old=True)  # at the revision before the one the new version pins
        self.assertEqual(
            self.lab.finish("requirements/kokoro-nvidia.txt"),
            [
                "Kokoro's pinned packages changed (requirements\\kokoro-nvidia.txt). Delete the .venv-kokoro folder and install "
                "it again: press Install on its card in Models & features with its switch on GPU, or run setup-kokoro.bat --gpu. "
                f"{KEPT}"
            ],
        )
        said = self.lab.text
        self.lab.environment(".venv-kokoro", "cpu")
        self.assertEqual(self.lab.finish("requirements/kokoro-nvidia.txt", update.AMD_LOCK), [])  # not the CPU build's lists
        self.lab.environment(".venv-kokoro", None)  # no PyTorch that can be read: every list counts, and no build is asked for
        self.assertEqual(
            self.lab.finish(update.AMD_LOCK),
            [
                "Kokoro's pinned packages changed (requirements\\amd-rocm.txt). Delete the .venv-kokoro folder and install it "
                f"again: press Install on its card in Models & features, or run setup-kokoro.bat. {KEPT}"
            ],
        )
        self.lab.environment(".venv-kokoro", "cuda")
        self.assertEqual(
            self.lab.finish("catalog/kokoro_snapshot.json"),
            ["Kokoro's model files changed. Run setup-kokoro.bat --gpu to fetch them."],
        )
        self.assertNotIn("--nvidia", said + self.lab.text)
        self.lab.install("Kokoro", "cuda")  # the files the new version pins are in place already
        self.assertEqual(self.lab.finish("catalog/kokoro_snapshot.json"), [])

    def test_the_voices_kokoro_leaves_out_by_default_are_not_asked_for(self):
        self.lab.install("Kokoro", "cpu")
        self.assertFalse(
            (
                install_kokoro.snapshot_dir(
                    self.lab.root, json.loads((self.lab.root / install_kokoro.MANIFEST).read_text("utf-8"))
                )
                / "voices/jf_alpha.pt"
            ).exists()
        )
        self.assertEqual(self.lab.finish("catalog/kokoro_snapshot.json"), [])

    def test_a_list_that_cannot_be_read_does_not_stop_the_update_from_finishing(self):
        self.lab.install("Kokoro", "cpu")
        (self.lab.root / "catalog/kokoro_snapshot.json").write_text("{", encoding="utf-8")
        advice = self.lab.finish("requirements/kokoro-cpu.txt")
        self.assertTrue(advice[0].startswith("The installed models could not be checked"), advice)
        self.assertIn("Updated to version 0.2.0.", self.lab.text)

    def test_the_build_of_pytorch_is_read_as_the_models_page_reads_it(self):
        import engine_devices

        entry = {"runtime": {"python": ".venv-kokoro/Scripts/python.exe"}}
        for build in (None, "cpu", "cuda", "rocm", "torch-2.6.0+cpu"):
            if build in TORCH or build is None:
                self.lab.environment(".venv-kokoro", build)
            else:
                (self.lab.root / ".venv-kokoro/Lib/site-packages" / f"{build}.dist-info").mkdir()
            self.assertEqual(
                update.torch_build(self.lab.root, ".venv-kokoro"), engine_devices.torch_build(self.lab.root, entry)[0]
            )
        self.assertIsNone(update.torch_build(self.lab.root, ".venv-missing"))

    def test_the_models_and_lists_are_the_installers_own(self):
        import engine_checks
        from tools import amd_build as amd
        from tools import install_breeze as ib
        from tools import install_chatterbox as ic
        from tools import install_phonon as ip
        from tools import install_qwen as iq

        ik, iv = install_kokoro, install_voxcpm
        self.assertEqual(update.AMD_LOCK, amd.LOCK)
        self.assertEqual(
            update.ENVIRONMENTS,
            {
                ik.ENVIRONMENT: {"cpu": (ik.LOCK,), "cuda": (ik.LOCK, ik.NVIDIA_LOCK), "rocm": (ik.LOCK, amd.LOCK)},
                ip.ENVIRONMENT: {None: (ip.LOCK,)},
                ic.ENVIRONMENT: {"cpu": (ic.LOCK,), "cuda": (ic.LOCK, ic.NVIDIA_LOCK), "rocm": (ic.LOCK, amd.LOCK)},
                **{module.ENVIRONMENT: {"cuda": (module.LOCK,), "rocm": (module.LOCK, amd.LOCK)} for module in (iq, iv, ib)},
            },
        )
        self.assertEqual(
            update.MODELS,
            {
                "Kokoro": (ik.ENVIRONMENT, "setup-kokoro.bat", (ik.MANIFEST,)),
                "Phonon": (ip.ENVIRONMENT, "setup-phonon.bat", (ip.MANIFEST,)),
                "Chatterbox Turbo": (ic.ENVIRONMENT, "setup-chatterbox.bat", (ic.MANIFEST,)),
                "Chatterbox 500M and Voice Conversion": (
                    ic.ENVIRONMENT,
                    "setup-chatterbox.bat --original",
                    (ic.ORIGINAL_MANIFEST,),
                ),
                **{
                    spec["name"]: (iq.ENVIRONMENT, f"setup-qwen.bat --model {spec['short']}", (spec["manifest"],))
                    for spec in iq.MODELS.values()
                },
                "VoxCPM2": (iv.ENVIRONMENT, "setup-voxcpm.bat", (iv.MANIFEST, iv.SOURCE_MANIFEST)),
                "Breeze TTS 2": (ib.ENVIRONMENT, "setup-breeze.bat", (ib.MANIFEST,)),
                "Drama 3": (None, "setup-drama3.bat", ("tools/install_drama3.py",)),
            },
        )
        # Every model the Models page can install is here, with the command its card names.
        self.assertEqual({command for _, command, _ in update.MODELS.values()}, set(engine_checks.GUIDED_INSTALLERS.values()))
        self.assertEqual(update.DRAMA_RECEIPT, engine_checks.DRAMA_RECEIPT)
        self.assertEqual(update.WEIGHT_SUFFIXES, engine_checks.WEIGHT_SUFFIXES)
        self.assertEqual(update.VOXCPM_SOURCE, iv.SOURCE_MANIFEST)
        for _, command, listings in update.MODELS.values():
            self.assertTrue((ROOT / command.split()[0]).is_file(), command)
            for listing in listings:
                self.assertTrue((ROOT / listing).is_file(), listing)
        # Every package list is watched, but the linter's and the one for the speech recognizer's .venv-asr, which is made by
        # hand (docs/SETUP.md) and has no installer to name.
        watched = {item for builds in update.ENVIRONMENTS.values() for lists in builds.values() for item in lists}
        watched |= {listing for _, listing in update.SYNCED}
        shipped = {path.relative_to(ROOT).as_posix() for path in (ROOT / "requirements").glob("*.txt")}
        self.assertEqual(shipped - watched, {"requirements/lint.txt", "requirements/asr-faster-whisper.txt"})
        for listing in watched:
            self.assertTrue((ROOT / listing).is_file(), listing)


class RunningLabTests(unittest.TestCase):
    def test_only_programs_from_this_folders_environments_count_and_the_updater_itself_does_not(self):
        root = Path(tempfile.gettempdir()) / "voice-lab-copy"
        images = {
            1: str(root / ".venv" / "Scripts" / "python.exe"),
            2: str(root / ".venv-kokoro" / "Scripts" / "python.exe"),
            3: str(root / ".venv-desktop" / "Scripts" / "pythonw.exe"),
            4: r"C:\Program Files\Python312\python.exe",
            5: str(root.parent / "voice-lab-copy-2" / ".venv" / "Scripts" / "python.exe"),
            6: str(root / "serve.bat"),
        }
        self.assertEqual([pid for pid, _image in update.lab_processes(root, images=images, own={1})], [2, 3])

    @unittest.skipUnless(os.name == "nt", "Windows lists processes this way")
    def test_windows_names_the_program_of_this_process(self):
        images = update.process_images()
        self.assertIn(os.getpid(), images)
        self.assertTrue(Path(images[os.getpid()]).name.lower().startswith("python"))


class LauncherTests(unittest.TestCase):
    def test_update_bat_runs_the_updater_in_a_block_cmd_has_read_before_python_starts(self):
        data = (ROOT / "update.bat").read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "batch files keep CRLF line ends")
        lines = [line.strip() for line in data.decode("ascii").splitlines()]
        start = lines.index("(")
        # cmd.exe expands %errorlevel% when it reads the block, so the exit code is read with call when the line runs; an
        # exit /b inside an if inside the block would not reach the process that started update.bat.
        self.assertEqual(
            lines[start:],
            [
                "(",
                '".venv\\Scripts\\python.exe" tools\\update.py %*',
                "call set result=%%errorlevel%%",
                "pause",
                "call exit /b %%result%%",
                ")",
            ],
        )
        self.assertNotIn("goto", " ".join(lines[start:]).lower())

    @unittest.skipUnless(os.name == "nt", "cmd.exe runs batch files on Windows")
    def test_update_bat_passes_on_the_updaters_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            lab = Path(temp)
            (lab / "tools").mkdir()
            (lab / ".venv" / "Scripts").mkdir(parents=True)
            shutil.copy2(ROOT / "update.bat", lab / "update.bat")
            # A stand-in updater that exits with the code it is given, run by this test's own Python.
            (lab / "tools" / "update.py").write_text("import sys\nsys.exit(int(sys.argv[1]))\n", encoding="utf-8")
            launcher = (lab / "update.bat").read_text(encoding="ascii")
            (lab / "update.bat").write_text(
                launcher.replace('".venv\\Scripts\\python.exe"', f'"{sys.executable}"'), encoding="ascii", newline=""
            )
            (lab / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
            for code in (0, 1, 130):
                done = subprocess.run(
                    ["cmd", "/c", str(lab / "update.bat"), str(code)], stdin=subprocess.DEVNULL, capture_output=True
                )
                self.assertEqual(done.returncode, code)


if __name__ == "__main__":
    unittest.main()
