import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
import zipfile

import numpy as np
import soundfile as sf
from drama3_production import Studio, script, protected_transform, LANGUAGES
from worker_pool import EnginePool
from worker_process import WorkerUnavailable


class ProductionTests(unittest.TestCase):
    def test_saved_cast_source_pins_reference_version_after_active_reference_changes(self):
        from cast_store import CastStore
        from drama3_lab_studio import create
        with tempfile.TemporaryDirectory() as temp:
            cast=CastStore(temp);character=cast.create_character('Keeper');identity=cast.create_identity(character['id'],'drama-3','Voice')
            def reference(text,value):
                raw=io.BytesIO();sf.write(raw,np.ones(44100)*value,44100,format='WAV')
                return cast.add_reference(identity['id'],'ref.wav',raw.getvalue(),transcript=text,origin='synthetic',allowed_use='QA')
            old=reference('Original words.',.1);new=reference('New reference words.',.2)
            cast.update_identity(identity['id'],{'status':'approved','reference_version_id':old['id']})
            seen=[]
            class Pool:
                def stream_drama3(self,body,refs,emit,cancel):seen.extend(refs);emit(b'\x01\x00'*441,44100);return {'device':'cpu'}
            studio=create(Pool(),cast);source=studio.sources()[0]['id']
            self.assertEqual(source,identity['id']+':'+old['id'])
            cast.update_identity(identity['id'],{'status':'approved','reference_version_id':new['id']})
            row=studio.start({'text':'Keep the gate closed.','cast':[{'speaker':0,'source_id':source}]})
            studio.thread.join(3)
            self.assertEqual(seen[0]['reference_id'],old['id'])
            self.assertEqual(studio.get(row['id'])['job']['request']['cast'][0]['source_id'],source)
            studio.close()

    def test_source_visibility_applies_to_history_previews_and_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            visible=[True]
            def authorize(row):
                if not visible[0]:raise PermissionError('Source hidden')
            def runner(body,refs,directory,emit,cancel):
                pcm=b'\x01\x00'*100;emit(pcm,44100)
                with wave.open(str(directory/'take.wav'),'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(44100);wav.writeframes(pcm)
                return {'device':'cpu'}
            studio=Studio(temp,lambda:[],lambda _:None,runner,authorize=authorize)
            row=studio.start({'text':'A line.'});studio.thread.join(3);self.assertEqual(len(studio.list()),1)
            visible[0]=False;self.assertEqual(studio.list(),[])
            with self.assertRaises(PermissionError):studio.get(row['id'])
            with self.assertRaises(PermissionError):studio.artifact(row['id'],'package')
            studio.close()

    def test_http_job_poll_and_exports_use_the_actual_handler(self):
        import urllib.request
        import serve
        from cast_store import CastStore
        from evaluation_store import EvaluationStore
        from worker_pool import read_catalog
        with tempfile.TemporaryDirectory() as temp:
            class Pool:
                entries={r['id']:r for r in read_catalog()['engines']}
                def stream_drama3(self,body,refs,emit,cancel):
                    emit(b'\x01\x00'*441,44100);return {'device':'cpu'}
            cast=CastStore(temp)
            http=serve.LocalThreadingHTTPServer(('127.0.0.1',0),serve.BaseHTTPRequestHandler)
            http.RequestHandlerClass=serve.make_handler(Pool(),http.server_port,cast,
                evaluation_store=EvaluationStore(Path(temp)/'eval.sqlite3',lab_root=temp))
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{http.server_port}/api/drama3'
            try:
                request=urllib.request.Request(base+'/jobs',data=json.dumps({'text':'A line.'}).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request) as response:job=json.load(response)['job']
                for _ in range(20):
                    with urllib.request.urlopen(base+'/jobs/'+job['id']+'?offset=0') as response:packet=json.load(response)
                    if packet['job']['status']=='complete':break
                    time.sleep(.01)
                self.assertEqual(packet['job']['status'],'complete',packet)
                self.assertEqual(packet['offset'],882)
                with urllib.request.urlopen(base+'/jobs/'+job['id']+'/package') as response:self.assertEqual(response.headers['Content-Type'],'application/zip')
            finally:http.RequestHandlerClass.close_drama();http.shutdown();http.server_close();thread.join(3)

    def test_markup_and_spoken_words_and_language_manifest(self):
        value='<|speaker:0|> [whispers] Stay close. <|speaker:1|> [normal voice] I will wait.'
        parsed=script(value)
        self.assertEqual(parsed['plain_text'],'Stay close. I will wait.')
        self.assertEqual(parsed['speakers'],[0,1])
        self.assertEqual(protected_transform(value,lambda s:s.replace('voice','lantern')),value)
        self.assertEqual(script('[laughs]')['speakers'],[0])
        for text in ['[oops','<|speaker:16|> Hello.','<|speaker:01|> Hello.','<|im_start|> Hello.','[[laughs]] Hi.','[whispers]']:
            with self.subTest(text=text),self.assertRaises(ValueError):script(text)
        self.assertEqual(len(LANGUAGES),83)
        path=Path(__file__).resolve().parents[1]/'out/drama3/models/Drama-35B-A3B-v3-Q4_K.gguf.json'
        if path.exists():self.assertEqual(set(LANGUAGES),set(json.loads(path.read_text())['languages']))

    def test_real_job_contract_preview_export_and_reference_immutability(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);ref=root/'ref.wav';sf.write(ref,np.zeros(44100),44100)
            entered=threading.Event();release=threading.Event();received=[]
            def runner(body,refs,directory,emit,cancel):
                received.append((body,refs))
                with wave.open(str(directory/'take.wav'),'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(44100)
                    for i in range(2):
                        pcm=b'\x01\x00'*4410;emit(pcm,44100);wav.writeframesraw(pcm)
                        if i==0:entered.set();release.wait(3)
                return {'device':'cpu','effective_settings':body['settings']}
            studio=Studio(root/'jobs',lambda:[],lambda key:{'path':str(ref),'text':'Reference words.','source_id':key},runner)
            try:
                text=('<|speaker:0|> Keep the gate closed. '*45)+'<|speaker:1|> We will wait here.'
                row=studio.start({'text':text,'cast':[{'speaker':0,'source_id':'a'},{'speaker':1,'source_id':'b'}],
                                  'settings':{'seed':0},'language':'ja'})
                self.assertTrue(entered.wait(2));preview=studio.get(row['id'])
                self.assertGreater(len(preview['pcm']),0)
                with self.assertRaises(ValueError):studio.artifact(row['id'],'audio')
                with self.assertRaises(RuntimeError):studio.start({'text':'Another take.'})
                release.set();studio.thread.join(3)
                final=studio.get(row['id'])['job'];self.assertEqual(final['status'],'complete',final)
                self.assertEqual(received[0][0]['text'],text)
                self.assertGreater(len(text),1200)
                self.assertEqual([r['speaker'] for r in received[0][1]],[0,1])
                package,_=studio.artifact(row['id'],'package')
                with zipfile.ZipFile(io.BytesIO(package)) as archive:
                    self.assertNotIn('[',archive.read('transcript.txt').decode())
                    saved=json.loads(archive.read('take.json'));self.assertEqual(saved['model']['effective_settings']['seed'],0)
                (root/'jobs'/row['id']/'take.wav').write_bytes(b'tampered')
                with self.assertRaisesRegex(ValueError,'checksum'):studio.artifact(row['id'],'package')
            finally:release.set();studio.close()

    def test_standby_instance_preserves_and_cancels_live_job(self):
        with tempfile.TemporaryDirectory() as temp:
            ready=threading.Event()
            def runner(body,refs,directory,emit,cancel):
                ready.set();cancel.wait(3);raise RuntimeError('stopped')
            owner=Studio(temp,lambda:[],lambda _:None,runner)
            row=owner.start({'text':'A retained line.'});self.assertTrue(ready.wait(2))
            standby=Studio(temp,lambda:[],lambda _:None,runner)
            self.assertEqual(standby.get(row['id'])['job']['status'],'running')
            with self.assertRaises(RuntimeError):standby.start({'text':'Duplicate.'})
            standby.cancel(row['id']);owner.thread.join(3)
            self.assertEqual(owner.get(row['id'])['job']['status'],'cancelled')
            with self.assertRaises(ValueError):owner.artifact(row['id'],'audio')
            owner.close();standby.close()

    def test_invalid_slots_and_reference_changes_never_become_accepted_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            ref=Path(temp)/'reference.wav';sf.write(ref,np.zeros(44100),44100)
            def runner(body,refs,directory,emit,cancel):ref.write_bytes(b'changed');return {}
            studio=Studio(Path(temp)/'jobs',lambda:[],lambda _:{'path':str(ref),'text':'Exact words.'},runner)
            with self.assertRaises(ValueError):studio.start({'text':'Hello.','settings':[]})
            with self.assertRaises(ValueError):studio.start({'text':'<|speaker:1|> Hello.','cast':[{'speaker':0,'source_id':'a'}]})
            with self.assertRaises(ValueError):studio.start({'text':'Hello.','cast':[{'speaker':True,'source_id':'a'}]})
            row=studio.start({'text':'Hello.','cast':[{'speaker':0,'source_id':'a'}]})
            studio.thread.join(3);self.assertEqual(studio.get(row['id'])['job']['status'],'failed');studio.close()

    def test_cancel_during_load_stops_only_the_owned_worker(self):
        entered=threading.Event();stopped=threading.Event();cancel=threading.Event()
        class Worker:
            pid=0;alive=False;log_path=Path('fixture.log')
            def start(self):self.alive=True
            def call(self,method,**kwargs):
                entered.set();stopped.wait(3);raise WorkerUnavailable('cancelled load')
            def close(self,force=False):self.alive=False;stopped.set()
        worker=Worker();pool=EnginePool('cpu',0,worker_factory=lambda _:worker)
        errors=[]
        def run():
            try:pool.stream_drama3({'text':'A line.','settings':{}},[],lambda *_:None,cancel)
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        self.assertTrue(entered.wait(2));cancel.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertFalse(worker.alive);self.assertTrue(errors);pool.close()


if __name__=='__main__':unittest.main()
