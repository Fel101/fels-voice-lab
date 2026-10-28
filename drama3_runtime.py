"""Offline speech.cpp Drama 3 runtime for Fel's Voice Lab."""
import base64
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
REPO = 'sakasegawa/drama-3-GGUF'
REVISION = '9a01354d04ab6eb521bc9cdfc56ab6824c474af2'
MODEL_NAME = 'Drama-35B-A3B-v3-Q4_K.gguf'
MODEL_SHA256 = '2a57b14c9b208a2ff833ab7305b920ca8f154ad807004a52d96a86a90a55523e'
DEFAULTS = {'seed':42, 'temperature':1., 'top_k':30, 'top_p':.9, 'max_seconds':47.6}
LIMITS = {'seed':(0, 2147483647), 'temperature':(.05, 2.), 'top_k':(1, 4097),
          'top_p':(.05, 1.), 'max_seconds':(.5, 95.1)}
LICENCE_CONDITIONS = {
    'attribution':'Built with Fish Audio', 'commercial_revenue_limit_usd':500000,
    'hosted_speech_service_requires_separate_license':True,
    'license_url':'https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE',
    'note':'Fish Audio Community License 1.0 applies to the model and derivative. Review its full terms before commercial use.'}


def controls(values=None):
    if values is None:values = {}
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError('Drama 3 supports only seed, temperature, top_k, top_p and max_seconds.')
    result = {**DEFAULTS, **values}
    for key, value in result.items():
        low, high = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'Drama 3 {key} must be a finite number from {low} to {high}.')
        if key in ('seed','top_k') and not isinstance(value, int):
            raise ValueError(f'Drama 3 {key} must be an integer.')
    return result


def assets(root=ROOT, receipt_path=None):
    receipt_path = Path(receipt_path or Path(root) / 'out/drama3/installation.json')
    try:
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        model, executable = Path(receipt['model_path']), Path(receipt['runtime_path'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('Install Drama 3 with Fel\'s Voice Lab tools/install_drama3.py first.') from exc
    if (receipt.get('repo') != REPO or receipt.get('revision') != REVISION or
            receipt.get('model_sha256') != MODEL_SHA256 or not model.is_file() or
            model.stat().st_size != 20787264352 or not executable.is_file()):
        raise ValueError('Drama 3 installation is incomplete or differs from the pinned Q4 model.')
    if hashlib.sha256(executable.read_bytes()).hexdigest() != receipt.get('runtime_sha256'):
        raise ValueError('Drama 3 native runtime checksum changed; reinstall the pinned runtime.')
    return receipt


class ChildJob:
    """Windows kills owned native children if their Python worker exits or is cancelled."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',w.DWORD),
                ('min_working',ctypes.c_size_t),('max_working',ctypes.c_size_t),('active',w.DWORD),
                ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name,ctypes.c_uint64) for name in ('reads','writes','other','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                        ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE,w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None,None)
        limits = Extended(); limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def add(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle,int(process._handle)):
            process.kill(); process.wait()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class SpeechRuntime:
    def __init__(self, *, root=ROOT, receipt_path=None, device='Vulkan1'):
        self.receipt = assets(root, receipt_path)
        self.model = Path(self.receipt['model_path'])
        self.executable = Path(self.receipt['runtime_path'])
        self.device = device
        self.design_device = device
        self.work = Path(root) / 'out/drama3'
        self.work.mkdir(parents=True, exist_ok=True)
        self.proc = None
        self.job = None
        self.log = None
        self.info = None
        self.voices = set()

    def _spawn(self, args, **kwargs):
        process = subprocess.Popen([str(self.executable), *map(str,args)],
            env={**os.environ,'SPEECH_NO_UPDATE_CHECK':'1'},
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0), **kwargs)
        self.job.add(process)
        return process

    def _read(self, process, events):
        try:
            for raw in process.stdout:
                events.put(json.loads(raw))
        except Exception as exc:
            events.put(exc)
        finally:
            events.put(None)

    def _event(self, timeout):
        try:
            item = self.events.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise TimeoutError('Drama 3 native worker timed out; it has been unloaded.') from None
        if item is None or isinstance(item, Exception):
            self.close()
            raise RuntimeError(f'Drama 3 native worker stopped or violated its JSON protocol: {item}')
        if item.get('type') in ('fatal','error','cancelled'):
            raise RuntimeError(f'Drama 3: {item.get("error", item)}')
        return item

    def load(self):
        if self.proc and self.proc.poll() is None:
            return self.identity()
        with self.model.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest() != MODEL_SHA256:
                raise ValueError('Drama 3 model checksum changed; restore the pinned download before loading.')
        self.job = ChildJob()
        log_path = self.work / ('native-' + uuid.uuid4().hex + '.log')
        self.log = log_path.open('a', encoding='utf-8')
        self.events = queue.Queue()
        started = time.monotonic()
        try:
            self.proc = self._spawn(['worker',self.model,'--device',self.device,'--threads','8','--no-warmup'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',bufsize=1)
            threading.Thread(target=self._read,args=(self.proc,self.events),daemon=True).start()
            ready = self._event(600)
            if ready.get('type') != 'ready' or ready.get('protocol') != 3:
                raise RuntimeError('Unsupported speech.cpp worker protocol.')
            self.info = ready['model']
            self.load_seconds = round(time.monotonic() - started,3)
            return self.identity()
        except BaseException:
            self.close()
            raise

    def identity(self):
        return {'backend':'speech.cpp-cpu' if self.device == 'cpu' else 'speech.cpp-vulkan', 'backend_version':'0.8.10', 'checkpoint':'fishaudio/drama-3',
            'checkpoint_revision':self.receipt['upstream_revision'], 'quantized_checkpoint':REPO,
            'quantized_revision':REVISION, 'model_sha256':MODEL_SHA256, 'quantization':'Q4_K',
            'runtime_sha256':self.receipt['runtime_sha256'], 'device':self.device, 'sample_rate':44100,
            'native_pid':self.proc.pid if self.proc else None, 'load_seconds':getattr(self,'load_seconds',None),
            'license':'Fish Audio Community License 1.0', 'license_conditions':LICENCE_CONDITIONS,
            'attribution':'Built with Fish Audio', 'native_watermark':None}

    def _select_device(self, *, cloning):
        # R9700/Vulkan reference-conditioned synthesis lost the device and Windows
        # bugchecked (0x116) during acceptance on 2026-10-18. Keep that path off the
        # GPU until an upstream fix is verified; encoding alone on CPU was not enough.
        selected = 'cpu' if cloning else self.design_device
        if selected != self.device:
            self.close()
            self.device = selected

    def prepare_voice(self, references):
        """Compile and synthesize reference-conditioned voices on CPU on this host."""
        if not references or len(references) > 16:
            raise ValueError('Drama 3 needs between one and sixteen references.')
        parts = []
        for index, ref in enumerate(references):
            path, transcript = Path(ref['path']).resolve(), ref['text']
            if not path.is_file() or not isinstance(transcript,str) or not transcript.strip():
                raise ValueError('Drama 3 cloning requires audio and its exact transcript.')
            import soundfile as sf
            duration = sf.info(path).duration
            if not .5 <= duration <= 60:
                raise ValueError('Drama 3 references must last 0.5–60 seconds each.')
            speaker = ref.get('speaker',index)
            if isinstance(speaker,bool) or not isinstance(speaker,int) or not 0 <= speaker <= 15:
                raise ValueError('Drama 3 speaker index must be 0–15.')
            parts.append({'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'text':transcript,
                          'speaker':speaker,'path':str(path)})
        self._select_device(cloning=True)
        self.load()
        key = hashlib.sha256(json.dumps(parts,sort_keys=True).encode()).hexdigest()
        voice = self.work / 'voices' / (key + '.voice.gguf')
        voice.parent.mkdir(exist_ok=True)
        if not voice.is_file():
            temporary = voice.with_name(key + '-' + uuid.uuid4().hex + '.voice.gguf')
            args = ['voice',self.model,*[ref['path'] for ref in parts]]
            for ref in parts:
                args += ['--transcript',ref['text'],'--speaker',str(ref['speaker'])]
            args += ['--device','cpu','--threads','8',temporary]
            process = self._spawn(args,stdout=subprocess.DEVNULL,stderr=self.log)
            try:
                if process.wait(timeout=300):
                    raise RuntimeError('Drama 3 reference encoding failed; see the native runtime log.')
                temporary.replace(voice)
            except BaseException:
                process.kill(); process.wait()
                raise
        if key not in self.voices:
            self._request({'type':'add_voice','name':key,'path':str(voice)})
            self.voices.add(key)
        return key, parts

    def _request(self, values, on_audio=None):
        rid = uuid.uuid4().hex
        self.proc.stdin.write(json.dumps({'id':rid,**values},ensure_ascii=False) + '\n')
        self.proc.stdin.flush()
        chunks, sequence = [], 0
        deadline = time.monotonic() + 3600
        while True:
            message = self._event(max(.01,deadline-time.monotonic()))
            if message.get('id') != rid:
                raise RuntimeError('Drama 3 response request ID mismatch.')
            if message['type'] == 'chunk':
                if message.get('seq') != sequence:
                    raise RuntimeError('Drama 3 audio chunks arrived out of order.')
                sequence += 1
                pcm = base64.b64decode(message['pcm'],validate=True)
                if len(pcm)%2:
                    raise RuntimeError('Drama 3 audio chunk has an invalid sample size.')
                chunks.append(pcm)
                if on_audio:
                    on_audio(pcm,44100)
            elif message['type'] == 'end':
                if 'samples' in message and message['samples'] != sum(len(chunk)//2 for chunk in chunks):
                    raise RuntimeError('Drama 3 delivered sample count mismatch.')
                if message.get('stop','complete') != 'complete':
                    raise RuntimeError(f'Drama 3 stopped at {message["stop"]}; increase the duration limit or shorten the line.')
                return b''.join(chunks), message

    def generate(self, text, *, description='', direction='', references=None, settings=None, on_audio=None, cpu_only=False):
        settings = controls(settings)
        if not isinstance(text,str) or not text.strip():
            raise ValueError('Drama 3 needs dialogue text.')
        for cue in (description,direction):
            if not isinstance(cue,str) or len(cue)>1000 or any(c in cue for c in '[]<>\r\n'):
                raise ValueError('Drama 3 voice/delivery descriptions must be plain text, up to 1000 characters.')
        from drama3_production import script
        script(text)
        self._select_device(cloning=bool(references) or cpu_only)
        self.load()
        started = time.monotonic()
        voice, provenance = 'none', []
        if references:
            voice, provenance = self.prepare_voice(references)
        prompt = ''.join('[' + cue.strip() + '] ' for cue in (description,direction) if cue.strip()) + text.strip()
        pcm, terminal = self._request({'type':'synthesize','text':prompt,'voice':voice,**settings},on_audio)
        import numpy as np
        samples = np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768.
        if not samples.size or not np.isfinite(samples).all():
            raise RuntimeError('Drama 3 produced no valid audio.')
        return samples,44100,{**self.identity(),'total_s':round(time.monotonic()-started,3),
            'effective_settings':settings,'references':provenance,'prompt':prompt,'stop':terminal.get('stop'),
            'execution_policy':'CPU cloning after R9700 Vulkan device-loss/Windows 0x116; GPU design only',
            'mode':'clone' if references else 'design'}

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=5)
            self.proc = None
        if self.job:
            self.job.close();self.job = None
        if self.log:
            self.log.close();self.log = None
        self.voices.clear()
