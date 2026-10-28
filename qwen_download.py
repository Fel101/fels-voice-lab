"""Download the Qwen3-TTS checkpoints into voice-lab/hf-cache. Run from .venv-qwen."""

from pathlib import Path

import common  # noqa: F401  (points HF_HOME at voice-lab/hf-cache)

from huggingface_hub import constants, snapshot_download
from huggingface_hub.file_download import are_symlinks_supported

REPOS = [
    "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",  # new voice from a text description
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base",  # voice cloning from a short clip
    "Qwen/Qwen3-TTS-12Hz-0.6B-Base",  # smaller, faster cloning
]

for repo in REPOS:
    # huggingface_hub 0.36 marks a folder "symlinks OK" before testing it, so its parallel download threads race
    # ahead and crash on Windows without Developer Mode (WinError 1314). The result is cached per folder, and the
    # folder asked about is the repo's own cache folder, so test that one up front; downloads then copy instead.
    repo_dir = Path(constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))
    are_symlinks_supported(repo_dir)
    path = snapshot_download(repo)
    print(f"{repo}: {path}", flush=True)
print("OK", flush=True)
