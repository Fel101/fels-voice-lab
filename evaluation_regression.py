"""Local regression check: speak one evaluation suite through one engine and compare it with a saved baseline.

Run it before a dependency or checkpoint pin changes, save a baseline, make the change, and run the check again.
For each clip it records what can be measured without a listener: whether the engine spoke, the word error rate of
a recognizer reading the clip back (the language screen's recognizer), the speaker proxy against the reference voice
(clone engines only), and the clip's length. Engines run through the app's worker pool, so a broken install shows up
here. CI installs core requirements only and runs no real engine, so this is the check that exercises them.

    .venv\\Scripts\\python.exe evaluation_regression.py save-baseline --suite neutral-clone-three-line --engine kokoro
    .venv\\Scripts\\python.exe evaluation_regression.py check --suite neutral-clone-three-line --engine kokoro

Exit codes: 0 nothing beyond the margins below, 3 a regression, 2 no baseline or a usage problem.
The margins are starting points, set before any run; they have not been calibrated against repeated runs.
"""

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import engine_checks
import language_matrix as lm
import speaker_similarity
from evaluation_suites import get_suite

LAB = Path(__file__).resolve().parent
BASELINES = LAB / "out" / "regression" / "baselines"
RUNS = LAB / "out" / "regression" / "runs"
REFERENCE = LAB / "reference" / "af_heart.wav"
SCHEMA = 1
WER_MARGIN = 0.10         # the mean word error rate may rise by this much before it is a regression
SPEAKER_MARGIN = 0.05     # the mean speaker proxy may fall by this much before it is a regression
DURATION_TOLERANCE = 0.25  # a clip whose length moves by more than this fraction is a regression


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def baseline_path(engine, suite_id):
    return BASELINES / f"{engine}__{suite_id}.json"


def plan_suite(suite, engine, *, repeats=None, voices=()):
    """Every case of the suite, once per repeat, for one engine. A case in a language the engine does not claim is skipped."""
    claimed = lm.claimed_columns(lm.claims(engine))
    plan = []
    for case in suite["cases"]:
        code = lm.COLUMN_OF_LABEL[case["language"]]
        voice = lm.kokoro_voice(code, voices) if engine == "kokoro" else None
        if code not in claimed:
            skip = f"{engine} does not claim {case['language']}"
        elif engine == "kokoro" and voice is None:
            skip = "no Kokoro voice pack for this language in the installed snapshot"
        else:
            skip = None
        for repeat in range(1, (repeats or suite["repeat_count"]) + 1):
            # Several cases can share a language, so each clip's file is named by its case, not only its language.
            plan.append({"id": f"{engine}:{case['case_id']}:{repeat}", "engine": engine, "column": code,
                         "label": lm.engine_label(engine, code), "case_id": case["case_id"], "text": case["text"],
                         "repeat": repeat, "seed": lm.SEED_BASE + repeat, "voice": voice, "skip": skip,
                         "file": f"{case['case_id']}-{repeat}.wav"})
    return plan


def speak_suite(engine, suite, *, pool, out_dir, repeats=None, voices=(), installed=None):
    """Speak the suite's clips with the app's engine pool. Direction and cues are not sent: the check covers the words, the voice and the length."""
    plan = plan_suite(suite, engine, repeats=repeats, voices=voices)
    installed = installed or (lambda entry: engine_checks.installation(entry, LAB))
    return lm.generate(pool, plan, out_dir, installed=installed)


def measure(records, out_dir, *, engine, run_asr=None, similarity=None):
    """Attach what the recognizer heard, its word error rate, and (clone engines only) the speaker proxy."""
    lm.transcribe(records, out_dir, run_asr=run_asr or lm._run_asr)
    lm.score(records)
    for record in records:
        record["speaker"] = None
        if record["status"] == "ok" and engine in engine_checks.CLONE_ENGINES and similarity is not None:
            record["speaker"] = round(float(similarity.similarity(REFERENCE, out_dir / record["wav"])), 4)


def _mean(values, digits):
    return round(statistics.fmean(values), digits) if values else None


def summarize(records):
    spoken = [record for record in records if record["status"] == "ok"]
    rates = [record["error_rate"] for record in spoken if record.get("error_rate") is not None]
    speakers = [record["speaker"] for record in spoken if record.get("speaker") is not None]
    return {"clips": len(records), "ok": len(spoken),
            "failed": sum(record["status"] == "failed" for record in records),
            "unavailable": sum(record["status"] == "unavailable" for record in records),
            "mean_error_rate": _mean(rates, 4), "mean_speaker": _mean(speakers, 4),
            "mean_seconds": _mean([record["audio_seconds"] for record in spoken], 3)}


def clip_rows(records):
    return {record["id"]: {"status": record["status"], "reason": record.get("reason"), "heard": record.get("heard"),
                           "error_rate": record.get("error_rate"), "speaker": record.get("speaker"),
                           "audio_seconds": record.get("audio_seconds")} for record in records}


def _paired_means(before, after, ids, key):
    """Mean of ``key`` before and after, over the clips where both runs measured it, so the two means cover the same clips."""
    pairs = [(before[clip_id][key], after[clip_id][key]) for clip_id in ids
             if before[clip_id][key] is not None and after[clip_id][key] is not None]
    if not pairs:
        return None
    return statistics.fmean(old for old, _ in pairs), statistics.fmean(new for _, new in pairs)


def compare(current, baseline):
    """What got worse than the baseline, in words. An empty list means nothing did."""
    before, after = baseline["clips"], current["clips"]
    if set(before) != set(after):
        return ["the suite's clips differ from the baseline's; save a new baseline before comparing"]
    problems, shared = [], []
    for clip_id, old in before.items():
        new = after[clip_id]
        if old["status"] != "ok":
            continue  # not spoken in the baseline, so there is nothing to compare it with
        if new["status"] != "ok":
            problems.append(f"{clip_id}: spoke in the baseline, now {new['status']}: {new['reason'] or 'no reason recorded'}")
            continue
        shared.append(clip_id)
        if abs(new["audio_seconds"] - old["audio_seconds"]) > DURATION_TOLERANCE * old["audio_seconds"]:
            problems.append(f"{clip_id}: {old['audio_seconds']:.2f} s in the baseline, {new['audio_seconds']:.2f} s now")
    word = _paired_means(before, after, shared, "error_rate")
    if word and word[1] - word[0] > WER_MARGIN:
        problems.append(f"mean word error rate rose from {word[0]:.3f} to {word[1]:.3f} over the clips both runs spoke")
    speaker = _paired_means(before, after, shared, "speaker")
    if speaker and speaker[0] - speaker[1] > SPEAKER_MARGIN:
        problems.append(f"mean speaker proxy fell from {speaker[0]:.3f} to {speaker[1]:.3f} over the clips both runs spoke")
    return problems


def _describe(result):
    summary = result["summary"]
    speaker = "n/a" if summary["mean_speaker"] is None else f"{summary['mean_speaker']:.3f}"
    word = "n/a" if summary["mean_error_rate"] is None else f"{summary['mean_error_rate']:.3f}"
    return (f"{result['suite_id']} on {result['engine']} ({result['device']}): {summary['ok']} spoken, "
            f"{summary['failed']} failed, {summary['unavailable']} unavailable; mean word error {word}, "
            f"mean speaker proxy {speaker}, mean length {summary['mean_seconds']} s")


def _execute(args, pool_factory=None):
    """Speak, measure and summarize one run. ``pool_factory`` replaces the app's worker pool in tests."""
    from common import pick_device  # imported first: it points the model cache at this lab and selects the device
    from kokoro_support import discover_kokoro_voices
    from worker_pool import EnginePool

    suite = get_suite(args.suite)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = args.out_dir or RUNS / f"{args.engine}-{args.suite}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    make_pool = pool_factory or (lambda: EnginePool(pick_device("auto"), idle_minutes=5))
    pool = make_pool()
    try:
        records = speak_suite(args.engine, suite, pool=pool, out_dir=out_dir, repeats=args.repeats,
                              voices=discover_kokoro_voices())
        entry = pool.entries.get(args.engine, {})
        device = pool.device
    finally:
        pool.close()
    measure(records, out_dir, engine=args.engine,
            similarity=speaker_similarity if speaker_similarity.encoder_available() else None)
    result = {"schema": SCHEMA, "created_at": utc_now(), "engine": args.engine, "suite_id": suite["suite_id"],
              "suite_version": suite["version"], "device": device, "checkpoint": entry.get("checkpoint"),
              "local_revision": entry.get("local_revision"), "out_dir": str(out_dir),
              "summary": summarize(records), "clips": clip_rows(records)}
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv=None, pool_factory=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("check", "speak the suite and compare it with the saved baseline"),
                            ("save-baseline", "speak the suite and save the run as the baseline")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--suite", required=True, help="evaluation suite id, for example neutral-clone-three-line")
        command.add_argument("--engine", required=True, choices=lm.ENGINE_ORDER)
        command.add_argument("--repeats", type=int, help="generations per case (defaults to the suite's repeat_count)")
        command.add_argument("--out-dir", type=Path, help="where the clips, recognizer manifest and result.json go")
    args = parser.parse_args(argv)
    try:
        get_suite(args.suite)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 2
    baseline_file = baseline_path(args.engine, args.suite)
    if args.command == "check" and not baseline_file.is_file():
        print(f"No baseline for {args.engine} on {args.suite}. Run save-baseline before the change.", file=sys.stderr)
        return 2
    result = _execute(args, pool_factory=pool_factory)
    print(_describe(result))
    if args.command == "save-baseline":
        BASELINES.mkdir(parents=True, exist_ok=True)
        baseline_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        not_spoken = [clip_id for clip_id, clip in result["clips"].items() if clip["status"] != "ok"]
        print(f"Saved baseline: {baseline_file}")
        if not_spoken:
            print(f"Not spoken, so not compared later: {', '.join(not_spoken)}")
        return 0
    problems = compare(result, json.loads(baseline_file.read_text(encoding="utf-8")))
    for problem in problems:
        print(f"REGRESSION: {problem}")
    if not problems:
        print("No regression against the baseline.")
    return 3 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
