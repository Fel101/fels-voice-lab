"""The engine adapters (engine_adapters/): each family imports in the service's environment, every engine it claims is in the
catalog, and the shared code names fewer engines than it did. The counts may only go down: when a family moves into its
adapter, lower PYTHON_LIMIT to the new count.

The page's scripts keep their count for now. The Node tests run a page function on its own, such as bodyForRequest, with an
engine id and no catalog, so moving the page onto the catalog's families and capabilities means changing those tests, which
step 15 of the plan leaves to the owner."""

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
PYTHON_LIMIT = 322
SCRIPTS_LIMIT = 101


def catalog_ids():
    catalog = json.loads((ROOT / "catalog" / "engines.json").read_text(encoding="utf-8"))
    return [entry["id"] for entry in catalog["engines"]]


def shared_files():
    return [path for path in sorted(ROOT.glob("*.py")) + sorted(ROOT.glob("*.js")) if path.name not in FAMILY_MODULES]


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


if __name__ == "__main__":
    unittest.main()
