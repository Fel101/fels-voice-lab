"""Screen each engine's claimed languages and record a word error rate for every cell.

The sentences come from the multilingual suite in evaluation_suites.py. Each engine speaks them on
the service device, and a small multilingual recognizer writes the words back with the language
forced. A cell is understood when its median error rate is at most UNDERSTOOD_MAX.

This is a screen, not a listening test. It shows whether the words survive a round trip through
speech and recognition. It does not judge accent, voice quality, likeness, or whether a fluent
speaker would accept the result.

    .venv\\Scripts\\python.exe language_matrix.py run
    .venv\\Scripts\\python.exe language_matrix.py publish <out-dir>\\evidence.json
    .venv\\Scripts\\python.exe language_matrix.py report
"""

import argparse
import json
import statistics
import subprocess
import threading
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import soundfile as sf

import engine_adapters
import engine_checks
from evaluation_suites import get_suite
from worker_pool import WorkerBusy, read_catalog

LAB = Path(__file__).resolve().parent
SUITE_ID = "multilingual-lines-common"
SCHEMA = 1
# Thresholds were fixed before the first recorded run; change them only with a new evidence file.
UNDERSTOOD_MAX = 0.20
PARTIAL_MAX = 0.40
SEED_BASE = 42
EVIDENCE = LAB / "docs" / "evidence" / "language-matrix.json"
PUBLIC_DOC = LAB / "docs" / "LANGUAGE_MATRIX.md"
ASR_PYTHON = LAB / ".venv-asr" / "Scripts" / "python.exe"
ASR_WORKER = LAB / "asr_worker.py"
ASR_MODEL_DIR = LAB / "hf-cache" / "faster-whisper-tiny-d90ca5fe"
ASR_NOTE = "faster-whisper tiny (multilingual, int8, CPU), the only Whisper model cached on this machine; language forced per clip"

# Matrix columns: code, the suite's label for the sentence, and the recognizer's language code.
COLUMNS = (
    ("en", "English", "en"),
    ("en-GB", "British English", "en"),
    ("es", "Spanish", "es"),
    ("fr", "French", "fr"),
    ("de", "German", "de"),
    ("it", "Italian", "it"),
    ("pt", "Portuguese", "pt"),
    ("ru", "Russian", "ru"),
    ("zh", "Chinese", "zh"),
    ("ja", "Japanese", "ja"),
    ("ko", "Korean", "ko"),
    ("hi", "Hindi", "hi"),
    ("ar", "Arabic", "ar"),
)
COLUMN_LABEL = {code: label for code, label, _ in COLUMNS}
ASR_CODE = {code: asr for code, _, asr in COLUMNS}
# Unspaced scripts have no word boundaries, so they are scored by character instead of by word.
CHARACTER_SCORED = {"zh", "ja"}
# Every language label an allowlist, document or upstream card uses, mapped to its column.
COLUMN_OF_LABEL = {
    "English": "en", "American English": "en", "British English": "en-GB", "Spanish": "es",
    "French": "fr", "German": "de", "Italian": "it", "Portuguese": "pt", "Brazilian Portuguese": "pt",
    "Russian": "ru", "Chinese": "zh", "Mandarin Chinese": "zh", "Japanese": "ja", "Korean": "ko",
    "Hindi": "hi", "Arabic": "ar",
}
# Kokoro's own language names, and the first letter of the voice packs that speak each one.
KOKORO_LABEL = {"en": "American English", "en-GB": "British English", "es": "Spanish", "fr": "French",
                "hi": "Hindi", "it": "Italian", "pt": "Brazilian Portuguese"}
KOKORO_LETTER = {"en": "a", "en-GB": "b", "es": "e", "fr": "f", "hi": "h", "it": "i", "pt": "p"}
# Claims beyond the scene picker's allowlist that an engine's own documentation makes (engine_adapters/).
DOCUMENTED_LANGUAGES = engine_adapters.table("documented_languages")
# The languages each engine's makers list, beyond those the Lab has checked (engine_adapters/).
UPSTREAM_LANGUAGES = engine_adapters.table("upstream_languages")
# The engines the matrix screens, in the catalog's order: every one that speaks text.
ENGINE_ORDER = tuple(entry["id"] for entry in read_catalog()["engines"]
                     if entry["id"] not in engine_adapters.engines_where("converts"))
CLIP_FIELDS = ("id", "engine", "column", "case_id", "repeat", "seed", "status", "reason", "device",
               "total_s", "audio_seconds", "load_seconds", "metric", "error_rate", "text", "heard")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(error_rate):
    if error_rate <= UNDERSTOOD_MAX:
        return "understood"
    if error_rate <= PARTIAL_MAX:
        return "partial"
    return "not_understood"


def metric_for(column):
    return "cer" if column in CHARACTER_SCORED else "wer"


def _clean(text):
    """Lowercase, and turn punctuation, symbols and separators into spaces. Letters and combining marks stay."""
    text = unicodedata.normalize("NFC", text).casefold()
    return "".join(" " if unicodedata.category(ch)[0] in "PSZC" else ch for ch in text)


def _units(text, metric):
    if metric == "cer":
        return list("".join(_clean(text).split()))
    return _clean(text).split()


def _edit_distance(expected, heard):
    previous = list(range(len(heard) + 1))
    for i, unit in enumerate(expected, 1):
        current = [i]
        for j, other in enumerate(heard, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (unit != other)))
        previous = current
    return previous[-1]


def error_rate(reference, heard, metric):
    """Edits needed to turn the heard text into the reference, divided by the reference's length."""
    expected = _units(reference, metric)
    if not expected:
        raise ValueError("The reference sentence has no scorable text.")
    return _edit_distance(expected, _units(heard, metric)) / len(expected)


# Writing systems the recognizer can write in. A transcript in another system is inconclusive, not a word error.
_FAMILIES = (("LATIN", "latin"), ("CYRILLIC", "cyrillic"), ("ARABIC", "arabic"),
             ("DEVANAGARI", "devanagari"), ("HANGUL", "hangul"), ("CJK", "cjk"),
             ("HIRAGANA", "cjk"), ("KATAKANA", "cjk"))


def _family(ch):
    name = unicodedata.name(ch, "")
    return next((family for prefix, family in _FAMILIES if name.startswith(prefix)), None)


def _families(text):
    return [family for family in (_family(ch) for ch in text if ch.isalpha()) if family]


def script_mismatch(reference, heard):
    """True when the recognizer wrote the speech in letters the sentence does not use, such as Latin for Devanagari."""
    expected = Counter(_families(reference)).most_common(1)
    written = set(_families(heard))
    return bool(expected) and bool(written) and expected[0][0] not in written


def claims(engine):
    """What an engine claims: the scene picker's languages, plus any documented or upstream claim."""
    from scene_store import SUPPORTED_LANGUAGES

    return {"scene_allowlist": sorted(label for label in SUPPORTED_LANGUAGES.get(engine, {"English"}) if label != "Auto"),
            "documented": list(DOCUMENTED_LANGUAGES.get(engine, ())),
            "upstream": list(UPSTREAM_LANGUAGES.get(engine, ()))}


def claimed_labels(claim):
    return sorted({*claim["scene_allowlist"], *claim["documented"], *claim["upstream"]})


def claimed_columns(claim):
    return {COLUMN_OF_LABEL[label] for label in claimed_labels(claim) if label in COLUMN_OF_LABEL}


def kokoro_voice(column, voices):
    """The first installed voice pack that starts with the language's letter; the app's default wins for English."""
    letter = KOKORO_LETTER.get(column)
    candidates = sorted(voice for voice in voices if letter and voice[:1] == letter)
    if not candidates:
        return None
    if "af_heart" in candidates:
        return "af_heart"
    return next((voice for voice in candidates if voice[1:2] == "f"), candidates[0])


def engine_label(engine, column):
    """The language name an engine's request takes. Engines that take none get the suite's name, which is unused."""
    if getattr(engine_adapters.get(engine), "voice_per_language", False):
        return KOKORO_LABEL.get(column)
    return COLUMN_LABEL[column]


def build_plan(*, suite, engines=None, columns=None, repeats=None, voices=()):
    """One clip per engine, column and repeat for every claimed cell. A cell that cannot run carries a skip reason."""
    repeats = repeats or suite["repeat_count"]
    sentences = {COLUMN_OF_LABEL[case["language"]]: case for case in suite["cases"]}
    plan = []
    for engine in ENGINE_ORDER:
        if engines and engine not in engines:
            continue
        claimed = claimed_columns(claims(engine))
        for code, _, _ in COLUMNS:
            if code not in claimed or (columns and code not in columns):
                continue
            case = sentences[code]
            per_language = getattr(engine_adapters.get(engine), "voice_per_language", False)
            voice = kokoro_voice(code, voices) if per_language else None
            skip = "no Kokoro voice pack for this language in the installed snapshot" if per_language and voice is None else None
            for repeat in range(1, repeats + 1):
                plan.append({"id": f"{engine}:{code}:{repeat}", "engine": engine, "column": code,
                             "label": engine_label(engine, code), "case_id": case["case_id"], "text": case["text"],
                             "repeat": repeat, "seed": SEED_BASE + repeat, "voice": voice, "skip": skip})
    return plan


def request_for(clip, loaded, *, root=LAB):
    """The request a Perform line would send for this clip, with its sentence and language swapped in."""
    engine = clip["engine"]
    request = {"engine": engine, "text": clip["text"]}
    adapter = engine_adapters.get(engine)
    if adapter is not None:
        return adapter.matrix_request(request, clip, loaded, root=root)
    return {**request, "mode": "design", "voice_description": engine_checks.CHECK_VOICE, "seed": clip["seed"]}


def _record(clip, status, reason=None, **measured):
    base = {key: value for key, value in clip.items() if key != "skip"}
    return {**base, "status": status, "reason": reason, "device": None, "total_s": None,
            "audio_seconds": None, "load_seconds": None, "wav": None, **measured}


def _reported(label, supported):
    return any(value.casefold() == label.casefold() for value in supported)


def _speak(pool, clip, loaded, out_dir):
    if clip["label"] and loaded.get("supported_languages") and not _reported(clip["label"], loaded["supported_languages"]):
        return _record(clip, "unavailable", f"the checkpoint does not report {clip['label']}")
    try:
        wav, sample_rate, info = pool.speak(request_for(clip, loaded))
    except WorkerBusy:
        raise
    except Exception as exc:
        return _record(clip, "failed", engine_checks.failure_reason(exc))
    relative = Path(clip["engine"]) / clip.get("file", f"{clip['column']}-{clip['repeat']}.wav")
    (out_dir / relative).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_dir / relative), wav, sample_rate)
    return _record(clip, "ok", device=engine_checks.clip_device(info, pool.device), total_s=info.get("total_s"),
                   audio_seconds=wav.size / sample_rate, load_seconds=loaded.get("load_seconds"),
                   wav=relative.as_posix())


def _speak_engine(pool, clips, out_dir):
    records = []
    try:
        loaded = pool.load(clips[0]["engine"])
    except WorkerBusy:
        raise
    except Exception as exc:
        records = [_record(clip, "failed", engine_checks.failure_reason(exc)) for clip in clips]
    else:
        records = [_speak(pool, clip, loaded, out_dir) for clip in clips]
    finally:
        pool.unload()
    return records


def generate(pool, plan, out_dir, *, installed):
    """Speak every planned clip, one engine at a time: load it, speak its clips, then unload it."""
    records = []
    for engine in dict.fromkeys(clip["engine"] for clip in plan):
        clips = [clip for clip in plan if clip["engine"] == engine]
        probe = installed(pool.entries[engine])
        if not probe["installed"]:
            reason = "not installed: " + ", ".join(probe["missing"])
            records.extend(_record(clip, "unavailable", reason) for clip in clips)
            continue
        records.extend(_record(clip, "unavailable", clip["skip"]) for clip in clips if clip["skip"])
        runnable = [clip for clip in clips if not clip["skip"]]
        if runnable:
            records.extend(_speak_engine(pool, runnable, out_dir))
    return records


def _run_asr(manifest):
    completed = subprocess.run(
        [str(ASR_PYTHON), "-I", str(ASR_WORKER), "--model-dir", str(ASR_MODEL_DIR), "--manifest", str(manifest)],
        cwd=str(LAB), capture_output=True, timeout=3600)
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"The recognizer failed: {detail[-800:]}")
    items = json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])["items"]
    return {item["id"]: item["text"] for item in items}


def transcribe(records, out_dir, *, run_asr=_run_asr):
    """Write the recognizer's manifest for the spoken clips and attach what it heard for each one."""
    spoken = [record for record in records if record["status"] == "ok"]
    if not spoken:
        return
    manifest = out_dir / "asr-manifest.json"
    manifest.write_text(json.dumps([{"id": record["id"], "audio": str(out_dir / record["wav"]),
                                     "language": ASR_CODE[record["column"]]} for record in spoken],
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    heard = run_asr(manifest)
    for record in spoken:
        record["heard"] = heard.get(record["id"], "")


def score(records):
    for record in records:
        if record["status"] != "ok":
            continue
        record["metric"] = metric_for(record["column"])
        if script_mismatch(record["text"], record["heard"]):
            expected = Counter(_families(record["text"])).most_common(1)[0][0]
            written = Counter(_families(record["heard"])).most_common(1)[0][0]
            record["error_rate"] = None
            record["reason"] = f"the recognizer wrote {written} letters for {expected} speech, so no word error is measured"
        else:
            record["error_rate"] = round(error_rate(record["text"], record["heard"], record["metric"]), 4)


def cell(clips, repeats):
    if not clips:
        return {"status": "not_run"}
    scored = [clip for clip in clips if clip["status"] == "ok" and clip.get("error_rate") is not None]
    if not scored:
        reason = next((clip["reason"] for clip in clips if clip.get("reason")), None)
        if any(clip["status"] == "ok" for clip in clips):
            return {"status": "inconclusive", "n": 0, "of": repeats, "reason": reason}
        unavailable = all(clip["status"] == "unavailable" for clip in clips)
        return {"status": "unavailable" if unavailable else "failed", "n": 0, "of": repeats, "reason": reason}
    rates = [clip["error_rate"] for clip in scored]
    median = round(statistics.median(rates), 3)
    return {"status": classify(median), "median": median, "best": min(rates), "worst": max(rates),
            "n": len(scored), "of": repeats, "metric": scored[0]["metric"],
            "devices": sorted({clip["device"] for clip in scored if clip["device"]})}


def summarize(records, *, engines, repeats, names):
    rows = []
    for engine in engines:
        claim = claims(engine)
        labels = claimed_labels(claim)
        claimed = claimed_columns(claim)
        cells = {}
        for code, _, _ in COLUMNS:
            if code in claimed:
                cells[code] = cell([r for r in records if r["engine"] == engine and r["column"] == code], repeats)
        rows.append({"engine": engine, "name": names.get(engine, engine),
                     "devices": sorted({r["device"] for r in records if r["engine"] == engine and r["device"]}),
                     "claims": {**claim, "labels": labels},
                     "not_screened": [label for label in labels if label not in COLUMN_OF_LABEL],
                     "cells": cells})
    return rows


def evidence(records, *, engines, repeats, suite, started, finished, device, names, gpu=None):
    sentences = {}
    for record in records:
        sentences.setdefault(record["column"], {"label": COLUMN_LABEL[record["column"]], "case_id": record["case_id"], "text": record["text"]})
    return {
        "schema": SCHEMA,
        "suite": {"id": SUITE_ID, "version": suite["version"], "repeats": repeats},
        "method": {"understood_max": UNDERSTOOD_MAX, "partial_max": PARTIAL_MAX, "recognizer": ASR_NOTE,
                   "character_scored_columns": sorted(CHARACTER_SCORED),
                   "sentences": [{"column": code, **sentences[code]} for code, _, _ in COLUMNS if code in sentences]},
        "run": {"started": started, "finished": finished, "device": device, "gpu": gpu},
        "columns": [{"code": code, "label": label, "metric": metric_for(code)} for code, label, _ in COLUMNS],
        "engines": summarize(records, engines=engines, repeats=repeats, names=names),
        "clips": [{field: record.get(field) for field in CLIP_FIELDS} for record in records],
    }


# Engines whose language label never reaches the speech runtime. Their labels are metadata, so the screen measures the text.
METADATA_LANGUAGE_ENGINES = engine_adapters.table("language_is_metadata")
# Reasons that explain a missing screen without a claim failing: the voice pack or the engine is not on this machine.
EXPLAINED_GAPS = ("voice pack", "not installed")


def picker_gaps(report):
    """Scene-picker languages this screen did not clear. Explained gaps and metadata labels are left out."""
    gaps = []
    for row in report["engines"]:
        if row["engine"] in METADATA_LANGUAGE_ENGINES:
            continue
        for label in row["claims"]["scene_allowlist"]:
            code = COLUMN_OF_LABEL.get(label)
            if code is None:
                continue
            summary = row["cells"].get(code, {"status": "not_run"})
            if summary["status"] == "understood":
                continue
            if summary["status"] == "unavailable" and any(word in (summary.get("reason") or "") for word in EXPLAINED_GAPS):
                continue
            gaps.append({"engine": row["engine"], "label": label, "status": summary["status"], "median": summary.get("median")})
    return gaps


_MARKS = {"path": None, "stamp": None, "marks": {}}
_MARKS_LOCK = threading.Lock()


def unscreened_languages(engine, path=EVIDENCE):
    """Scene-picker languages the published screen did not clear for this engine. The app marks them unscreened."""
    try:
        stamp = Path(path).stat().st_mtime_ns
    except OSError:
        return []
    with _MARKS_LOCK:
        if _MARKS["path"] != str(path) or _MARKS["stamp"] != stamp:
            try:
                report = json.loads(Path(path).read_text(encoding="utf-8"))
            except ValueError:
                return []
            marks = {}
            for gap in picker_gaps(report):
                marks.setdefault(gap["engine"], []).append(gap["label"])
            _MARKS.update(path=str(path), stamp=stamp, marks={key: sorted(labels) for key, labels in marks.items()})
        return list(_MARKS["marks"].get(engine, []))


def _unscreened_lines(report):
    names = {row["engine"]: row["name"] for row in report["engines"]}
    by_engine = {}
    for gap in picker_gaps(report):
        detail = gap["status"].replace("_", " ") + (f" {gap['median']:.2f}" if gap["median"] is not None else "")
        by_engine.setdefault(gap["engine"], []).append(f"{gap['label']} ({detail})")
    return [f"- **{names[engine]}**: {', '.join(items)}." for engine, items in by_engine.items()] or ["- None."]


def _metadata_lines(report):
    lines = []
    for row in report["engines"]:
        if row["engine"] not in METADATA_LANGUAGE_ENGINES:
            continue
        by_status = {}
        for label in row["claims"]["scene_allowlist"]:
            code = COLUMN_OF_LABEL.get(label)
            if code is not None:
                status = row["cells"].get(code, {"status": "not_run"})["status"]
                by_status.setdefault(status, []).append(label)
        described = "; ".join(f"{status.replace('_', ' ')}: {', '.join(labels)}" for status, labels in sorted(by_status.items()))
        lines.append(f"- **{row['name']}** labels are metadata: {METADATA_LANGUAGE_ENGINES[row['engine']]}. "
                     f"At the text level, {described}. {len(row['not_screened'])} labels were not screened.")
    return lines


def _cell_text(cell_summary):
    if cell_summary is None:
        return "—"
    status = cell_summary["status"]
    if status in {"understood", "partial", "not_understood"}:
        word = status.replace("_", " ")
        partial = "" if cell_summary["n"] == cell_summary["of"] else f" ({cell_summary['n']}/{cell_summary['of']})"
        return f"{cell_summary['median']:.2f} {word}{partial}"
    return {"failed": "failed", "unavailable": "unavailable", "not_run": "not run", "inconclusive": "inconclusive"}[status]


def _engine_note(row):
    unavailable = sorted({f"{COLUMN_LABEL[code]}: {cell_summary['reason']}" for code, cell_summary in row["cells"].items()
                          if cell_summary["status"] in {"unavailable", "failed", "inconclusive"} and cell_summary.get("reason")})
    screened = [COLUMN_LABEL[code] for code, cell_summary in row["cells"].items() if cell_summary["status"] in {"understood", "partial", "not_understood"}]
    claims_text = row["claims"]
    parts = [
        f"- **{row['name']}** — devices: {', '.join(row['devices']) or 'none recorded'}.",
        f"  Scene picker: {', '.join(claims_text['scene_allowlist']) or 'none'}.",
    ]
    if claims_text["documented"]:
        parts.append(f"  Documented: {', '.join(claims_text['documented'])}.")
    if claims_text["upstream"]:
        parts.append(f"  Upstream card: {len(claims_text['upstream'])} languages.")
    parts.append(f"  Screened: {', '.join(screened) or 'none'}.")
    if row["not_screened"]:
        parts.append(f"  Not screened ({len(row['not_screened'])}): {', '.join(row['not_screened'])}.")
    if unavailable:
        parts.append(f"  Not run or not scored: {'; '.join(unavailable)}.")
    return "\n".join(parts)


def render(report):
    columns = report["columns"]
    rows = report["engines"]
    statuses = Counter(cell_summary["status"] for row in rows for cell_summary in row["cells"].values())
    header = ["Engine", "Device"] + [column["label"] + ("*" if column["metric"] == "cer" else "") for column in columns]
    lines = [
        "# Language screen",
        "",
        f"Recorded {report['run']['finished'][:10]} on the service device `{report['run']['device']}`"
        f" ({report['run']['gpu'] or 'accelerator name not read'}). Generated from"
        " [evidence/language-matrix.json](evidence/language-matrix.json) by `python language_matrix.py report`.",
        "",
        "Each cell is the median word error rate (character error rate for Chinese and Japanese) over the"
        " generations. The screen asks one question: after the engine speaks the sentence, does a speech"
        " recognizer write back the same words? It does not judge accent, voice quality, likeness or acting,"
        " and it is not a listening test.",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]
    for row in rows:
        cells = [_cell_text(row["cells"].get(column["code"])) for column in columns]
        lines.append("| " + " | ".join([row["name"], ", ".join(row["devices"]) or "—", *cells]) + " |")
    lines += [
        "",
        f"Cells: {statuses['understood']} understood, {statuses['partial']} partial, {statuses['not_understood']} not understood,"
        f" {statuses['failed']} failed, {statuses['unavailable']} unavailable, {statuses['inconclusive']} inconclusive.",
        "",
        "Key: `0.04 understood` means the median error rate is at most "
        f"{UNDERSTOOD_MAX:.2f}; `partial` is at most {PARTIAL_MAX:.2f}; `not understood` is above that. `failed` means"
        " generation raised an error. `unavailable` means the installation, voice pack or checkpoint lacks the language."
        " `inconclusive` means the recognizer wrote the speech in other letters, so no word error is measured.",
        " `(2/3)` means only some generations finished. `—` means the engine does not claim the language, so it was not run.",
        "",
        "\\* Scored by character error rate, because these scripts have no spaces between words.",
        "",
        "## Per engine",
        "",
    ]
    lines += [_engine_note(row) for row in rows]
    lines += [
        "",
        "## Method",
        "",
        f"- Recognizer: {report['method']['recognizer']}. Its own error on each language is not measured here, so"
        " compare cells with each other, not against an absolute scale.",
        f"- Thresholds, fixed before the first recorded run: understood at most {UNDERSTOOD_MAX:.2f}; partial at most {PARTIAL_MAX:.2f}.",
        "- Devices: `cuda` is the name PyTorch gives the accelerator; on this build it is the AMD GPU through ROCm."
        " `Vulkan1` is the Drama runtime's design device, reported by the worker.",
        f"- Generations: {report['suite']['repeats']} per cell from suite `{report['suite']['id']}` version {report['suite']['version']}."
        " Engines that take a seed get seeds 43 to 45; Kokoro is deterministic, so its repeats match.",
        "- Sentences contain no numerals: the recognizer writes numbers as digits, which would count as word errors.",
        "- A transcript written in other letters than its sentence (for example Latin letters for Hindi) is inconclusive:"
        " no word error is measured for it.",
        "- Sentences (translated by the project; no native speaker has checked them):",
        "",
        "| Column | Suite label | Sentence |",
        "|---|---|---|",
    ]
    lines += [f"| {item['column']} | {item['label']} | {item['text']} |" for item in report["method"]["sentences"]]
    lines += [
        "",
        "## Unscreened picker entries",
        "",
        "The app keeps these scene-picker entries and marks each one `(unscreened)` in every language picker for its engine,"
        " because this screen did not clear it. Remove them or re-screen them before any claim that the model speaks the language.",
        "",
        *_unscreened_lines(report),
        "",
        *_metadata_lines(report),
        "",
        "## Known confounds",
        "",
        "- Base checkpoints (1.7B and 0.6B) clone the bundled English reference voice for every language, so their cells"
        " measure cross-lingual cloning, not a native reference.",
        "- CustomVoice uses one preset speaker, the first the checkpoint reports, for every language.",
        "- Japanese: the recognizer often writes katakana for kana and kanji words (マモナク for まもなく). The character"
        " metric counts that as errors, and several engines score the same clip the same way, which points at spelling"
        " rather than speech.",
        "- Korean: spacing differences count as word errors.",
        "- Portuguese: the recognizer often writes \"trein\" for \"trem\", so these cells are the most sensitive to recognizer quality.",
        "",
        "## Limits",
        "",
        "- This is a screen. A cell marked understood means the recognizer wrote back the sentence; it says nothing about"
        " accent, emotion, identity or whether a fluent speaker would accept the speech.",
        "- One short sentence per language, without numbers or names. Longer text, dialects and mixed-language lines are not screened.",
        "- The cells were generated on the service device above. CPU and other accelerators are not part of this matrix.",
        "- Drama 3's language label is stored with each job, but neither Drama path sends it to the speech runtime."
        " Perform and scenes lose it in `EnginePool.speak`, and the studio's `stream_drama3` never reads it."
        " Its cells therefore measure the text alone.",
        "- The scene picker lists what the app accepts for each engine, which can differ from what was screened. Only the"
        " cells marked understood are screened results; the others are untested, unavailable or failed.",
    ]
    return "\n".join(lines) + "\n"


def _now_stamp():
    return utc_now().replace(":", "").replace("-", "")


def gpu_name():
    """The accelerator's name as the Qwen environment's torch reports it, or None when that cannot be read."""
    python = LAB / ".venv-fqwen" / "Scripts" / "python.exe"
    try:
        completed = subprocess.run(
            [str(python), "-I", "-c", "import torch; print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"],
            cwd=str(LAB), capture_output=True, text=True, encoding="utf-8", timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = completed.stdout.strip().splitlines()
    return lines[-1].strip() if completed.returncode == 0 and lines and lines[-1].strip() else None


def sanitize(value):
    """Replace this machine's folders with neutral names, so the published evidence carries no personal location."""
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return value.replace(str(LAB), "voice-lab").replace(LAB.as_posix(), "voice-lab").replace(str(Path.home()), "~")
    return value


def coverage_problems(report):
    """Why a run is not the full matrix: a missing engine, a cell that differs from its claims, or a short clip count."""
    problems = []
    rows = {row["engine"]: row for row in report["engines"]}
    expected = 0
    for engine in ENGINE_ORDER:
        claimed = claimed_columns(claims(engine))
        expected += len(claimed) * report["suite"]["repeats"]
        if engine not in rows:
            problems.append(f"{engine} is missing")
        elif set(rows[engine]["cells"]) != claimed:
            problems.append(f"{engine} cells differ from its claims")
    if len(report["clips"]) != expected:
        problems.append(f"{len(report['clips'])} clips recorded; {expected} expected")
    return problems


def publish(source, *, evidence_path=EVIDENCE, doc_path=PUBLIC_DOC):
    """Check that a run covers every claimed cell, then write its sanitized evidence and the matrix page."""
    report = json.loads(Path(source).read_text(encoding="utf-8"))
    problems = coverage_problems(report)
    if problems:
        raise SystemExit("Not a full run, so it cannot be published: " + "; ".join(problems))
    report = sanitize(report)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    doc_path.write_text(render(report), encoding="utf-8")
    return report


def save_records(out_dir, meta, records):
    """Write every clip's record before the recognizer runs, so a recognizer failure never loses a generation."""
    (out_dir / "records.json").write_text(json.dumps({"meta": meta, "records": records}, ensure_ascii=False, indent=2),
                                          encoding="utf-8")


def load_records(out_dir):
    saved = json.loads((out_dir / "records.json").read_text(encoding="utf-8"))
    return saved["meta"], saved["records"]


def finish(records, *, meta, out_dir, suite, run_asr=_run_asr):
    """Recognize and score the saved clips, then write the evidence beside them."""
    transcribe(records, out_dir, run_asr=run_asr)
    score(records)
    report = evidence(records, engines=meta["engines"], repeats=meta["repeats"], suite=suite,
                      started=meta["started"], finished=utc_now(), device=meta["device"], names=meta["names"],
                      gpu=meta["gpu"])
    evidence_file = out_dir / "evidence.json"
    evidence_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence_file


def _run(args):
    from common import pick_device  # imported first: it points the model cache at this lab and selects the GPU
    from kokoro_support import discover_kokoro_voices
    from worker_pool import EnginePool

    suite = get_suite(SUITE_ID)
    if args.resume:
        out_dir = args.resume
        meta, records = load_records(out_dir)
    else:
        repeats = args.repeats or suite["repeat_count"]
        started = utc_now()
        out_dir = args.out_dir or LAB / "out" / "language-matrix" / _now_stamp()
        out_dir.mkdir(parents=True, exist_ok=True)
        names = {entry["id"]: entry.get("name", entry["id"]) for entry in read_catalog()["engines"]}
        engines = [engine for engine in ENGINE_ORDER if not args.engine or engine in args.engine]
        plan = build_plan(suite=suite, engines=engines, columns=args.column, repeats=repeats, voices=discover_kokoro_voices())
        pool = EnginePool(pick_device("auto"), idle_minutes=5)
        try:
            records = generate(pool, plan, out_dir, installed=lambda entry: engine_checks.installation(entry, LAB))
        finally:
            pool.close()
        meta = {"started": started, "device": pool.device, "gpu": gpu_name(), "engines": engines,
                "repeats": repeats, "names": names}
        save_records(out_dir, meta, records)  # kept on disk before the recognizer runs
    evidence_file = finish(records, meta=meta, out_dir=out_dir, suite=suite)
    statuses = Counter(record["status"] for record in records)
    print(f"Clips: {statuses['ok']} spoken, {statuses['failed']} failed, {statuses['unavailable']} unavailable.")
    print(f"Evidence: {evidence_file}")
    print(f'Publish after review: python language_matrix.py publish "{evidence_file}"')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="speak, recognize and score the planned cells")
    run.add_argument("--engine", action="append", choices=ENGINE_ORDER, help="limit to one engine (repeatable)")
    run.add_argument("--column", action="append", choices=[code for code, _, _ in COLUMNS],
                     help="limit to one language column (repeatable)")
    run.add_argument("--repeats", type=int, help="generations per cell (defaults to the suite's repeat_count)")
    run.add_argument("--out-dir", type=Path, help="where clips, the manifest and the evidence file go")
    run.add_argument("--resume", type=Path,
                     help="recognize and score an earlier run's records.json instead of speaking the clips again")
    publish_command = commands.add_parser("publish", help="publish a full run's evidence and render the matrix page")
    publish_command.add_argument("evidence", type=Path, help="evidence.json written by a full run")
    commands.add_parser("report", help="re-render docs/LANGUAGE_MATRIX.md from the published evidence")
    args = parser.parse_args(argv)
    if args.command == "publish":
        publish(args.evidence)
        print(f"Wrote {EVIDENCE} and {PUBLIC_DOC}")
        return 0
    if args.command == "report":
        PUBLIC_DOC.write_text(render(json.loads(EVIDENCE.read_text(encoding="utf-8"))), encoding="utf-8")
        print(f"Wrote {PUBLIC_DOC}")
        return 0
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
