"""List locally cached Kokoro voices, or explicitly cache selected voice packs."""

import argparse
import re
import sys
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))
import common  # noqa: F401  (sets the model cache location before anything imports Hugging Face)
from kokoro_support import KOKORO_REPO, KOKORO_REPO_CACHE, discover_kokoro_voices, kokoro_snapshot_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="download only the explicitly named voice packs")
    parser.add_argument("voices", nargs="*", help="voice IDs such as ef_dora or ff_siwis")
    args = parser.parse_args()
    if not args.download:
        if args.voices:
            parser.error("voice IDs are accepted only with --download")
        print("Cached voices:", ", ".join(discover_kokoro_voices()) or "(none)")
        print("Snapshot:", kokoro_snapshot_dir())
        return
    if not args.voices or len(args.voices) > 8 or any(not re.fullmatch(r"[a-z][fm]_[a-z][a-z0-9_]*", v) for v in args.voices):
        parser.error("--download requires 1–8 valid voice IDs")
    if len(set(args.voices)) != len(args.voices):
        parser.error("duplicate voice IDs are not allowed")
    ref = KOKORO_REPO_CACHE / "refs" / "main"
    if not ref.is_file():
        raise SystemExit("No local Kokoro revision is recorded; restore the existing checkpoint before adding voices.")
    revision = ref.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise SystemExit(f"Invalid cached Kokoro revision: {revision!r}")
    from huggingface_hub import hf_hub_download

    cached = set(discover_kokoro_voices())
    for voice in args.voices:
        if voice in cached:
            print(f"Already cached: {voice}")
            continue
        path = hf_hub_download(repo_id=KOKORO_REPO, filename=f"voices/{voice}.pt",
                               revision=revision, cache_dir=str(KOKORO_REPO_CACHE.parent))
        print(f"Cached {voice}: {path}")
    print("Cached voices:", ", ".join(discover_kokoro_voices()))


if __name__ == "__main__":
    main()
