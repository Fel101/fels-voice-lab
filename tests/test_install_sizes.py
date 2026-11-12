"""The size of each model's Install, as the Models page and the first-run note show it before anyone presses Install."""

import json
import os
import sys
import tempfile
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine_install  # noqa: E402
import install_sizes  # noqa: E402
import serve  # noqa: E402
from chatterbox_support import TURBO_ENGINE_ID  # noqa: E402
from library_fixture import temp_cast  # noqa: E402
from tests.test_amd_build import R9700  # noqa: E402
from tools import amd_build as amd  # noqa: E402
from tools import first_setup  # noqa: E402
from tools import install_kokoro as ik  # noqa: E402
from worker_pool import EnginePool  # noqa: E402

RTX_5070 = {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": (12, 0), "driver": (617, 14), "memory_mib": 8151}


def row(engine, *, state="not_installed", installer="setup.bat", gpu_install=False):
    return {
        "id": engine,
        "state": state,
        "installation": {
            "installed": state != "not_installed",
            "missing": [],
            **({"installer": installer} if installer else {}),
        },
        "gpu": {"install": gpu_install},
    }


class ModelBytesTests(unittest.TestCase):
    def test_every_model_with_an_installer_has_a_size(self):
        for engine in engine_install.INSTALLERS:
            with self.subTest(engine=engine):
                download, disk = install_sizes.model_bytes(engine, ROOT)
                self.assertGreater(download, 100 * 10**6)
                self.assertGreaterEqual(disk, download)
        self.assertIsNone(install_sizes.model_bytes("not-a-model", ROOT))

    def test_the_size_is_the_installers_own_plan_for_the_build_it_puts_in(self):
        manifest = ik.load_manifest(ROOT / ik.MANIFEST)
        files = sum(item["size"] for item in ik.checkpoint_files(manifest, ik.choose_voices(manifest, ["supported"])))
        cpu = install_sizes.model_bytes("kokoro", ROOT)
        self.assertEqual(cpu, ((ik.PACKAGES_DOWNLOAD_MB * 10**6) + files, ik.PACKAGES_DISK_MB * 10**6 + files))
        nvidia = install_sizes.model_bytes("kokoro", ROOT, nvidia=True)
        self.assertEqual(nvidia[0] - cpu[0], ik.NVIDIA_EXTRA_DOWNLOAD_MB * 10**6)
        download, disk = amd.sizes_mb("gfx1201", ik.OTHER_DOWNLOAD_MB, ik.OTHER_DISK_MB)
        self.assertEqual(
            install_sizes.model_bytes("kokoro", ROOT, card=R9700), (download * 10**6 + files, disk * 10**6 + files)
        )
        # setup.bat's menu asks the same numbers
        self.assertEqual(first_setup.model_sizes()["kokoro"], cpu)
        self.assertEqual(first_setup.model_sizes()["chatterbox"], install_sizes.model_bytes(TURBO_ENGINE_ID, ROOT))

    def test_a_folder_whose_file_lists_cannot_be_read_gives_no_size(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(install_sizes.model_bytes("kokoro", Path(folder)))
            self.assertIsNone(install_sizes.size_fields(None))
            with self.assertRaisesRegex(ValueError, "could not be read from their installers' file lists"):
                first_setup.model_sizes(folder)
        self.assertEqual(install_sizes.size_fields((1, 2)), {"download_bytes": 1, "disk_bytes": 2})


class StatusTests(unittest.TestCase):
    def installer(self, *, present=False, cards=(), amd_cards=(), pool=None):
        return engine_install.EngineInstaller(
            pool,
            root=ROOT,
            nvidia_present=lambda: present,
            nvidia_cards=lambda: [dict(card) for card in cards],
            amd_listed=lambda: bool(amd_cards),
            amd_cards=lambda: [dict(card) for card in amd_cards],
        )

    def test_a_model_that_is_not_installed_gets_the_size_of_the_build_its_install_puts_in(self):
        rows = [
            row("kokoro"),
            row(TURBO_ENGINE_ID, gpu_install=True),
            row("phonon-31m-en", state="usable", installer=None),
            row("manual", installer=None),
        ]
        status = self.installer(present=True, cards=[RTX_5070]).with_sizes({"engines": [dict(item) for item in rows]})
        kokoro, turbo, phonon, manual = status["engines"]
        self.assertEqual(kokoro["install_size"], install_sizes.size_fields(install_sizes.model_bytes("kokoro", ROOT)))
        self.assertEqual(
            turbo["install_size"], install_sizes.size_fields(install_sizes.model_bytes(TURBO_ENGINE_ID, ROOT, nvidia=True))
        )
        self.assertNotIn("install_size", phonon)  # installed
        self.assertNotIn("install_size", manual)  # no Install on its card
        with mock.patch.dict(os.environ):
            os.environ.pop(amd.VARIABLE, None)
            on_amd = self.installer(amd_cards=[R9700]).with_sizes({"engines": [row(TURBO_ENGINE_ID, gpu_install=True)]})
        self.assertEqual(
            on_amd["engines"][0]["install_size"],
            install_sizes.size_fields(install_sizes.model_bytes(TURBO_ENGINE_ID, ROOT, card=R9700)),
        )

    def test_the_status_route_carries_the_sizes(self):
        pool = EnginePool("cpu", 0, worker_factory=lambda entry: None)
        lab = temp_cast(self)
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        server.RequestHandlerClass = serve.make_handler(
            pool, server.server_address[1], lab, engine_installer=self.installer(pool=pool)
        )
        thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/api/engines/status", timeout=20
            ) as answer:
                status = json.loads(answer.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            pool.close()
            thread.join(timeout=3)
        rows = {item["id"]: item for item in status["engines"]}
        missing = [
            key for key, item in rows.items() if item["state"] == "not_installed" and item["installation"].get("installer")
        ]
        for key in missing:
            self.assertIn("install_size", rows[key], key)
            self.assertGreater(rows[key]["install_size"]["download_bytes"], 0, key)


if __name__ == "__main__":
    unittest.main()
