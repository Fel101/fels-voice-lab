"""The engine adapters (engine_adapters/): each family imports in the service's environment, every engine it claims is in the
catalog, a new engine takes one family module and one catalog entry, and the shared code names no more engines than it did.
The counts may only go down: when the shared code stops naming an engine somewhere, lower the limit to the new count.

What still names one: the page's scripts, whose Node tests run a page function on its own, such as bodyForRequest, with an
engine id and no catalog; Kokoro's installed-voice lookups in serve.py and engine_checks.py, which tests stand in for there;
VoxCPM2's import path in engine_worker.py, set before anything else is imported; the recorder import's default model; an
evaluation suite written for Drama 3; and examples in help text."""

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine_adapters  # noqa: E402
from engine_adapters.base import Adapter  # noqa: E402
from engine_checks import CHECK_TEXT  # noqa: E402

# Where an engine may be named besides the adapters, the installers (tools/), the tests, the docs and the catalog: its family's
# own modules, which read its files, hold its model code or run in its worker, and the route table, whose examples name them.
FAMILY_MODULES = {
    "breeze_gen.py",
    "breeze_worker.py",
    "chatterbox_support.py",
    "chatterbox_vc_worker.py",
    "drama3_lab_studio.py",
    "drama3_production.py",
    "drama3_runtime.py",
    "drama3_worker.py",
    "engines.py",
    "fqwen_compat.py",
    "kokoro_support.py",
    "kokoro_timing.py",
    "phonon_support.py",
    "qwen_download.py",
    "qwen_support.py",
    "qwen_worker.py",
    "voxcpm_worker.py",
    "api_spec.py",
}
# The families' constants for their engine ids, which name an engine as surely as its id in quotes.
CONSTANTS = (
    "PHONON_ENGINE_ID",
    "TURBO_ENGINE_ID",
    "ORIGINAL_ENGINE_ID",
    "VC_ENGINE_ID",
    "CHATTERBOX_VC_ENGINE_ID",
    "VOXCPM_ENGINE_ID",
    "QWEN_ENGINE_IDS",
    "QWEN_MODELS",
    "QWEN_MODES",
)
# How many times the shared Python, and the page's scripts, name an engine. Each may only go down.
PYTHON_LIMIT = 9
SCRIPTS_LIMIT = 101


def catalog_ids():
    catalog = json.loads((ROOT / "catalog" / "engines.json").read_text(encoding="utf-8"))
    return [entry["id"] for entry in catalog["engines"]]


def shared_files():
    """The top-level modules and page scripts other than the families' own, and the route groups (route_groups/)."""
    top = [path for path in sorted(ROOT.glob("*.py")) + sorted(ROOT.glob("*.js")) if path.name not in FAMILY_MODULES]
    return top + sorted(ROOT.glob("route_groups/*.py"))


def names(text):
    """Each place a text names an engine: one of its ids in quotes, or a family's constant for its ids."""
    ids = sorted(catalog_ids(), key=len, reverse=True)
    quoted = re.compile(r"""(['"])(?:%s)\1""" % "|".join(re.escape(engine) for engine in ids))
    constants = re.compile(r"\b(?:%s)\b" % "|".join(CONSTANTS))
    return len(quoted.findall(text)) + len(constants.findall(text))


class EngineAdapterTests(unittest.TestCase):
    def test_every_adapter_is_for_an_engine_in_the_catalog_and_says_which(self):
        self.assertTrue(engine_adapters.ADAPTERS)
        for engine_id, adapter in engine_adapters.ADAPTERS.items():
            self.assertIsInstance(adapter, Adapter)
            self.assertEqual(adapter.engine_id, engine_id)
            self.assertIn(engine_id, catalog_ids())

    def test_the_adapters_import_without_any_model_package(self):
        # In a Python of their own, as other tests put stand-ins for model packages in this one's sys.modules. The service's
        # environment holds requirements/core.txt only, so a model package imported at the top of an adapter would break it.
        models = ("torch", "ptts", "kokoro", "chatterbox", "transformers", "faster_qwen3_tts", "voxcpm")
        script = f"import sys, engine_adapters; print(','.join(m for m in {models!r} if m in sys.modules))"
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "importing the adapters imported a model package")

    def test_the_shared_code_names_no_more_engines_than_before(self):
        for suffix, limit in ((".py", PYTHON_LIMIT), (".js", SCRIPTS_LIMIT)):
            with self.subTest(suffix):
                self.assert_at_most(suffix, limit)

    def assert_at_most(self, suffix, limit):
        counts = {path.name: names(path.read_text(encoding="utf-8")) for path in shared_files() if path.suffix == suffix}
        total = sum(counts.values())
        busiest = ", ".join(f"{name} {count}" for name, count in sorted(counts.items(), key=lambda item: -item[1])[:5] if count)
        self.assertLessEqual(
            total,
            limit,
            f"the shared code names an engine {total} times (most in {busiest}); ask the "
            "engine's adapter instead (engine_adapters/)",
        )
        # A ratchet: once the count goes down, the limit follows it, so it cannot creep back up.
        name = "PYTHON_LIMIT" if suffix == ".py" else "SCRIPTS_LIMIT"
        self.assertEqual(total, limit, f"the shared code names an engine {total} times now: lower {name} to {total}")


# A model that is not in the Lab: one family module (below) and one catalog entry (a copy of Kokoro's, renamed) are all it
# takes for the shared tables to list it and for EnginePool.speak to speak with it.
PRETEND = """
import json
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
import engine_adapters
from engine_adapters.base import Adapter


class Pretend(Adapter):
    engine_id = "pretend-1"
    guided_installer = "setup-pretend.bat"
    installer = "tools/install_pretend.py"
    required_packages = ("pretend",)
    min_free_ram_gb = 1
    uses_torch = False
    cpu_only = "Pretend runs on the CPU."
    languages = frozenset({"English"})
    performance_defaults = {"speed": 1.0}
    control_ui = {"speed": ("Speed", 0.5, 2.0, 0.05)}
    output_license = {"code_license": "none", "weights_license": "none", "output_use": "none", "status": "unknown",
                      "evidence": "a test"}
    generation_mode = "fixed_voice"

    def check_speak(self, req, pool):
        if req.get("voice", "Pretend voice") != "Pretend voice":
            raise ValueError("Pretend has one voice.")

    def speak_params(self, req, pool, worker, checked):
        return {"voice": "Pretend voice", "speed": req.get("speed", 1.0)}

    def smoke_request(self, base, loaded=None, root=None):
        return {**base, "voice": "Pretend voice"}


family = types.ModuleType("pretend_family")
family.ADAPTERS = {"pretend-1": Pretend()}
sys.modules["pretend_family"] = family
engine_adapters.FAMILIES += ("pretend_family",)
engine_adapters.ADAPTERS = engine_adapters._load()

import cast_store, engine_checks, engine_devices, engine_install, output_license, performance_settings, ram_needs
import scene_store, worker_pool, worker_runtime
from worker_pool import EnginePool, read_catalog

catalog = read_catalog()
kokoro = next(entry for entry in catalog["engines"] if entry["id"] == "kokoro")
catalog["engines"].append({**kokoro, "id": "pretend-1", "name": "Pretend 1"})


class Worker:
    def __init__(self):
        self.calls, self.alive, self.pid = [], False, None
        self.log_path = Path(tempfile.gettempdir()) / "voice-lab-pretend.log"

    def start(self):
        self.alive = True

    def close(self, force=False):
        self.alive = False

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "load":
            return {"model_loaded": True, "sample_rate": 24000, "load_seconds": 0.1, "pid": 4242}
        if method == "generate":
            output = Path(params["output_file"])
            output.parent.mkdir(parents=True, exist_ok=True)
            np.save(output, (0.2 * np.sin(np.arange(2400, dtype=np.float32) * 0.05)).astype(np.float32), allow_pickle=False)
            return {"audio_file": str(output), "sample_rate": 24000, "sample_count": 2400, "info": {}}
        if method == "unload":
            return {"model_loaded": False}
        raise AssertionError(method)


with tempfile.TemporaryDirectory() as folder:
    worker_pool.OUT = Path(folder)
    worker = Worker()
    pool = EnginePool("cpu", 0, worker_factory=lambda entry: worker, idle_poll_seconds=0.1, catalog=catalog,
                      install_probe=lambda entry: {"installed": True, "missing": [], "environment": "", "checkpoint": ""})
    try:
        pool.speak({"engine": "pretend-1", "text": "Hello there."})
        refused = None
        try:
            pool.speak({"engine": "pretend-1", "text": "Hello there.", "voice": "Someone else"})
        except ValueError as exc:
            refused = str(exc)
    finally:
        pool.close()
generated = [params for method, params in worker.calls if method == "generate"]
print(json.dumps({
    "ram": ram_needs.MIN_FREE_RAM_GB.get("pretend-1"),
    "worker": "pretend-1" in worker_runtime.ENGINE_IDS,
    "torch": "pretend-1" in worker_runtime.TORCH_ENGINE_IDS,
    "launcher": engine_checks.GUIDED_INSTALLERS.get("pretend-1"),
    "installer": engine_install.INSTALLERS.get("pretend-1"),
    "packages": list(engine_checks.REQUIRED_PACKAGES.get("pretend-1", ())),
    "cpu_only": engine_devices.CPU_ONLY.get("pretend-1"),
    "licence": output_license.ENGINE_LICENSE_NOTES.get("pretend-1", {}).get("evidence"),
    "defaults": performance_settings.DEFAULTS.get("pretend-1"),
    "controls": [control["id"] for control in performance_settings.performance_controls("pretend-1", kokoro["capabilities"])["controls"]],
    "languages": sorted(scene_store.SUPPORTED_LANGUAGES.get("pretend-1", ())),
    "test": engine_checks.smoke_request("pretend-1"),
    "spoke": [{key: params.get(key) for key in ("text", "voice", "speed")} for params in generated],
    "refused": refused,
}))
"""


class PretendEngineTests(unittest.TestCase):
    def test_a_new_model_takes_one_family_module_and_one_catalog_entry(self):
        # In a Python of its own, as adding a family changes the tables the shared modules build when they are imported.
        result = subprocess.run(
            [sys.executable, "-c", PRETEND, str(ROOT)], cwd=ROOT, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(
            seen,
            {
                "ram": 1,
                "worker": True,
                "torch": False,
                "launcher": "setup-pretend.bat",
                "installer": "tools/install_pretend.py",
                "packages": ["pretend"],
                "cpu_only": "Pretend runs on the CPU.",
                "licence": "a test",
                "defaults": {"speed": 1.0},
                "controls": ["speed"],
                "languages": ["English"],
                "test": {"engine": "pretend-1", "text": CHECK_TEXT, "voice": "Pretend voice"},
                "spoke": [{"text": "Hello there.", "voice": "Pretend voice", "speed": 1.0}],
                "refused": "Pretend has one voice.",
            },
        )


if __name__ == "__main__":
    unittest.main()
