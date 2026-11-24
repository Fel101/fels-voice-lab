"""Update now (self_update.py, the System page's routes, desktop.py's hand-over, and update.py's --dry-run --json, --from-lab
and the notice --finish leaves). GitHub, the downloads, the processes, pip, the updater's window and File Explorer are
stand-ins."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_lab import engine_adapters  # noqa: E402
from voice_lab import self_update  # noqa: E402
from voice_lab import updates  # noqa: E402
from tests.test_update import CURRENT, NEW, OLD, ModelLab, answers, make_zip, release  # noqa: E402
from tests.test_update import Lab as ZipLab  # noqa: E402
from tools import update  # noqa: E402


class UpdaterTests(unittest.TestCase):
    """update.py's side: the plan for the page, the updater the Lab starts, and the notice."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.lab = ZipLab(Path(temp.name))
        self.root = self.lab.root
        self.enterContext(mock.patch.object(update, "VERSION", CURRENT))
        self.finished, self.opened, self.waits, self.asked = [], [], [], []

    def runner(self, command, **kwargs):
        self.finished.append(command)
        return subprocess.CompletedProcess(command, 0)

    def fetch(self, url, destination):
        tag = url.rsplit("/", 1)[1]
        files = {"v0.2.0": NEW, f"v{CURRENT}": OLD}.get(tag)
        return make_zip(Path(destination), files, top=f"fels-voice-lab-{tag[1:]}") if files is not None else None

    def check(self, found=None):
        return lambda: updates.check(current=CURRENT, fetch=answers(found or release()))

    def from_lab(self, *argv, running=None, check=None, fetch=None):
        """Run the updater; `running` is the list of processes each look finds, the last one repeated."""
        looks = list(running or [[]])
        clock = iter(range(0, 10_000, 1))

        def processes(root):
            return looks.pop(0) if len(looks) > 1 else looks[0]

        out = []
        status = update.from_lab(
            self.root,
            args=update.build_parser().parse_args(["--from-lab", *argv]),
            processes=processes,
            wait=self.waits.append,
            clock=lambda: next(clock),
            opener=lambda root: self.opened.append(root) or True,
            ask=lambda: self.asked.append(True) or "",
            say=out.append,
            check=check or self.check(),
            fetch=fetch or self.fetch,
            runner=self.runner,
            interactive=False,
        )
        return status, " ".join(" ".join(out).split())

    def notice(self):
        path = update.notice_path(self.root)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def test_the_plan_for_the_page_is_update_bats_plan_in_sentences(self):
        args = update.build_parser().parse_args(["--dry-run", "--json"])
        python = str(self.root / ".venv/Scripts/python.exe")
        report = update.plan_report(
            self.root,
            args=args,
            check=self.check(),
            runner=self.runner,
            processes=lambda root: [(42, python)],
            fetch=self.fetch,
            interactive=False,
        )
        self.assertEqual(
            {key: report[key] for key in ("ok", "newer", "from", "to", "method", "running")},
            {"ok": True, "newer": True, "from": CURRENT, "to": "0.2.0", "method": "zip", "running": True},
        )
        self.assertEqual(report["release"]["url"], release()["html_url"])
        self.assertTrue(
            report["steps"][0].startswith("The Lab's own files, from the release on GitHub: 4 to replace, 1 new, 1 to remove;")
        )
        self.assertIn("out\\update\\0.1.0-to-0.2.0", report["steps"][1])
        self.assertTrue(any("catalog/engines.json" in note for note in report["notes"]))
        self.assertTrue(all("\n" not in text for text in report["steps"] + report["notes"]))
        self.assertEqual(self.lab.files(), self.lab.before)
        self.assertEqual(self.finished, [])
        json.dumps(report).encode("ascii")  # what main prints

    def test_a_plan_that_cannot_be_made_says_why(self):
        args = update.build_parser().parse_args(["--dry-run", "--json"])
        report = update.plan_report(
            self.root,
            args=args,
            check=self.check(updates.UpdateError("GitHub could not be reached (offline).")),
            runner=self.runner,
            processes=lambda root: [],
            fetch=self.fetch,
            interactive=False,
        )
        self.assertEqual(report, {"ok": False, "problem": "GitHub could not be reached (offline)."})

    def test_json_goes_only_with_a_dry_run(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            update.main(["--json"])

    def test_the_updater_waits_for_the_lab_to_close_updates_and_opens_it_again(self):
        python = str(self.root / ".venv/Scripts/python.exe")
        status, out = self.from_lab("--reopen", "window", running=[[(42, python)], [(42, python)], []])
        self.assertEqual(status, 0)
        self.assertEqual(self.lab.read("serve.py"), NEW["serve.py"])
        self.assertEqual(self.finished, [[sys.executable, str(self.root / "tools" / "update.py"), "--finish"]])
        self.assertEqual(self.opened, [self.root])
        self.assertEqual(self.waits, [0.5, 0.5, update.CLOSE_SECONDS])
        self.assertEqual(self.asked, [])
        self.assertIn("Waiting for the Lab to close", out)
        self.assertIn("The Lab is opening again.", out)
        self.assertIsNone(self.notice(), "the new version's --finish leaves the notice of an update that worked")

    def test_a_lab_that_does_not_close_is_left_alone(self):
        python = str(self.root / ".venv-kokoro/Scripts/python.exe")
        status, out = self.from_lab("--reopen", "window", running=[[(42, python)]])
        self.assertEqual(status, 1)
        self.assertEqual(self.lab.files(), self.lab.before)
        self.assertEqual((self.finished, self.opened, self.asked), ([], [], [True]))
        notice = self.notice()
        self.assertEqual(notice["state"], "failed")
        self.assertIn("The Lab did not close within 2 minutes, so nothing was changed.", notice["message"])
        self.assertIn("then press Update now again", notice["message"])

    def test_a_failed_download_opens_the_old_version_and_says_why(self):
        status, out = self.from_lab("--reopen", "window", fetch=lambda url, destination: None)
        self.assertEqual(status, 1)
        self.assertEqual(self.lab.files(), self.lab.before)
        self.assertEqual(self.opened, [self.root])
        self.assertEqual(self.asked, [True])
        notice = self.notice()
        self.assertEqual((notice["state"], notice["from"], notice["to"]), ("failed", CURRENT, "0.2.0"))
        self.assertEqual(notice["message"], "GitHub has no source ZIP for v0.2.0. Nothing was changed; try again later.")
        self.assertIn("Press Enter to close this window.", out)

    def test_an_error_nobody_expected_is_said_and_kept_not_raised(self):
        def broken(url, destination):
            raise RuntimeError("disk on fire")

        status, out = self.from_lab(fetch=broken)
        self.assertEqual(status, 1)
        self.assertIn("The update stopped: RuntimeError: disk on fire.", self.notice()["message"])
        self.assertIn("Start the Lab again: run serve.bat", out)

    def test_nothing_newer_is_said_once_on_the_page(self):
        status, out = self.from_lab(check=self.check(release(f"v{CURRENT}")))
        self.assertEqual(status, 0)
        self.assertEqual(self.notice()["state"], "current")
        self.assertIn("newest version", self.notice()["message"])
        self.assertEqual(self.lab.files(), self.lab.before)

    def test_the_lab_is_opened_with_its_shortcuts_python_and_not_where_there_is_none(self):
        started = []
        self.assertFalse(update.open_lab(self.root, starter=lambda *a, **k: started.append((a, k))))
        window = self.root / ".venv-desktop/Scripts/pythonw.exe"
        window.parent.mkdir(parents=True)
        window.write_bytes(b"")
        self.assertTrue(update.open_lab(self.root, starter=lambda *a, **k: started.append((a, k))))
        (command,), options = started[0]
        self.assertEqual(command, [str(window), str(self.root / "desktop.py")])
        self.assertEqual(options["cwd"], self.root)

    def test_every_model_the_update_names_has_its_install_button(self):
        installers = engine_adapters.table("guided_installer")
        self.assertEqual(set(update.MODEL_ENGINES), set(update.MODELS))
        for name, engine in update.MODEL_ENGINES.items():
            self.assertEqual(installers[engine], update.MODELS[name][1], name)


class FinishNoticeTests(unittest.TestCase):
    def test_finish_leaves_a_notice_naming_each_model_to_install_again(self):
        lab = ModelLab(self.enterContext(tempfile.TemporaryDirectory()))
        lab.install("Kokoro", "cpu")
        lab.install("Phonon", old=True)
        lab.finish("requirements/kokoro-cpu.txt", "catalog/phonon_snapshot.json")
        notice = json.loads(update.notice_path(lab.root).read_text(encoding="utf-8"))
        self.assertEqual(
            {key: notice[key] for key in ("schema", "state", "from", "to", "problems")},
            {"schema": 1, "state": "updated", "from": "0.1.0", "to": "0.2.0", "problems": []},
        )
        self.assertEqual(notice["release_url"], "https://github.com/Fel101/fels-voice-lab/releases")
        self.assertEqual(
            [(item["models"], item["folder"]) for item in notice["advice"]],
            [
                ([{"engine": "kokoro", "name": "Kokoro"}], ".venv-kokoro"),
                ([{"engine": "phonon-31m-en", "name": "Phonon"}], None),
            ],
        )
        self.assertTrue(notice["advice"][0]["text"].startswith("Kokoro's pinned packages changed"))
        self.assertNotIn(update.KEEP, json.dumps(notice, ensure_ascii=False))


class Recorder:
    """A stand-in for subprocess.run and Popen that keeps what it was asked."""

    def __init__(self, stdout="", returncode=0, error=None):
        self.calls, self.stdout, self.returncode, self.error = [], stdout, returncode, error

    def __call__(self, command, **options):
        self.calls.append((command, options))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "pip: something broke\n")


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_the_plan_is_update_pys_last_line_of_json(self):
        runner = Recorder(stdout='noise\n{"ok": true, "newer": false, "message": "This is the newest version, 0.4.0."}\n')
        updater = self_update.SelfUpdate(self.root, python="py.exe", runner=runner)
        self.assertEqual(updater.plan()["message"], "This is the newest version, 0.4.0.")
        command, options = runner.calls[0]
        self.assertEqual(command, ["py.exe", str(self.root / "tools" / "update.py"), "--dry-run", "--json"])
        self.assertEqual(options["timeout"], self_update.PLAN_SECONDS)

    def test_an_updater_that_says_nothing_or_does_not_start_is_explained(self):
        updater = self_update.SelfUpdate(self.root, runner=Recorder(stdout="", returncode=3))
        with self.assertRaisesRegex(self_update.UpdateNowError, r"exit code 3\). pip: something broke Run update.bat"):
            updater.plan()
        updater = self_update.SelfUpdate(self.root, runner=Recorder(error=subprocess.TimeoutExpired("update.py", 300)))
        with self.assertRaisesRegex(self_update.UpdateNowError, "longer than 5 minutes"):
            updater.plan()

    def test_a_service_started_by_serve_bat_starts_the_updater_itself_and_then_stops(self):
        starter, stopped, said = Recorder(), threading.Event(), []
        updater = self_update.SelfUpdate(self.root, starter=starter, say=said.append)
        self.assertEqual(updater.start(stopped.set), {"how": "console"})
        self.assertTrue(stopped.wait(5))
        self.assertEqual(said, [self_update.CLOSING_FOR_CONSOLE])
        self.assertIn("start the Lab again with serve.bat", said[0])
        command, options = starter.calls[0]
        self.assertEqual(command, self_update.updater_command(self.root, "none"))
        self.assertEqual(command[2:], ["--from-lab", "--reopen", "none"])
        self.assertEqual(options["creationflags"], self_update.NEW_CONSOLE)
        with self.assertRaisesRegex(self_update.UpdateNowError, "already started"):
            updater.start(stopped.set)

    def test_a_service_the_window_started_leaves_the_updater_to_the_window(self):
        session = self.root / "out/desktop/session-1"
        session.mkdir(parents=True)
        starter, stopped, said = Recorder(), threading.Event(), []
        updater = self_update.SelfUpdate(self.root, starter=starter, say=said.append)
        updater.desktop_session = session
        self.assertFalse(self_update.requested(session))
        self.assertEqual(updater.start(stopped.set), {"how": "window"})
        self.assertTrue(stopped.wait(5))
        self.assertEqual(starter.calls, [])
        self.assertEqual(said, [])  # the window's service has no console anyone sees
        self.assertTrue(self_update.requested(session))

    def test_the_notice_is_read_once_and_forgotten(self):
        updater = self_update.SelfUpdate(self.root)
        self.assertIsNone(updater.notice())
        path = self.root / self_update.NOTICE
        path.parent.mkdir(parents=True)
        path.write_text('{"schema": 2, "state": "updated"}', encoding="utf-8")
        self.assertIsNone(updater.notice(), "a notice from a later version is not guessed at")
        update.save_notice(self.root, {"state": "failed", "from": "0.4.0", "message": "Nothing was changed."})
        self.assertEqual(updater.notice()["message"], "Nothing was changed.")
        updater.dismiss()
        updater.dismiss()
        self.assertIsNone(updater.notice())


class StandIn:
    """What a route needs of SelfUpdate."""

    how = "console"

    def __init__(self):
        self.started, self.dismissed = [], 0

    def plan(self):
        return {"ok": True, "newer": True, "from": "0.4.0", "to": "0.4.1", "steps": [], "notes": []}

    def start(self, stop):
        self.started.append(stop)
        return {"how": self.how}

    def notice(self):
        return {"schema": 1, "state": "updated", "to": "0.4.1"}

    def dismiss(self):
        self.dismissed += 1

    def open_folder(self):
        return {"folder": "C:\\Lab"}


class RouteTests(unittest.TestCase):
    def setUp(self):
        from voice_lab import serve
        from voice_lab.cast_store import CastStore

        temp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.updater = StandIn()
        self.install = None

        class Installer:
            def snapshot(installer, kind="engine"):
                return self.install if kind == "engine" else None

        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        handler = serve.make_handler(
            types.SimpleNamespace(entries={}, gpu_install=None),
            server.server_address[1],
            CastStore(temp),
            engine_installer=Installer(),
            self_update=self.updater,
        )
        server.RequestHandlerClass = handler
        self.scenes = handler.lab.scenes
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def ask(self, path, body=None):
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_the_plan_and_the_start(self):
        status, answer = self.ask("/api/update/plan", {})
        self.assertEqual((status, answer["blocked"], answer["how"], answer["plan"]["to"]), (200, None, "console", "0.4.1"))
        status, answer = self.ask("/api/update/start", {})
        self.assertEqual((status, answer), (202, {"how": "console"}))
        self.assertEqual(len(self.updater.started), 1)

    def test_an_install_or_a_rendering_batch_holds_the_update_with_the_reason(self):
        self.install = {"name": "Kokoro 82M", "state": "running"}
        status, answer = self.ask("/api/update/plan", {})
        self.assertEqual(answer["blocked"], "Kokoro 82M is installing. Update now works once it has finished.")
        self.assertEqual(self.ask("/api/update/start", {}), (409, {"error": answer["blocked"], "code": "busy"}))
        self.install = None
        with mock.patch.object(self.scenes, "rendering_jobs", return_value=1):
            status, answer = self.ask("/api/update/start", {})
        self.assertEqual(status, 409)
        self.assertIn("A scene batch is rendering", answer["error"])
        self.assertEqual(self.updater.started, [])

    def test_the_notice_its_dismissal_and_the_folder(self):
        self.assertEqual(self.ask("/api/update/notice"), (200, {"notice": {"schema": 1, "state": "updated", "to": "0.4.1"}}))
        self.assertEqual(self.ask("/api/update/notice/dismiss", {}), (200, {"ok": True}))
        self.assertEqual(self.updater.dismissed, 1)
        self.assertEqual(self.ask("/api/system/open-folder", {}), (200, {"folder": "C:\\Lab"}))


class SceneRenderingTests(unittest.TestCase):
    def test_only_batches_that_render_or_wait_to_count(self):
        from voice_lab.cast_store import CastStore
        from voice_lab.scene_store import SceneStore

        temp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        store = SceneStore(CastStore(temp), {}, root=temp)
        self.assertEqual(store.rendering_jobs(), 0)
        with store._connect() as db:
            db.execute("PRAGMA foreign_keys=OFF")  # jobs with no scene behind them: only their status is counted here
            for number, status in enumerate(("queued", "running", "paused", "completed", "failed", "cancelled", "running")):
                db.execute(
                    "INSERT INTO jobs (id,scene_id,scene_revision,scene_checksum,scene_snapshot_json,status,created_at,"
                    "updated_at) VALUES (?,?,1,'',?,?,'','')",
                    (f"job-{number}", "scene", "{}", status),
                )
        self.assertEqual(store.rendering_jobs(), 3)


class ServeBatTests(unittest.TestCase):
    def test_serve_bat_runs_the_service_in_a_block_cmd_has_read_before_python_starts(self):
        """Update now can replace serve.bat while it waits at its pause; cmd.exe reads a batch file as it goes."""
        data = (ROOT / "serve.bat").read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "batch files keep CRLF line ends")
        lines = [line.strip() for line in data.decode("ascii").splitlines()]
        start = lines.index("(")
        self.assertEqual(lines[start:], ["(", '".venv\\Scripts\\python.exe" serve.py --port 8765 %*', "pause", ")"])

    @unittest.skipUnless(sys.platform == "win32", "cmd.exe runs batch files on Windows")
    def test_serve_bat_still_passes_its_arguments_to_the_service(self):
        with tempfile.TemporaryDirectory() as temp:
            lab = Path(temp)
            (lab / ".venv" / "Scripts").mkdir(parents=True)
            launcher = (ROOT / "serve.bat").read_text(encoding="ascii")
            (lab / "serve.bat").write_text(
                launcher.replace('".venv\\Scripts\\python.exe"', f'"{sys.executable}"'), encoding="ascii", newline=""
            )
            # A stand-in service that writes down what it was started with.
            (lab / "serve.py").write_text(
                "import sys, pathlib\npathlib.Path('args.txt').write_text(' '.join(sys.argv[1:]))\n", encoding="utf-8"
            )
            done = subprocess.run(
                ["cmd", "/c", str(lab / "serve.bat"), "--device", "cpu"], stdin=subprocess.DEVNULL, capture_output=True
            )
            self.assertEqual(done.returncode, 0)
            self.assertEqual((lab / "args.txt").read_text(), "--port 8765 --device cpu")


class DesktopHandOverTests(unittest.TestCase):
    def setUp(self):
        from voice_lab import desktop

        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.backend = desktop.DesktopBackend(root=self.root)
        self.backend.session = self.root / "out/desktop/session-1"
        self.backend.session.mkdir(parents=True)

    def test_the_window_closes_and_starts_the_updater_only_when_the_service_asked(self):
        closed, starter = [], Recorder()
        self.backend.process = types.SimpleNamespace(wait=lambda: 0)
        self.backend.wait_for_update(lambda: closed.append(True))
        self.assertFalse(self.backend.hand_over_update(starter=starter))
        self.assertEqual((closed, starter.calls), ([], []))
        (self.backend.session / self_update.REQUEST).write_text("{}", encoding="utf-8")
        self.backend.wait_for_update(lambda: closed.append(True))
        self.assertTrue(self.backend.hand_over_update(starter=starter))
        self.assertEqual(closed, [True])
        command, options = starter.calls[0]
        self.assertEqual(command, self_update.updater_command(self.backend.root, "window"))
        self.assertEqual(options["creationflags"], self_update.NEW_CONSOLE)


if __name__ == "__main__":
    unittest.main()
