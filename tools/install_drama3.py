"""Install a pinned Drama 3 GGUF and portable Windows Vulkan runtime without pip changes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / 'out/drama3'
REPO = 'sakasegawa/drama-3-GGUF'
REVISION = '9a01354d04ab6eb521bc9cdfc56ab6824c474af2'
MODEL = 'Drama-35B-A3B-v3-Q4_K.gguf'
MODEL_SHA256 = '2a57b14c9b208a2ff833ab7305b920ca8f154ad807004a52d96a86a90a55523e'
RUNTIME_VERSION = '0.8.10'
RUNTIME_URL = 'https://github.com/nyosegawa/speech.cpp/releases/download/v0.8.10/speech-0.8.10-windows-x64-vulkan.zip'
RUNTIME_SHA256 = '2904dd54de7f18cf438338c28216ebffc205d29c6a2de3e645af40dd136b92f2'


def download_model(models):
    """Bounded HTTP ranges avoid Xet stalls and retain resumable completed ranges."""
    target = models / MODEL
    if target.is_file() and digest(target) == MODEL_SHA256:
        return
    partial, state = models / (MODEL + '.partial'), models / (MODEL + '.ranges.json')
    total, block = 20787264352, 32 * 1024 * 1024
    completed = json.loads(state.read_text()) if state.exists() else []
    if not partial.exists():
        completed = []
        with partial.open('wb') as stream: stream.truncate(total)
    base = f'https://huggingface.co/{REPO}/resolve/{REVISION}/{MODEL}?download=true'
    lock = threading.Lock()
    def fetch(index):
        start, end = index * block, min(total,(index+1)*block)-1
        for attempt in range(5):
            try:
                request = urllib.request.Request(base + f'&range_start={start}', headers={'Range':f'bytes={start}-{end}'})
                with urllib.request.urlopen(request,timeout=120) as source:
                    if source.status != 206 or source.headers.get('Content-Range') != f'bytes {start}-{end}/{total}':
                        raise RuntimeError('Model host did not honor the requested byte range.')
                    data = source.read(end-start+2)
                if len(data) != end-start+1: raise RuntimeError('Incomplete model byte range.')
                with lock:
                    with partial.open('r+b') as stream:
                        stream.seek(start); stream.write(data); stream.flush(); os.fsync(stream.fileno())
                    completed.append(index)
                    temp = state.with_suffix('.tmp')
                    temp.write_text(json.dumps(completed)); temp.replace(state)
                    if len(completed)%16==0:
                        print(f'Model download: {min(total,len(completed)*block)/1e9:.2f} / {total/1e9:.2f} GB',flush=True)
                return
            except Exception:
                if attempt == 4: raise
                time.sleep(attempt+1)
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(fetch,[i for i in range((total+block-1)//block) if i not in completed]))
    if digest(partial) != MODEL_SHA256:
        raise RuntimeError('Model range download checksum mismatch; partial and receipt retained.')
    partial.replace(target)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(8 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-only', action='store_true')
    args = parser.parse_args()
    INSTALL.mkdir(parents=True, exist_ok=True)
    archive = INSTALL / 'speech-0.8.10-windows-x64-vulkan.zip'
    if not archive.exists():
        print('Downloading portable Vulkan runtime', flush=True)
        with urllib.request.urlopen(RUNTIME_URL, timeout=120) as source, archive.open('wb') as target:
            while data := source.read(1024 * 1024):
                target.write(data)
    if digest(archive) != RUNTIME_SHA256:
        raise RuntimeError('Portable runtime checksum mismatch; retained archive for inspection.')
    runtime = INSTALL / 'runtime'
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if runtime.resolve() not in (runtime / member.filename).resolve().parents:
                raise RuntimeError('Archive member escaped installation directory.')
        for member in bundle.infolist():
            destination=runtime/member.filename
            if member.is_dir():
                destination.mkdir(parents=True,exist_ok=True)
                continue
            with bundle.open(member) as stream:
                expected=hashlib.file_digest(stream,'sha256').hexdigest()
            if not destination.is_file() or digest(destination)!=expected:
                bundle.extract(member,runtime)
    executable = next(runtime.rglob('speech.exe'))
    print('Verified runtime: ' + str(executable), flush=True)
    if args.runtime_only:
        return
    models = INSTALL / 'models'
    models.mkdir(parents=True, exist_ok=True)
    for filename in ('LICENSE', 'NOTICE', 'README.md', MODEL + '.json'):
        print('Downloading pinned model asset: ' + filename, flush=True)
        temporary = models / (filename + '.download')
        url = f'https://huggingface.co/{REPO}/resolve/{REVISION}/{filename}'
        with urllib.request.urlopen(url, timeout=120) as source, temporary.open('wb') as target:
            while data := source.read(1024 * 1024):
                target.write(data)
        temporary.replace(models / filename)
    print('Downloading pinned model with resumable HTTP ranges',flush=True)
    download_model(models)
    model = models / MODEL
    print('Checking complete model SHA-256', flush=True)
    if digest(model) != MODEL_SHA256:
        raise RuntimeError('Model checksum mismatch; retained file for inspection.')
    receipt = {'schema':1, 'upstream_model':'fishaudio/drama-3',
        'upstream_revision':'e0a9218aa48888270f06ef1ed9ea67622c0e17d2',
        'repo':REPO, 'revision':REVISION, 'model_path':str(model.resolve()), 'model_sha256':MODEL_SHA256,
        'runtime':'speech.cpp', 'runtime_version':RUNTIME_VERSION, 'runtime_path':str(executable.resolve()),
        'runtime_sha256':digest(executable), 'runtime_archive_sha256':RUNTIME_SHA256,
        'backend':'Vulkan1', 'quantization':'Q4_K', 'attribution':'Built with Fish Audio',
        'license':'Fish Audio Community License 1.0', 'license_path':str((models / 'LICENSE').resolve()),
        'notice_path':str((models / 'NOTICE').resolve())}
    (INSTALL / 'installation.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print('Model and runtime verified; receipt: ' + str(INSTALL / 'installation.json'), flush=True)


if __name__ == '__main__':
    main()
