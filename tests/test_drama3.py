"""Drama 3 contract, reference persistence, native PCM and Windows ownership checks."""
import base64
import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from drama3_runtime import controls, SpeechRuntime
from worker_pool import EnginePool, read_catalog, process_is_running
from cast_store import CastStore
from scene_store import SceneStore


class Drama3Tests(unittest.TestCase):
    def test_clone_switches_off_gpu_and_design_restores_selected_device(self):
        runtime=SpeechRuntime.__new__(SpeechRuntime)
        runtime.device=runtime.design_device='Vulkan1'
        with patch.object(runtime,'close') as close:
            runtime._select_device(cloning=True)
            self.assertEqual(runtime.device,'cpu')
            close.assert_called_once()
            runtime._select_device(cloning=True)
            self.assertEqual(close.call_count,1)
            runtime._select_device(cloning=False)
            self.assertEqual(runtime.device,'Vulkan1')
            self.assertEqual(close.call_count,2)

    def test_effective_controls_and_invalid_values(self):
        self.assertEqual(controls({'seed':0,'top_k':1})['seed'],0)
        for changes in ({'cfg':1},{'top_k':1.5},{'seed':True},{'temperature':float('nan')},
                        {'max_seconds':100},{'top_p':0},{'seed':-1}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):controls(changes)

    def test_broker_rejects_invalid_clone_before_starting_any_model(self):
        def forbidden(_):raise AssertionError('A model should not have started')
        pool=EnginePool('cpu',0,worker_factory=forbidden)
        try:
            for body in ({'cfg':1},{'mode':'continuation'},{'mode':'clone','reference_path':'missing.wav'},
                         {'temperature':True}):
                with self.subTest(body=body),self.assertRaises(ValueError):
                    pool.speak({'engine':'drama-3','text':'Keep the gate closed.',**body})
        finally:pool.close()

    def test_native_pcm_sequence_count_and_truncation(self):
        runtime=SpeechRuntime.__new__(SpeechRuntime)
        runtime.proc=types.SimpleNamespace(stdin=io.StringIO())
        runtime.events=queue.Queue()
        pcm=b'\x01\x00\x02\x00'
        runtime.events.put({'type':'chunk','id':'fixture','seq':0,'pcm':base64.b64encode(pcm).decode()})
        runtime.events.put({'type':'end','id':'fixture','samples':2,'stop':'complete'})
        with patch('drama3_runtime.uuid.uuid4',return_value=types.SimpleNamespace(hex='fixture')):
            result, terminal=runtime._request({'type':'synthesize','text':'A line.'})
        self.assertEqual(result,pcm)
        runtime.events.put({'type':'end','id':'fixture','samples':0,'stop':'max_seconds'})
        with patch('drama3_runtime.uuid.uuid4',return_value=types.SimpleNamespace(hex='fixture')):
            with self.assertRaisesRegex(RuntimeError,'duration limit'):
                runtime._request({'type':'synthesize','text':'A line.'})

    def test_cast_reference_and_scene_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            cast=CastStore(temp); character=cast.create_character('Drama test')
            identity=cast.create_identity(character['id'],'drama-3','Q4 clone')
            raw=io.BytesIO();sf.write(raw,.1*np.sin(np.arange(24000)*.05),24000,format='WAV')
            with self.assertRaisesRegex(ValueError,'transcript'):
                cast.add_reference(identity['id'],'ref.wav',raw.getvalue(),origin='synthetic',allowed_use='test')
            reference=cast.add_reference(identity['id'],'ref.wav',raw.getvalue(),transcript='Keep the gate closed.',
                                         origin='synthetic',allowed_use='test')
            cast.update_identity(identity['id'],{'status':'approved','reference_version_id':reference['id']})
            entries={entry['id']:entry for entry in read_catalog()['engines']}
            store=SceneStore(cast,entries,root=temp)
            scene=store.create_scene({'name':'Drama','lines':[{'line_id':'first','identity_id':identity['id'],
                'text':'Keep the gate closed.','direction':'quietly','settings':{'seed':0,'max_seconds':12}}]})
            self.assertEqual(scene['lines'][0]['engine_id'],'drama-3')
            self.assertEqual(scene['lines'][0]['settings']['seed'],0)
            self.assertEqual(store.get_scene(scene['scene_id'])['lines'][0]['text'],'Keep the gate closed.')

    @unittest.skipUnless(os.name=='nt','Windows child-process ownership')
    def test_native_child_dies_when_python_worker_is_terminated(self):
        python=getattr(sys,'_base_executable',sys.executable)
        script="""import subprocess,sys,time
sys.path.insert(0,sys.argv[1])
from drama3_runtime import ChildJob
job=ChildJob()
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])
job.add(child)
print(child.pid,flush=True)
time.sleep(120)
"""
        parent=subprocess.Popen([python,'-u','-c',script,str(ROOT)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                text=True,creationflags=subprocess.CREATE_NO_WINDOW)
        child=None
        try:
            line=parent.stdout.readline().strip()
            self.assertTrue(line,parent.stderr.read() if parent.poll() is not None else 'Missing child PID')
            child=int(line);self.assertTrue(process_is_running(child))
            parent.terminate();parent.wait(5)
            deadline=time.monotonic()+5
            while process_is_running(child) and time.monotonic()<deadline:time.sleep(.05)
            self.assertFalse(process_is_running(child),'Native child survived termination of its owning worker')
        finally:
            if parent.poll() is None:parent.kill();parent.wait()
            parent.stdout.close();parent.stderr.close()
            if child and process_is_running(child):
                subprocess.run(['taskkill','/PID',str(child),'/F'],capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)


if __name__=='__main__':unittest.main()
