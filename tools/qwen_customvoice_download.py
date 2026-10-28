"""Explicit, revision-pinned installer for the optional Qwen CustomVoice checkpoint.

Ordinary worker load and generation never call this module. The default command is a
local-only cache check; adding ``--download`` is the only path that contacts the Hub.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from common import LAB

REPO_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
REVISION = "0c0e3051f131929182e2c023b9537f8b1c68adfe"
MIN_FREE_BYTES = 6 * 1024**3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="explicitly download the pinned checkpoint")
    args = parser.parse_args()

    from huggingface_hub import snapshot_download

    if args.download:
        free_bytes = shutil.disk_usage(LAB).free
        if free_bytes < MIN_FREE_BYTES:
            parser.error(f"Need at least {MIN_FREE_BYTES / 1024**3:.1f} GiB free under {LAB}; found {free_bytes / 1024**3:.1f} GiB.")
        # The caller explicitly selected --download. Override offline flags that may
        # have been inherited by this one installer process only.
        os.environ["HF_HUB_OFFLINE"] = "0"
        local_files_only = False
    else:
        local_files_only = True

    path = Path(snapshot_download(
        repo_id=REPO_ID,
        revision=REVISION,
        local_files_only=local_files_only,
    )).resolve()
    files = [item for item in path.rglob("*") if item.is_file()]
    report = {
        "repo_id": REPO_ID,
        "revision": REVISION,
        "local_files_only": local_files_only,
        "snapshot_path": str(path),
        "file_count": len(files),
        "bytes": sum(item.stat().st_size for item in files),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
