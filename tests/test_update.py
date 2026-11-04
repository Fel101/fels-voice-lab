"""Check for updates (updates.py, and the service's /api/updates) and update.bat (tools/update.py). GitHub, the downloads,
the processes and pip are stand-ins; the Git tests make their own repositories in a temporary folder."""

import contextlib
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
from tools import update  # noqa: E402

GIT = shutil.which("git")


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
        files = {"v0.2.0": NEW, f"v{VERSION}": OLD}.get(tag)
        return make_zip(Path(destination), files, top=f"fels-voice-lab-{tag[1:]}") if files is not None else None

    def update(self, *argv, running=(), check=None, fetch=None):
        args = update.build_parser().parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = update.update(
                self.lab.root,
                args=args,
                check=check or (lambda: updates.check(fetch=answers(release()))),
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
            (record["state"], record["from"], record["to"], record["method"]), ("applied", VERSION, "0.2.0", "zip")
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
        status, out = self.update("--yes", check=lambda: updates.check(fetch=answers(release(f"v{VERSION}"))))
        self.assertEqual((status, out), (0, f"This is the newest version, {VERSION}."))
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
            "from": VERSION,
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
                check=lambda: updates.check(fetch=answers(release())),
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


class FinishTests(unittest.TestCase):
    def test_finish_updates_changed_package_lists_and_names_the_models_to_reinstall(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for folder in (".venv/Scripts", ".venv-desktop/Scripts", ".venv-kokoro", ".venv-phonon"):
                (root / folder).mkdir(parents=True)
            (root / ".venv/Scripts/python.exe").write_bytes(b"")
            (root / ".venv-desktop/Scripts/python.exe").write_bytes(b"")
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
                        "requirements/kokoro-nvidia.txt",
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
            self.assertIn("Kokoro's pinned packages changed. Delete the .venv-kokoro folder and run setup-kokoro.bat", text)
            self.assertIn("Phonon's model files changed. Run setup-phonon.bat to fetch them.", text)
            self.assertNotIn("Chatterbox", text)  # not installed here
            self.assertIn("Updated to version 0.2.0.", text)
            self.assertIn("out\\update\\0.1.0-to-0.2.0\\files: catalog/engines.json", text)

    def test_the_model_files_it_watches_are_the_installers_own(self):
        from tools import install_chatterbox as ic, install_kokoro as ik, install_phonon as ip

        self.assertEqual(update.MODELS["Kokoro"], (ik.ENVIRONMENT, "setup-kokoro.bat", (ik.LOCK, ik.NVIDIA_LOCK), ik.MANIFEST))
        self.assertEqual(update.MODELS["Phonon"], (ip.ENVIRONMENT, "setup-phonon.bat", (ip.LOCK,), ip.MANIFEST))
        self.assertEqual(
            update.MODELS["Chatterbox Turbo"], (ic.ENVIRONMENT, "setup-chatterbox.bat", (ic.LOCK, ic.NVIDIA_LOCK), ic.MANIFEST)
        )
        for environment, launcher, locks, manifest in update.MODELS.values():
            self.assertTrue((ROOT / launcher).is_file(), launcher)
            for name in (*locks, manifest):
                self.assertTrue((ROOT / name).is_file(), name)


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
