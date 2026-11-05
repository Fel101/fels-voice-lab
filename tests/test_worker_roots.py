"""Exercise isolated audio roots across the real broker-to-worker process boundary."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cast_store import CastStore
from reference_paths import (REFERENCE_ROOTS_ENV, CONVERSION_SOURCE_ROOTS_ENV,
                             configured_worker_roots, resolve_reference_path)
from worker_pool import EnginePool
import serve


class IsolatedWorkerRootsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-root-regression-")
        self.root = Path(self.temp.name).resolve()
        self.refs = self.root / "data/cast/media/references"
        self.takes = self.root / "data/cast/media/takes"
        self.refs.mkdir(parents=True)
        self.takes.mkdir(parents=True)
        self.reference = self.refs / "reference.wav"
        self.source = self.takes / "take.wav"
        self.outside = self.root / "outside.wav"
        self.unrelated = self.root / "other-state/data/cast/media/references/audio.wav"
        self.unrelated.parent.mkdir(parents=True)
        for path in (self.reference, self.source, self.outside, self.unrelated):
            sf.write(path, np.zeros(24000 * 6, dtype=np.float32), 24000)
        self.pool = EnginePool("cpu", 0, reference_roots=(self.refs,), conversion_source_roots=(self.takes,))

    def tearDown(self):
        self.pool.close()
        self.temp.cleanup()

    def entry(self, engine):
        """The catalog entry, run by the core Python when the engine's own environment (such as .venv-chatterbox) is not here."""
        entry = dict(self.pool.entries[engine])
        if not (ROOT / entry['runtime']['python']).is_file():
            entry['runtime'] = {**entry['runtime'], 'python': sys.executable}
        return entry

    def test_launch_environment_overrides_inherited_roots(self):
        with patch.dict(os.environ, {REFERENCE_ROOTS_ENV:'["inherited"]', CONVERSION_SOURCE_ROOTS_ENV:'["inherited"]'}):
            worker = self.pool._new_worker("orig", self.entry("orig"))
        self.assertEqual(json.loads(worker.env[REFERENCE_ROOTS_ENV]), [str(self.refs.resolve())])
        self.assertEqual(json.loads(worker.env[CONVERSION_SOURCE_ROOTS_ENV]), [str(self.takes.resolve())])
        self.assertEqual(worker.env["HF_HUB_OFFLINE"], "1")

    def test_real_worker_guards_accept_isolated_references_in_their_own_python_environments(self):
        # The actual launch environment and adapter validators run in each pinned runtime;
        # no model or GPU is loaded by this containment regression.
        guards = {
            "breeze-tts-2": "from breeze_worker import BreezeWorker; guard=BreezeWorker._reference_path",
            "qwen3-tts-1.7b-base": "from qwen_worker import QwenWorker; guard=QwenWorker._reference_path",
            "qwen3-tts-0.6b-base": "from qwen_worker import QwenWorker; guard=QwenWorker._reference_path",
            "voxcpm2": "from voxcpm_worker import VoxCPMWorker; guard=VoxCPMWorker._reference",
            "orig": "from reference_paths import resolve_reference_path; guard=lambda p:resolve_reference_path('orig',p)",
            "turbo": "from reference_paths import resolve_reference_path; guard=lambda p:resolve_reference_path('turbo',p)",
        }
        for engine, imports in guards.items():
            with self.subTest(engine=engine):
                self.assertEqual(self.pool._resolve_reference_path(engine, self.reference), self.reference)
                # Own synthetic reference replaces the af_heart fixture, which a fresh checkout does not have.
                for denied in (self.source, self.outside, self.unrelated):
                    with self.assertRaises(ValueError):
                        self.pool._resolve_reference_path(engine, denied)
                entry = dict(self.pool.entries[engine])
                if not (ROOT / entry['runtime']['python']).is_file():
                    # On clean source builds, validate the same adapter guard in the core runtime.
                    entry['runtime'] = {**entry['runtime'], 'python': sys.executable}
                worker = self.pool._new_worker(engine, entry)
                bootstrap = ""
                if engine == "voxcpm2":
                    entrypoint = ast.parse((ROOT / 'engine_worker.py').read_text(encoding='utf-8'))
                    main = next(node for node in entrypoint.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
                    setup = next(node for node in main.body if isinstance(node, ast.If)
                                 and ast.unparse(node.test) == "args.engine == 'voxcpm2'")
                    bootstrap = ("import importlib.util, os, sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
                                 "root=Path.cwd(); args=SimpleNamespace(engine='voxcpm2')\n" + ast.unparse(setup) + "\n")
                script = bootstrap + imports + "\nimport sys\nfor value in sys.argv[1:3]: assert str(guard(value))==value\n" \
                    "for value in sys.argv[3:]:\n try: guard(value)\n except ValueError: pass\n else: raise AssertionError('accepted unrelated audio: '+value)\n"
                result = subprocess.run([worker.command[0], "-c", script, str(self.reference),
                    str(self.reference), str(self.outside),
                    str(self.refs / ".." / ".." / ".." / ".." / "outside.wav"), str(self.source), str(self.unrelated)],
                    cwd=ROOT, env=worker.env, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_bad_configuration_remains_rejected(self):
        for value in ('invalid-json', '{}', '["relative/path"]', '[1]'):
            with patch.dict(os.environ, {REFERENCE_ROOTS_ENV:value}):
                with self.assertRaises(ValueError):
                    configured_worker_roots()

    def test_link_escape_remains_rejected(self):
        try:
            escaped = self.refs / "escaped.wav"
            escaped.symlink_to(self.outside)
        except OSError as exc:
            if os.name != 'nt':
                self.skipTest(f'Symbolic-link creation unavailable: {exc}')
            # Directory junctions exercise Windows resolved containment without
            # requiring the optional symbolic-link privilege.
            outside_dir = self.root / 'outside-directory'
            outside_dir.mkdir()
            (outside_dir / 'audio.wav').write_bytes(self.outside.read_bytes())
            junction = self.refs / 'escaped-directory'
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                'New-Item -ItemType Junction -Path $env:VOICE_LAB_TEST_LINK -Target $env:VOICE_LAB_TEST_TARGET -ErrorAction Stop | Out-Null'],
                env={**os.environ, 'VOICE_LAB_TEST_LINK':str(junction), 'VOICE_LAB_TEST_TARGET':str(outside_dir)},
                capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest(f'Junction creation unavailable: {result.stderr}')
            escaped = junction / 'audio.wav'
        with self.assertRaises(ValueError):
            resolve_reference_path("orig", escaped, extra_roots=(self.refs,))

    def test_isolated_conversion_assets_and_source_only_roots(self):
        cast = CastStore(self.root)
        for target, audio in ((True, self.reference), (False, self.reference), (False, self.source)):
            path, _ = serve._conversion_audio_path(str(audio), "Test", target=target, cast=cast)
            self.assertEqual(path, audio)
        with self.assertRaises(ValueError):
            serve._conversion_audio_path(str(self.source), "Target", target=True, cast=cast)
        with self.assertRaises(ValueError):
            serve._conversion_audio_path(str(self.outside), "Source", cast=cast)
        self.assertIn(str(self.reference), {row['path'] for row in serve._conversion_asset_rows(cast, target=True)})
        self.assertIn(str(self.source), {row['path'] for row in serve._conversion_asset_rows(cast)})
        worker = self.pool._new_worker("chatterbox-vc", self.entry("chatterbox-vc"))
        script = """from chatterbox_vc_worker import _resolve_audio
import sys
assert str(_resolve_audio(sys.argv[1], 'Target voice reference')) == sys.argv[1]
assert str(_resolve_audio(sys.argv[2], 'Source performance')) == sys.argv[2]
for value, role in ((sys.argv[2], 'Target voice reference'), (sys.argv[3], 'Source performance')):
    try: _resolve_audio(value, role)
    except ValueError: pass
    else: raise AssertionError('accepted invalid conversion input')
"""
        result = subprocess.run([worker.command[0], '-c', script, str(self.reference), str(self.source), str(self.outside)],
                                cwd=ROOT, env=worker.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
