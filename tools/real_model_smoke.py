"""Does a real model speak? Start the Lab's service, run each model's own Test, have it say one sentence, and check the clip.

    .venv\\Scripts\\python.exe tools\\real_model_smoke.py kokoro
    .venv\\Scripts\\python.exe tools\\real_model_smoke.py --all-installed

Nearly every other test runs without loading a model. This one loads them. For each model it:

1. presses the Models page's Test (POST /api/engines/{id}/check), which loads the model and speaks a short check;
2. asks the loaded model for its sample rate (POST /api/load answers at once for a model already loaded);
3. has it say SENTENCE through POST /api/speak, with the request the Test itself uses for that model;
4. checks the WAV: at the model's sample rate, about as long as SENTENCE takes to say, audible and not clipped by the Lab's
   own waveform checks (engine_checks.audible and evaluation_diagnostics.take_checks), and labelled inside the file as
   AI-generated speech made by that model (synthetic_label);
5. unloads it, so the next model loads on its own.

It prints a table and keeps each clip, report.json and the service's log in out\\smoke\\<date and time>\\, for a person to
listen to: a clip can pass every check and still not be speech. Close the Lab first. This starts a service of its own on a
free port, and two services would load models side by side. Test results are kept, as the Models page keeps them.

Exit status: 0 when every model passed; 1 when one did not; 2 for a mistake in the command line, or no model to try; 3 when
the service did not start.
"""

import argparse
import json
import math
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine_checks  # noqa: E402
import evaluation_diagnostics  # noqa: E402
import synthetic_label  # noqa: E402
from tools import lab_cli  # noqa: E402

SENTENCE = "Every model in the Lab should say this sentence clearly, at an even pace, and then stop."
# A clip of SENTENCE is as long as its words take at between WORDS_PER_SECOND words a second, with up to EDGE_SECONDS of
# quiet before and after them on top.
WORDS_PER_SECOND = (1.2, 6.0)
EDGE_SECONDS = 1.5
# What take_checks flags that fails a clip here. A long pause is shown but passes: a model may breathe at a comma.
FAILING = {
    "silent_audio": "it is silent",
    "clipping": "it is clipped (samples at full scale)",
    "duration_suspiciously_short": "it is too short for its words",
}
# Rows in GET /api/engines/status that --all-installed leaves out: no model to try, or one that converts and never speaks.
NOT_TRIED = {"not_integrated", "not_installed", "conversion_task"}
SERVICE_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
START_SECONDS = 180
CHECK_SECONDS = 1800  # a first load can read gigabytes from disk
SPEAK_SECONDS = 900
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Usage(Exception):
    """A mistake in the command line, or nothing to try."""


class NotStarted(Exception):
    """The service did not start."""


def expected_seconds(text=SENTENCE):
    """The shortest and longest a clip of text may be."""
    words = len(text.split())
    return words / WORDS_PER_SECOND[1], words / WORDS_PER_SECOND[0] + 2 * EDGE_SECONDS


def choose(status, names, *, all_installed):
    """The models to try, from GET /api/engines/status: those named, or every installed one that speaks."""
    rows = {row["id"]: row for row in status.get("engines", [])}
    if all_installed:
        return [engine for engine, row in rows.items() if row.get("state") not in NOT_TRIED]
    unknown = [name for name in names if name not in rows]
    if unknown:
        raise Usage(f"Unknown model: {', '.join(unknown)}. The models are: {', '.join(rows)}.")
    converts = [name for name in names if rows[name].get("state") == "conversion_task"]
    if converts:
        raise Usage(f"{', '.join(converts)} converts recordings and does not speak text, so there is nothing to try.")
    return list(names)


def judge(path, *, engine, sample_rate, text=SENTENCE):
    """Check one clip. Returns its facts, `problems` (empty when it passes) and `notes` (shown, never failing)."""
    path = Path(path)
    facts = {"seconds": None, "sample_rate": None, "peak_dbfs": None, "label": None, "flags": [], "problems": [], "notes": []}
    try:
        with sf.SoundFile(path) as clip:
            kind, rate, label = clip.format, clip.samplerate, clip.comment
            samples = clip.read(dtype="float32", always_2d=True)
    except (RuntimeError, OSError) as exc:
        facts["problems"].append(f"the answer is not audio the Lab can read ({exc})")
        return facts
    seconds = samples.shape[0] / rate
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    facts.update(seconds=round(seconds, 2), sample_rate=rate, label=label,
                 peak_dbfs=round(20 * math.log10(peak), 1) if peak > 0 else None)  # fmt: skip
    problems = facts["problems"]
    if kind != "WAV":
        problems.append(f"it is {kind}, not WAV")
    if sample_rate is None:
        problems.append("the model did not say its sample rate")
    elif rate != sample_rate:
        problems.append(f"it is at {rate} Hz, not the model's {sample_rate} Hz")
    low, high = expected_seconds(text)
    if not low <= seconds <= high:
        problems.append(f"it lasts {seconds:.2f} s, outside {low:.1f} to {high:.1f} s for this sentence")
    heard = engine_checks.audible(samples[:, 0], rate) if samples.size else "it holds no samples"
    if heard:
        problems.append(heard)
    if samples.size:
        checks = evaluation_diagnostics.take_checks(path, expected_text=text)
        facts["flags"] = checks["flags"]
        problems.extend(words for flag, words in FAILING.items() if flag in checks["flags"])
        if "long_pause" in checks["flags"]:
            facts["notes"].append(f"a pause of {checks['longest_pause_seconds']:.1f} s inside it")
    name = synthetic_label.engine_name(engine)
    if not synthetic_label.is_labelled(label):
        problems.append("the file carries no AI-generated speech label")
    elif f" made by {name} with " not in label:
        problems.append(f"its label does not name {name}")
    return facts


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Service:
    """serve.py on a free port, with its output in folder/service.log. It stops with this process (--parent-pid)."""

    def __init__(self, folder, *, device=None, python=None):
        self.folder, self.device = Path(folder), device
        self.python = python or (SERVICE_PYTHON if SERVICE_PYTHON.is_file() else Path(sys.executable))
        self.process, self.base = None, None

    def start(self):
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        command = [str(self.python), str(ROOT / "serve.py"), "--port", str(port), "--parent-pid", str(os.getpid()),
                   "--idle-minutes", "0", *(["--device", self.device] if self.device else [])]  # fmt: skip
        self.log = self.folder / "service.log"
        with self.log.open("wb") as log:
            self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                            creationflags=NO_WINDOW)  # fmt: skip
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise NotStarted(f"The service stopped as it started (exit code {self.process.returncode}). See {self.log}.")
            try:
                return lab_cli.get_json(self.base, "/api/health", 5)
            except lab_cli.Failed:
                time.sleep(1)
        raise NotStarted(f"The service did not answer within {START_SECONDS} s. See {self.log}.")

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        try:
            lab_cli.call(self.base, "POST", "/api/unload", timeout=120)  # the models' workers end with their models
        except lab_cli.Failed:
            pass
        self.process.terminate()
        try:
            self.process.wait(30)
        except subprocess.TimeoutExpired:
            self.process.kill()


def post_json(base, path, body=None, *, timeout):
    return json.loads(lab_cli.call(base, "POST", path, body=body, timeout=timeout)[1].decode("utf-8"))


def try_model(base, engine, folder):
    """Test one model, have it speak, and judge the clip. Returns its row of the report."""
    row = {"engine": engine, "name": synthetic_label.engine_name(engine), "test": None, "ran_on": None, "load_seconds": None,
           "speak_seconds": None, "clip": None, "seconds": None, "sample_rate": None, "peak_dbfs": None, "flags": [],
           "problems": [], "notes": []}  # fmt: skip
    try:
        check = post_json(base, f"/api/engines/{engine}/check", timeout=CHECK_SECONDS)["check"]
    except lab_cli.Failed as exc:
        check = {"status": "failed", "reason": str(exc)}
    row.update(test=check.get("status"), ran_on=check.get("ran_on"), load_seconds=check.get("load_seconds"))
    if check.get("status") == "not_installed":
        missing = ", ".join(check.get("missing") or []) or "its files"
        row["problems"].append(f"it is not installed: {missing} not found")
        return row
    if check.get("status") != "passed":
        row["problems"].append(f"its Test failed: {check.get('reason') or 'no reason given'}")
    try:
        loaded = post_json(base, "/api/load", {"engine": engine}, timeout=CHECK_SECONDS)
        request = {**engine_checks.smoke_request(engine, loaded, root=ROOT), "text": SENTENCE}
        headers, data = lab_cli.call(base, "POST", "/api/speak", body=request, timeout=SPEAK_SECONDS)
    except (lab_cli.Failed, ValueError) as exc:
        row["problems"].append(f"it did not speak: {exc}")
        return row
    clip = folder / f"{engine}.wav"
    clip.write_bytes(data)
    verdict = judge(clip, engine=engine, sample_rate=loaded.get("sample_rate"))
    row["problems"].extend(verdict.pop("problems"))
    row.update(verdict, clip=clip.name, speak_seconds=_number(headers.get("x-gen-seconds")))
    return row


def _number(text):
    try:
        return round(float(text), 2)
    except (TypeError, ValueError):
        return None


def _cell(value, digits=1):
    if value is None:
        return "-"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def table(rows):
    """The report as text: one line per model, then what failed and what was noticed."""
    head = ("Model", "Test", "Ran on", "Load s", "Speak s", "Clip s", "Hz", "Peak dBFS", "Result")
    lines = [[row["name"], row["test"] or "-", row["ran_on"] or "-", _cell(row["load_seconds"]),
              _cell(row["speak_seconds"]), _cell(row["seconds"], 2), _cell(row["sample_rate"]), _cell(row["peak_dbfs"]),
              "failed" if row["problems"] else "passed"] for row in rows]  # fmt: skip
    widths = [max(len(str(item[column])) for item in [head, *lines]) for column in range(len(head))]
    text = ["  ".join(str(item).ljust(width) for item, width in zip(cells, widths)).rstrip() for cells in [head, *lines]]
    for row in rows:
        text.extend(f"{row['name']}: {problem.rstrip('.')}." for problem in row["problems"])
        text.extend(f"{row['name']} (noted, not failing): {note.rstrip('.')}." for note in row["notes"])
    return "\n".join(text)


def run(args, *, out=ROOT / "out" / "smoke", service_factory=Service, say=print, now=datetime.now):
    folder = Path(out) / now().strftime("%Y-%m-%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    service = service_factory(folder, device=args.device)
    try:
        say(f"Starting the Lab's service (its log: {folder / 'service.log'}) ...")
        health = service.start()
        status = lab_cli.get_json(service.base, "/api/engines/status", 60)
        engines = choose(status, args.models, all_installed=args.all_installed)
        if not engines:
            raise Usage("No model is installed. Install one from the Models page, or name one to see why it fails.")
        rows = []
        for engine in engines:
            say(f"{synthetic_label.engine_name(engine)}: Test, then the sentence ...")
            rows.append(try_model(service.base, engine, folder))
            try:
                lab_cli.call(service.base, "POST", "/api/unload", timeout=120)
            except lab_cli.Failed:
                pass
    finally:
        service.stop()
    report = {"made_at": now().isoformat(timespec="seconds"), "version": health.get("version"),
              "device": health.get("device"), "sentence": SENTENCE, "models": rows}  # fmt: skip
    (folder / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    say("")
    say(table(rows))
    say("")
    say(f"The clips are in {folder}. Listen to them: a clip can pass every check and still not be speech.")
    return 1 if any(row["problems"] for row in rows) else 0


def build_parser():
    parser = argparse.ArgumentParser(description="Load real models, have each say one sentence, and check the clips.")
    parser.add_argument("models", nargs="*", metavar="MODEL", help="model ids, such as kokoro (GET /api/catalog lists them)")
    parser.add_argument("--all-installed", action="store_true", help="every installed model that speaks")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), help="the service's --device (default: its own, auto)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.models) == args.all_installed:
        parser.error("name the models to try, or give --all-installed")
    try:
        return run(args)
    except Usage as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (NotStarted, lab_cli.Failed) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
