"""tools/first_setup.py, which setup.bat runs once the core is in place: the desktop window, then the speech models chosen.
The installers, the desktop setup and the questions are stand-ins, so nothing is installed."""

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import first_setup as fs  # noqa: E402


class Installers:
    """Stands in for subprocess.run: a dry run prints a plan and passes, a real run succeeds, unless told otherwise."""

    def __init__(self, failing_plan=None, failing_install=None):
        self.commands, self.failing_plan, self.failing_install = [], failing_plan, failing_install

    def __call__(self, command, **kwargs):
        script = Path(command[1]).name
        self.commands.append([script, *command[2:]])
        if "--dry-run" in command:
            code = 1 if script == self.failing_plan else 0
            done = "Checks passed. This was a dry run, so nothing was changed." if not code else "error: the path is too long"
            return subprocess.CompletedProcess(command, code, f"The plan of {script}\n{done}\n", "")
        return subprocess.CompletedProcess(command, 1 if script == self.failing_install else 0)

    def runs(self, option):
        return [command[0] for command in self.commands if option in command]


class FirstSetupTests(unittest.TestCase):
    def setUp(self):
        self.installers = Installers()
        self.desktop_calls = 0
        self.desktop_ok = True
        self.installed = set()
        self.questions = []
        self.answers = []

    def desktop(self):
        self.desktop_calls += 1
        return self.desktop_ok

    def probe(self, entry, root):
        return {"installed": entry["id"] in self.installed}

    def ask(self, question):
        self.questions.append(question)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def run_setup(self, *argv, interactive=True, nvidia=False, total_ram=32.0):
        args = fs.build_parser().parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = fs.run(
                args,
                ask=self.ask,
                interactive=interactive,
                runner=self.installers,
                probe=self.probe,
                ram=lambda: {"total_gb": total_ram, "free_gb": total_ram / 2},
                nvidia_present=lambda: nvidia,
                desktop=self.desktop,
            )
        return status, " ".join(out.getvalue().split())

    def test_the_recommended_models_are_planned_together_then_installed_one_after_another(self):
        status, out = self.run_setup("--models", "recommended", "--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.desktop_calls, 1)
        self.assertEqual(
            [command[0] for command in self.installers.commands],
            ["install_kokoro.py", "install_phonon.py", "install_kokoro.py", "install_phonon.py"],
        )
        self.assertEqual(self.installers.runs("--dry-run"), ["install_kokoro.py", "install_phonon.py"])
        self.assertEqual(self.installers.runs("--yes"), ["install_kokoro.py", "install_phonon.py"])
        self.assertIn("The plan of install_kokoro.py", out)
        self.assertNotIn("This was a dry run", out, "the installers' dry-run endings would only confuse here")
        self.assertIn("Start the Lab with the Fel's Voice Lab icon on your Desktop.", out)

    def test_every_installer_runs_with_the_labs_own_python(self):
        self.run_setup("--models", "all", "--yes")
        command = fs.installer_command("kokoro", "--yes")
        self.assertEqual(command[:2], [sys.executable, str(ROOT / "tools" / "install_kokoro.py")])
        self.assertEqual(self.installers.runs("--yes"), ["install_kokoro.py", "install_phonon.py", "install_chatterbox.py"])

    def test_one_question_is_asked_before_anything_is_installed(self):
        self.answers = ["yes"]
        status, out = self.run_setup("--models", "kokoro", "phonon")
        self.assertEqual(status, 0)
        self.assertEqual(len(self.questions), 1)
        self.assertIn("Install kokoro and phonon now, as planned above?", self.questions[0])
        self.assertEqual(self.installers.runs("--yes"), ["install_kokoro.py", "install_phonon.py"])

    def test_declining_installs_nothing_and_says_how_to_add_them_later(self):
        self.answers = [""]
        status, out = self.run_setup("--models", "recommended")
        self.assertEqual(status, 0)
        self.assertEqual(self.installers.runs("--yes"), [])
        self.assertIn("No model was installed. Install them later from Models & features", out)

    def test_the_menu_offers_the_recommended_models_by_default(self):
        for answers, expected in (
            ([""], ["install_kokoro.py", "install_phonon.py"]),
            (["1"], ["install_kokoro.py", "install_phonon.py"]),
            (["nonsense", "2"], ["install_kokoro.py", "install_phonon.py", "install_chatterbox.py"]),
            (["3"], []),
        ):
            with self.subTest(answers=answers):
                self.installers.commands.clear()
                self.answers = list(answers) + ["yes"]
                status, out = self.run_setup()
                self.assertEqual(status, 0)
                self.assertEqual(self.installers.runs("--yes"), expected)
                self.assertIn("Which speech models should setup install now?", out)

    def test_a_closed_input_chooses_nothing(self):
        status, out = self.run_setup()
        self.assertEqual(status, 0)
        self.assertEqual(self.installers.commands, [])
        self.assertIn("It cannot speak until a model is installed", out)

    def test_the_menu_says_when_this_computer_is_too_small_for_cloning(self):
        small = fs.menu_text(8, fs.model_sizes())
        self.assertIn("This computer has 8 GB, which is probably too little for it.", " ".join(small.split()))
        self.assertNotIn("too little", fs.menu_text(32, fs.model_sizes()))
        self.assertNotIn("This computer has", fs.menu_text(None, fs.model_sizes()))

    def test_the_sizes_come_from_what_each_installer_plans_with(self):
        sizes = fs.model_sizes()
        self.assertEqual(set(sizes), {"kokoro", "phonon", "chatterbox"})
        for name, (download, disk) in sizes.items():
            with self.subTest(model=name):
                self.assertGreater(download, 0)
                self.assertGreater(disk, download / 2)
        self.assertLess(sizes["phonon"][1], sizes["kokoro"][1])
        self.assertLess(sizes["kokoro"][1], sizes["chatterbox"][1])
        self.assertTrue(fs.sizes_text(["kokoro"], sizes).startswith("About 0."))

    def test_models_already_installed_are_left_alone(self):
        self.installed = {"kokoro"}
        status, out = self.run_setup("--models", "recommended", "--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.installers.runs("--yes"), ["install_phonon.py"])
        self.assertIn("kokoro is already installed; setup leaves it as it is.", out)

    def test_an_installer_that_cannot_run_here_stops_setup_before_anything_is_installed(self):
        self.installers.failing_plan = "install_phonon.py"
        with self.assertRaisesRegex(fs.SetupStopped, "The phonon installer cannot run here yet"):
            self.run_setup("--models", "recommended", "--yes")
        self.assertEqual(self.installers.runs("--yes"), [])

    def test_an_installer_that_stops_part_way_says_to_run_setup_again(self):
        self.installers.failing_install = "install_kokoro.py"
        with self.assertRaisesRegex(fs.SetupStopped, "Run setup.bat again to continue"):
            self.run_setup("--models", "recommended", "--yes")
        self.assertEqual(self.installers.runs("--yes"), ["install_kokoro.py"])

    def test_without_a_terminal_it_installs_only_what_it_was_told_to_and_never_asks(self):
        status, out = self.run_setup(interactive=False)
        self.assertEqual((status, self.questions, self.installers.commands), (0, [], []))
        self.assertIn("Add --models recommended (or all) to install them.", out)
        with self.assertRaisesRegex(fs.SetupStopped, "Add --yes to accept the plans"):
            self.run_setup("--models", "kokoro", interactive=False)
        self.assertEqual(self.installers.runs("--yes"), [])

    def test_an_nvidia_computer_is_offered_the_gpu_builds_for_the_models_that_have_them(self):
        self.answers = ["2", "yes", "yes"]
        status, out = self.run_setup(nvidia=True)
        self.assertEqual(status, 0)
        self.assertIn("This computer has an NVIDIA graphics driver.", out)
        self.assertIn("has not been tried on an NVIDIA card yet", out)
        gpu = [command for command in self.installers.commands if "--nvidia" in command]
        self.assertEqual(sorted({command[0] for command in gpu}), ["install_chatterbox.py", "install_kokoro.py"])
        self.assertNotIn("--nvidia", next(command for command in self.installers.commands if command[0] == "install_phonon.py"))

    def test_the_gpu_builds_are_not_offered_without_an_nvidia_driver_and_can_be_asked_for(self):
        self.answers = ["1", "yes"]
        self.run_setup(nvidia=False)
        self.assertFalse(any("--nvidia" in command for command in self.installers.commands))
        self.installers.commands.clear()
        self.run_setup("--models", "kokoro", "--yes", "--nvidia")
        self.assertEqual([command for command in self.installers.commands if "--nvidia" in command][0][0], "install_kokoro.py")

    def test_a_desktop_window_that_cannot_be_set_up_does_not_stop_the_models(self):
        self.desktop_ok = False
        status, out = self.run_setup("--models", "phonon", "--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.installers.runs("--yes"), ["install_phonon.py"])
        self.assertIn("Start the Lab with serve.bat", out)
        self.desktop_calls = 0
        self.run_setup("--models", "none", "--no-desktop")
        self.assertEqual(self.desktop_calls, 0)

    def test_unknown_model_names_are_refused_by_name(self):
        self.assertEqual(fs.parse_models(["recommended", "chatterbox", "kokoro"]), ["kokoro", "phonon", "chatterbox"])
        self.assertEqual(fs.parse_models(["none"]), [])
        with self.assertRaisesRegex(fs.SetupStopped, "There is no model called 'qwen'"):
            fs.parse_models(["qwen"])

    def test_main_turns_a_stop_into_a_message_and_an_exit_status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(fs.main(["--models", "qwen", "--no-desktop"]), 1)
        self.assertIn("There is no model called 'qwen'", out.getvalue())

    def test_every_model_it_offers_is_a_guided_installer_of_the_catalog(self):
        import engine_checks
        import engine_install

        for name, model in fs.MODELS.items():
            with self.subTest(model=name):
                self.assertEqual(engine_install.INSTALLERS[model["engine"]], model["script"])
                self.assertIn(model["engine"], engine_checks.GUIDED_INSTALLERS)
        self.assertEqual({model["engine"] for model in fs.MODELS.values()}, set(engine_checks.GUIDED_INSTALLERS))


class SetupBatchTests(unittest.TestCase):
    def test_setup_bat_runs_the_core_then_the_first_setup_with_what_was_typed_after_it(self):
        data = (ROOT / "setup.bat").read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "batch files keep CRLF line ends")
        lines = [line.strip() for line in data.decode("ascii").splitlines()]
        core = lines.index("py -3.12 tools\\setup_core.py")
        self.assertEqual(lines[core + 1], "if errorlevel 1 goto done", "a core that failed is not followed by the rest")
        self.assertEqual(lines[core + 2], '".venv\\Scripts\\python.exe" tools\\first_setup.py %*')

    def test_the_first_setup_prints_only_plain_ascii(self):
        self.assertTrue((ROOT / "tools" / "first_setup.py").read_bytes().isascii())
        self.assertTrue(fs.menu_text(8, fs.model_sizes()).isascii())


if __name__ == "__main__":
    unittest.main()
