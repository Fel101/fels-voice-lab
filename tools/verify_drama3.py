"""Real Drama 3 design, speaker tags and isolated Cast HTTP acceptance; always unloads."""
import hashlib
import argparse
import io
import json
from pathlib import Path
import sys
import threading
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import soundfile as sf
import serve
from cast_store import CastStore
from evaluation_store import EvaluationStore
from lines import REFERENCE_TEXT
from worker_pool import EnginePool, process_is_running


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--clone-only',action='store_true',help='Test the CPU cloning path without repeating GPU design checks')
    args=parser.parse_args()
    run=ROOT/'out/drama3'/('acceptance-'+time.strftime('%Y%m%d-%H%M%S'))
    run.mkdir(parents=True,exist_ok=False)
    cast=CastStore(run)
    pool=EnginePool('cuda',0,reference_roots=(cast.data_root/'media/references',))
    server=serve.LocalThreadingHTTPServer(('127.0.0.1',0),serve.BaseHTTPRequestHandler)
    server.RequestHandlerClass=serve.make_handler(pool,server.server_address[1],cast,
        evaluation_store=EvaluationStore(run/'evaluations.sqlite3',lab_root=run))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_address[1]}'
    report={'run':str(run),'checks':[],'status':'running','human_listening':'not performed'}
    pids=[]
    def save(): (run/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    def sample(label, pcm, rate, info, elapsed):
        assert len(pcm) and np.isfinite(pcm).all() and np.max(np.abs(pcm))>1e-5
        path=run/(label+'.wav');sf.write(path,pcm,rate,subtype='PCM_16')
        row={'label':label,'path':str(path),'sample_rate':rate,'audio_seconds':len(pcm)/rate,
             'wall_seconds':round(elapsed,3),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'info':info}
        report['checks'].append(row);save()
        native=info.get('backend_result',info)
        if native.get('native_pid'):pids.append(native['native_pid'])
        print(f'PASS {label}: {len(pcm)/rate:.3f}s / {elapsed:.2f}s elapsed',flush=True)
    print('RUN '+str(run),flush=True)
    save()
    try:
        cases=[('neutral',{'text':'Keep the gate closed. I will return before sunset.'}),
            ('designed',{'text':'Welcome to the smithy. Keep your lantern close.',
                         'voice_description':'A warm, clear adult woman with a natural speaking voice'}),
            ('speakers',{'text':'<|speaker:0|> The gate is closed. <|speaker:1|> Then we wait for sunrise.'}),
            ('event',{'text':'[sighs] Fine. We will wait here.'})]
        for label,body in ([] if args.clone_only else cases):
            print('START '+label,flush=True);start=time.monotonic()
            pcm,rate,info=pool.speak({'engine':'drama-3','mode':'design','seed':42,**body})
            sample(label,pcm,rate,info,time.monotonic()-start)
        character=cast.create_character('Drama 3 isolated clone')
        identity=cast.create_identity(character['id'],'drama-3','Synthetic reference')
        source=ROOT/'reference/af_heart.wav'
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        reference=cast.add_reference(identity['id'],'af_heart.wav',source.read_bytes(),transcript=REFERENCE_TEXT,
            origin='Retained synthetic Kokoro reference',allowed_use='Local acceptance; checkpoint terms apply')
        cast.update_identity(identity['id'],{'status':'approved','reference_version_id':reference['id']})
        print('START isolated Cast HTTP clone',flush=True);start=time.monotonic()
        request=urllib.request.Request(base+'/api/performance',data=json.dumps({'identity_id':identity['id'],
            'line_id':'drama3-clone','text':'Keep the gate closed. We will return before sunset.',
            'settings':{'seed':0,'temperature':1.,'top_k':30,'top_p':.9}}).encode(),
            headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=700) as response:
            body=response.read();take=cast.get_take(response.headers['X-Take-Id'])
        assert cast.get_media_path('take',take['id']).read_bytes()==body
        assert take['reference_version_id']==reference['id']
        assert take['model']['effective_settings']['seed']==0
        pcm,rate=sf.read(io.BytesIO(body),dtype='float32')
        sample('cast-clone',pcm,rate,take['model'],time.monotonic()-start)
        assert take['model']['backend_result']['device']=='cpu', 'Reference-conditioned generation must avoid the crashing GPU path'
        assert take['model']['mode']=='clone'
        report['take_id']=take['id'];report['reference_path']=str(cast.get_media_path('reference',reference['id']))
        assert hashlib.sha256(source.read_bytes()).hexdigest()==before
        report['source_unchanged']=True;report['status']='passed'
    except BaseException as exc:
        report['status']='failed';report['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        pool.close();server.shutdown();server.server_close();thread.join(5)
        report['native_pids']=sorted(set(pids))
        # Windows job termination is asynchronous when the owning worker is
        # force-closed. Give its native children the same bounded wait as the
        # dedicated ownership regression before declaring a leak.
        deadline=time.monotonic()+5
        while any(process_is_running(pid) for pid in pids) and time.monotonic()<deadline:
            time.sleep(.05)
        report['native_workers_exited']=all(not process_is_running(pid) for pid in pids)
        if not report['native_workers_exited']:
            report['status']='failed';report['error']='Native workers did not exit within five seconds of shutdown'
        save()
    assert report['native_workers_exited']
    print('PASS all Fel\'s Voice Lab Drama 3 checks; model unloaded',flush=True)


if __name__=='__main__':main()
