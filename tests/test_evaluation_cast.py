"""Cast-to-comparison integration: real stores, synthetic audio, no model inference."""

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from cast_store import CastStore
from evaluation_cast import import_cast_takes
from evaluation_store import EvaluationStore, RATING_FIELDS


class CastComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cast = CastStore(self.root)
        self.evaluations = EvaluationStore(lab_root=self.root)
        self.takes = [self.make_take('First', 180), self.make_take('Second', 320)]

    def tearDown(self):
        self.temp.cleanup()

    def make_take(self, label, frequency, *, text='The gate is open.', language='English', direction=''):
        char = self.cast.create_character(label)
        identity = self.cast.create_identity(char['id'], 'drama-3', label + ' voice')
        signal = .1 * np.sin(2 * np.pi * frequency * np.arange(24000) / 24000)
        source = io.BytesIO()
        sf.write(source, signal, 24000, format='WAV', subtype='PCM_16')
        ref = self.cast.add_reference(identity['id'], 'synthetic.wav', source.getvalue(),
            transcript='Synthetic test words.', origin='Synthetic test fixture, no human recording',
            allowed_use='Local tests only; no redistribution')
        identity = self.cast.get_generation_identity(identity['id'], ref['id'])
        return self.cast.create_take(identity, line_id='common-line', text=text, spoken_text=text,
            language=language, direction=direction, settings={'seed': 42},
            model={'checkpoint': 'synthetic test model', 'mode': 'clone'}, wav=signal,
            sample_rate=24000, generation_info={'total_s': 0.1})

    def import_takes(self, **kwargs):
        return import_cast_takes(self.cast, self.evaluations,
                                 [take['id'] for take in self.takes], **kwargs)

    def test_same_engine_voices_preserve_rights_source_and_support_real_blind_rating(self):
        originals = {take['id']: self.cast.get_media_path('take', take['id']).read_bytes()
                     for take in self.takes}
        run = self.import_takes(title='Two Fish candidates')
        self.assertEqual(len(run['samples']), 2)
        self.assertEqual(len({sample['engine_id'] for sample in run['samples']}), 2)
        for sample in run['samples']:
            self.assertEqual(sample['metadata']['engine_id'], 'drama-3')
            self.assertEqual(sample['metadata']['provenance']['allowed_use'],
                             'Local tests only; no redistribution')
            self.assertEqual((self.root / sample['raw_path']).read_bytes(),
                             originals[sample['metadata']['take_id']])
            self.assertEqual(self.cast.get_media_path('take', sample['metadata']['take_id']).read_bytes(),
                             originals[sample['metadata']['take_id']])
            self.assertTrue(sample['listening_path'].startswith('out/comparisons/cast-'))
        session = self.evaluations.create_blind_session(run['id'], 'Synthetic persistence fixture')
        self.assertEqual(len(session['trials']), 1)
        self.assertNotIn('candidate', json.dumps(session).replace(run['id'], ''))
        self.assertIsNone(session['trials'][0]['rating'])
        self.evaluations.save_rating(session['session_id'], session['trials'][0]['trial_id'],
                                    {'left': dict.fromkeys(RATING_FIELDS, 3),
                                     'right': dict.fromkeys(RATING_FIELDS, 4), 'preference': 'B'})
        revealed = self.evaluations.reveal_blind_session(session['session_id'])
        key = revealed['trials'][0]['revealed']['left_engine']
        self.assertIn(key, run['manifest']['candidate_labels'])
        reopened = EvaluationStore(lab_root=self.root)
        self.assertEqual(reopened.get_run(run['id'])['manifest_sha256'], run['manifest_sha256'])

    def test_different_text_language_or_direction_is_not_silently_paired(self):
        for field, value in [('text', 'Different words.'), ('language', 'British English'),
                             ('direction', 'Whisper softly.')]:
            other = self.make_take(field, 440, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'identical text'):
                import_cast_takes(self.cast, self.evaluations, [self.takes[0]['id'], other['id']])
        self.assertEqual(self.evaluations.list_runs(), [])
        self.assertFalse((self.root / 'out/comparisons').exists())

    def test_invalid_duplicate_and_unknown_ids_cannot_import_paths(self):
        for ids in [[], ['../private.wav', self.takes[0]['id']],
                    [self.takes[0]['id']] * 2, [self.takes[0]['id']] * 21]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                import_cast_takes(self.cast, self.evaluations, ids)
        with self.assertRaises(KeyError):
            import_cast_takes(self.cast, self.evaluations, ['0' * 32, '1' * 32])
        self.assertFalse((self.root / 'out/comparisons').exists())

    def test_changed_audio_is_rejected_before_creating_comparison(self):
        source = self.cast.get_media_path('take', self.takes[0]['id'])
        source.write_bytes(source.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.import_takes()
        self.assertFalse((self.root / 'out/comparisons').exists())

    def test_state_root_and_output_containment_are_enforced(self):
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaisesRegex(ValueError, 'same selected state root'):
            import_cast_takes(self.cast, EvaluationStore(lab_root=other), [t['id'] for t in self.takes])
        outside = EvaluationStore(lab_root=self.root, out_root=self.root.parent / 'not-this-lab')
        with self.assertRaisesRegex(ValueError, 'inside the selected state root'):
            import_cast_takes(self.cast, outside, [t['id'] for t in self.takes])
        self.assertFalse((self.root.parent / 'not-this-lab').exists())

    def test_failed_mastering_leaves_no_run_or_partial_owned_files(self):
        with patch('evaluation_cast._listening_copy', side_effect=ValueError('Unreadable audio')):
            with self.assertRaisesRegex(ValueError, 'Unreadable audio'):
                self.import_takes()
        self.assertEqual(self.evaluations.list_runs(), [])
        self.assertEqual(list((self.root / 'out/comparisons').iterdir()), [])
        self.assertTrue(all(self.cast.get_media_path('take', take['id']).is_file() for take in self.takes))

    def test_http_import_uses_selected_state_and_rejects_foreign_origin(self):
        import serve
        class Engines:
            entries = {}
        server = ThreadingHTTPServer(('127.0.0.1', 0), serve.make_handler(
            Engines(), 0, self.cast, evaluation_store=self.evaluations))
        port = server.server_address[1]
        server.RequestHandlerClass = serve.make_handler(Engines(), port, self.cast,
                                                        evaluation_store=self.evaluations)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f'http://127.0.0.1:{port}/api/evaluations/cast-import'
        payload = json.dumps({'title': 'HTTP fixture', 'take_ids': [take['id'] for take in self.takes]}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=payload,
                    headers={'Content-Type': 'application/json'}), timeout=10) as response:
                self.assertEqual(response.status, 201)
                run = json.loads(response.read())['run']
                self.assertEqual(run['title'], 'HTTP fixture')
            self.assertTrue((self.root / run['manifest_path']).is_file())
            def post_evaluation(route, body):
                request = urllib.request.Request(f'http://127.0.0.1:{port}' + route,
                    data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.loads(response.read())
            session = post_evaluation('/api/evaluations/blind-sessions', {'run_id': run['id']})['session']
            route = '/api/evaluations/blind-sessions/' + session['session_id']
            rated = post_evaluation(route + '/ratings', {'trial_id': session['trials'][0]['trial_id'],
                'ratings': {'left': dict.fromkeys(RATING_FIELDS, 2), 'right': dict.fromkeys(RATING_FIELDS, 5),
                            'preference': 'B'}, 'notes': 'Synthetic HTTP persistence fixture'})['session']
            self.assertTrue(rated['trials'][0]['rating_complete'])
            self.assertNotIn('revealed', rated['trials'][0])
            revealed = post_evaluation(route + '/reveal', {})['session']
            self.assertEqual(revealed['trials'][0]['rating']['left']['naturalness'], 2)
            self.assertEqual(revealed['trials'][0]['rating']['right']['naturalness'], 5)
            self.assertEqual(revealed['trials'][0]['rating']['preference'], 'B')
            self.assertIn('revealed', revealed['trials'][0])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(url, data=payload,
                    headers={'Content-Type': 'application/json', 'Origin': 'https://foreign.invalid'}), timeout=10)
            self.assertEqual(caught.exception.code, 403)
            self.assertEqual(len(self.evaluations.list_runs()), 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
