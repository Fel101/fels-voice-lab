"""Align Cast takes that have no word-time record yet, and store the records beside the takes.

    .venv\\Scripts\\python.exe align_takes.py --all
    .venv\\Scripts\\python.exe align_takes.py --take-id <id> [--take-id <id> ...]

Only derived word-time records are written. Take audio, Cast provenance and scenes are not changed. The aligner
runs once for the whole batch, in the isolated .venv-asr environment, and nothing is downloaded.
"""

import argparse
from collections import Counter

import word_timing
from cast_store import CastStore


def take_for_alignment(store, take_id):
    take = store.get_take(take_id)
    return {"id": take["id"], "engine_id": take["engine_id"], "language": take["language"],
            "spoken_text": take["spoken_text"], "audio_sha256": take["audio_sha256"],
            "media_path": store.get_media_path("take", take_id)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="align every take that has no record")
    group.add_argument("--take-id", action="append", help="align one take (repeatable)")
    parser.add_argument("--redo", action="store_true",
                        help="also re-align takes that already have a record; use after an aligner or check change")
    args = parser.parse_args(argv)

    store = CastStore()
    if args.take_id:
        take_ids = args.take_id
    elif args.redo:
        take_ids = store.all_take_ids()
    else:
        take_ids = store.take_ids_without_word_timings()
    if not take_ids:
        print("Nothing to align: every take already has a word-time record.")
        return 0
    takes = [take_for_alignment(store, take_id) for take_id in take_ids]
    records = word_timing.align_takes(takes)
    for take_id, record in records.items():
        store.set_word_timings(take_id, record)
    outcome = Counter("error" if "error" in record else (record["checks"]["status"]) for record in records.values())
    print(f"Aligned {len(records)} take(s): {outcome.get('ok', 0)} ok, {outcome.get('review', 0)} to review, "
          f"{outcome.get('error', 0)} error(s).")
    for take_id, record in records.items():
        if "error" in record:
            print(f"  {take_id}: {record['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
