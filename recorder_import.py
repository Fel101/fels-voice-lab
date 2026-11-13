"""Bring a Voice Slate kept clip into the Cast library as a cloning reference, with its provenance, and check it.

    .venv\\Scripts\\python.exe recorder_import.py <Voice Slate folder>\\voice-clips\\my-voice            list and validate
    .venv\\Scripts\\python.exe recorder_import.py <clip>.json --check-identity                           and compare with the master
    .venv\\Scripts\\python.exe recorder_import.py <clip>.json --register --character "Narrator"          import it

Without --register nothing is written. Import is a choice made clip by clip: a speaker folder needs --clip ID (or
--all). A clip is checked before anything is copied (schema, consent, transcript, paths inside its own folder, and the
WAV's SHA-256 against its sidecar), then registered through CastStore.add_reference, which copies it into
data/cast/media/references/<id>/ (a folder the workers already allow) next to a verbatim copy of its sidecar. The
master is never copied; the identity check reads it where it is and stores only numbers. Voice Slate's folder is not
added to any allowlist and nothing in it is changed. The new voice is a *candidate*: approving it stays with you.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf

import engine_adapters
from cast_store import (LAB, MAX_REFERENCE_SECONDS, MAX_REFERENCE_TEXT, MIN_REFERENCE_SECONDS, REFERENCE_ENGINES,
                        REFERENCE_POLICIES, CastStore, ID_RE)
from recorder_clip import ClipRefused, RecorderClip, find_sidecars, load_clip
from recorder_identity import DEFAULT_MIN_SIMILARITY, check_identity, store_result
from reference_paths import resolve_reference_path

DEFAULT_ENGINE = "turbo"
MAX_ORIGIN, MAX_ALLOWED_USE = 1000, 1000     # CastStore.add_reference limits
CLIP_FILE, IMPORT_FILE = "recorder-clip.json", "recorder-import.json"
IMPORT_SCHEMA = "voice-lab-recorder-import-v1"


def _plain(text, limit):
    return "".join(ch if ord(ch) >= 32 else " " for ch in str(text)).strip()[:limit]


def compose_origin(clip):
    """The person's own origin text, then a note that ties the voice to the Voice Slate clip it came from.

    The origin is copied into the provenance of every take made from the reference, so the note is how a finished
    take can be traced back to the clip.
    """
    note = (f"[Voice Slate clip {clip.id}; speaker {_plain(clip.speaker, 40)}; "
            f"{_plain(clip.source, 20) or 'unknown source'}; kept {_plain(clip.created, 40) or 'unknown time'}; "
            f"sha256 {clip.export_sha256[:12]}]")
    return f"{clip.origin} {note}"


def _check_registrable(clip, engine_id):
    """Everything the Cast store or the worker policy would refuse, found before the first store write."""
    if engine_id not in REFERENCE_ENGINES:
        raise ClipRefused(f"{engine_id!r} is not a reference-cloning engine; choose one of "
                          f"{', '.join(sorted(REFERENCE_ENGINES))}")
    problems = []
    if len(clip.transcript) > MAX_REFERENCE_TEXT:
        problems.append(f"the transcript is {len(clip.transcript)} characters; voice-lab keeps at most {MAX_REFERENCE_TEXT}")
    if len(compose_origin(clip)) > MAX_ORIGIN:
        problems.append(f"the origin plus the provenance note is over {MAX_ORIGIN} characters; shorten the origin")
    if len(clip.allowed_use) > MAX_ALLOWED_USE:
        problems.append(f"the allowed use is {len(clip.allowed_use)} characters; voice-lab keeps at most {MAX_ALLOWED_USE}")
    longest = REFERENCE_POLICIES.get(engine_id, {}).get("max_seconds", MAX_REFERENCE_SECONDS)
    if not MIN_REFERENCE_SECONDS <= clip.duration_seconds <= longest:
        problems.append(f"the clip is {clip.duration_seconds:.1f} s; {engine_id} takes references of "
                        f"{MIN_REFERENCE_SECONDS:g} to {longest:g} s")
    # A model with a shortest voice recording of its own (Turbo's, which resolve_reference_path also refuses)
    adapter = engine_adapters.get(engine_id)
    too_short = adapter.reference_problem(clip.duration_seconds) if adapter is not None else None
    if too_short:
        problems.append(too_short)
    samples = _mono(sf.read(io.BytesIO(clip.wav_bytes), dtype="float32", always_2d=True)[0])
    if float(np.max(np.abs(samples))) < 1e-6 or math.sqrt(float(np.mean(np.square(samples, dtype=np.float64)))) < 1e-3:
        problems.append("the clip is silent or quieter than -60 dBFS, which the Cast store refuses")
    if problems:
        raise ClipRefused(problems)


def _mono(samples):
    return samples.mean(axis=1) if samples.ndim == 2 else samples


def _characters(store):
    return store.list_cast() if store.db_path.exists() else []


def _find_character(store, name_or_id):
    name_or_id = str(name_or_id or "").strip()
    if not name_or_id:
        raise ClipRefused("name the Cast character this voice is for (--character)")
    cast = _characters(store)
    if ID_RE.fullmatch(name_or_id):
        return next((row for row in cast if row["id"] == name_or_id), None)
    matches = [row for row in cast if row["name"].casefold() == name_or_id.casefold()]
    if len(matches) > 1:
        raise ClipRefused(f"{len(matches)} characters are named {name_or_id!r}; pass the character's id instead")
    return matches[0] if matches else None


def _default_label(clip):
    return f"Voice Slate: {_plain(clip.speaker, 60)}"


def _find_identity(character, engine_id, label):
    if character is None:
        return None
    return next((row for row in character["identities"]
                 if row["engine_id"] == engine_id and row["kind"] == "reference" and row["label"] == label), None)


def _same_registration(reference, clip):
    """Same audio with the same transcript, origin and allowed use: nothing new to register."""
    return (reference["source_sha256"] == clip.export_sha256 and reference["transcript"] == clip.transcript and
            reference["allowed_use"] == clip.allowed_use and reference["origin"] == compose_origin(clip))


def plan_import(clip, *, character, engine_id=DEFAULT_ENGINE, label=None, create_character=False, store=None):
    """What importing would do, without doing it. Raises ClipRefused for anything that would be refused."""
    store = store or CastStore(LAB)
    _check_registrable(clip, engine_id)
    found = _find_character(store, character)
    name = str(character).strip()
    if found is None:
        if ID_RE.fullmatch(name):
            raise ClipRefused(f"no Cast character has the id {name}")
        if not create_character:
            raise ClipRefused(f"no Cast character is named {name!r}; check the spelling or pass --create-character")
        if len(name) > 80:
            raise ClipRefused("a character name has at most 80 characters")
    label = (_plain(label, 80) if label else "") or _default_label(clip)
    identity = _find_identity(found, engine_id, label)
    existing = next((ref for ref in identity["references"] if _same_registration(ref, clip)), None) if identity else None
    earlier = next((ref for ref in identity["references"] if ref["source_sha256"] == clip.export_sha256),
                   None) if identity else None
    return {"character": {"id": found["id"] if found else None, "name": found["name"] if found else name,
                          "exists": found is not None},
            "identity": {"id": identity["id"] if identity else None, "label": label, "engine_id": engine_id,
                         "exists": identity is not None},
            "reference": {"id": existing["id"] if existing else None, "already_registered": existing is not None,
                          "earlier_import_with_other_details": bool(earlier and not existing)}}


def _import_record(clip, ids, warnings, stamp):
    data = clip.data
    return {"schema": IMPORT_SCHEMA, "imported_at": stamp,
            "recorder_clip": {"id": clip.id, "speaker": clip.speaker, "schema": clip.schema, "created": clip.created,
                              "source": clip.source, "export_sha256": clip.export_sha256,
                              "sidecar_sha256": clip.sidecar_sha256, "transcript_status": clip.transcript_status,
                              "consent_confirmed": data.get("consent_confirmed"), "flags": clip.flags,
                              "noise_reduction": clip.denoise, "master_file": data["master"].get("file"),
                              "master_seconds": data.get("master_seconds")},
            "read_from": clip.sidecar_path.name,          # the file's name only: where it sat on this computer is private
            "voice_lab": ids, "warnings": list(warnings),
            "note": ("consent_confirmed is the speaker's own statement in Voice Slate, not a verified release. The master "
                     "recording was not copied. Identity checks are kept beside this file as identity-check-*.json.")}


def _write_new(folder, name, payload):
    """Write a file in a reference folder unless it is already there. Returns whether it was written."""
    path = folder / name
    if path.exists():
        return False
    with path.open("xb") as handle:
        handle.write(payload)
    return True


def import_clip(clip, *, character, engine_id=DEFAULT_ENGINE, label=None, create_character=False, store=None,
                identity_check=True, min_similarity=DEFAULT_MIN_SIMILARITY, against=None, embed=None,
                accept_asr_draft=False):
    """Register one kept clip as a candidate voice reference and (by default) check its identity against its master.

    ``clip`` is a RecorderClip or the path of its sidecar. Everything is validated before the first write. Importing
    the same audio again with the same transcript, origin and allowed use registers nothing new; if those changed (a
    transcript corrected in Voice Slate, say) the clip becomes a new reference version and the old one stays.
    Returns a report dict; raises ClipRefused if the clip or the request is refused.
    """
    if not isinstance(clip, RecorderClip):
        clip = load_clip(clip, accept_asr_draft=accept_asr_draft)
    store = store or CastStore(LAB)
    plan = plan_import(clip, character=character, engine_id=engine_id, label=label,
                       create_character=create_character, store=store)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    created = []
    if not plan["character"]["exists"]:
        found = store.create_character(plan["character"]["name"], f"Voice Slate speaker {_plain(clip.speaker, 60)!r}.",
                                       tags=["voice-slate"])
        created.append("character")
    else:
        found = _find_character(store, plan["character"]["id"])
    identity = _find_identity(found, engine_id, plan["identity"]["label"])
    if identity is None:
        identity = store.create_identity(found["id"], engine_id, plan["identity"]["label"],
                                         notes="Candidate voice from Voice Slate clips; approve it in Cast after auditioning.")
        created.append("identity")
    if plan["reference"]["already_registered"]:
        reference, status = store.get_reference(plan["reference"]["id"]), "already_registered"
    else:
        reference = store.add_reference(
            identity["id"], clip.wav_path.name, clip.wav_bytes, transcript=clip.transcript,
            origin=compose_origin(clip), allowed_use=clip.allowed_use,
            edits={"target_sample_rate": 24_000, "channel": "mono"})
        status = "registered"
        created.append("reference")
    ids = {"character_id": found["id"], "identity_id": identity["id"], "reference_id": reference["id"],
           "engine_id": engine_id, "reference_version": reference["version"]}
    warnings = list(clip.warnings)
    if status == "registered" and plan["reference"]["earlier_import_with_other_details"]:
        warnings.append("this audio was registered before with a different transcript, origin or allowed use; "
                        "this is a new reference version and the earlier one is kept")
    folder = store.get_media_path("reference_source", reference["id"]).parent
    record = (json.dumps(_import_record(clip, ids, warnings, stamp), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    written = [name for name, payload in ((CLIP_FILE, clip.sidecar_bytes), (IMPORT_FILE, record))
               if _write_new(folder, name, payload)]
    if status == "already_registered" and written:
        status = "completed"                 # a first import that stopped before its provenance files were written
    audio = store.get_media_path("reference", reference["id"])
    try:
        resolve_reference_path(engine_id, audio, extra_roots=(store.data_root / "media" / "references",))
    except ValueError as exc:             # registered, so this would be a bug here, not a bad clip: say so plainly
        raise RuntimeError(f"Reference {reference['id']} was registered but the worker policy refuses it: {exc}") from exc
    report = {"status": status, "created": created, "wrote_provenance": written, **ids,
              "reference_audio": str(audio), "folder": str(folder), "clip": clip.summary(), "warnings": warnings,
              "identity_check": None, "identity_check_file": None}
    if identity_check:
        report["identity_check"] = check_identity(clip, min_similarity=min_similarity, against=against, embed=embed)
        report["identity_check_file"] = str(store_result(store, reference["id"], report["identity_check"]))
    return report


# ---- command line -----------------------------------------------------------------------------------------------

def _clip_sidecars(target):
    if target.is_dir():
        found = find_sidecars(target)
        if not found:
            hint = (" It holds speaker folders: name one of them." if any(
                find_sidecars(child) for child in target.iterdir() if child.is_dir()) else "")
            raise ClipRefused(f"no Voice Slate clip sidecars are directly inside {target}.{hint}")
        return found, True
    if target.suffix.lower() == ".wav":
        target = target.with_suffix(".json")
    if not target.is_file():
        raise ClipRefused(f"{target} does not exist")
    return [target], False


def _sidecar_id(path):
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
        return data.get("id") if isinstance(data, dict) else None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None


def _say(text=""):
    print(text)


def _show_clip(clip):
    denoise = clip.denoise
    noise = (f"ON (afftdn nf={denoise['nf']:g})" if denoise["nf"] is not None else "ON") if denoise["enabled"] else "off"
    _say(f"  {clip.id}  {clip.wav_path.name}  {clip.duration_seconds:.1f} s  transcript: {clip.transcript_status or '?'}")
    _say(f"    \"{_plain(clip.transcript, 90)}\"")
    _say(f"    origin: {_plain(clip.origin, 90)}")
    _say(f"    allowed use: {_plain(clip.allowed_use, 90)}")
    _say(f"    noise reduction: {noise}" + ("  (Voice Slate did not check the voice still matches)"
                                            if denoise["enabled"] and not denoise["recorder_identity_checked"] else ""))
    for warning in clip.warnings:
        _say(f"    note: {warning}")


def _show_check(result):
    if result["status"] != "checked":
        _say("    identity: not checked - " + " ".join(result["reasons"]))
        return
    method = result["method"]
    _say(f"    identity: {result['score']:.3f} against the master (threshold {result['threshold']:.2f}) -> "
         f"{result['verdict'].upper()}  [{method['master_reference']}"
         + (f": {method['master_reference_note']}" if method["master_reference_note"] else "") + "]")
    for reason in result["reasons"]:
        _say(f"      {reason}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="a kept clip's sidecar (.json), the kept .wav, or one speaker's folder")
    parser.add_argument("--register", action="store_true", help="import into the Cast library (otherwise nothing is written)")
    parser.add_argument("--character", help="the Cast character (name or id) this voice is a reference for")
    parser.add_argument("--create-character", action="store_true", help="create --character if no character has that name")
    parser.add_argument("--engine", default=DEFAULT_ENGINE, help=f"cloning engine for the voice (default {DEFAULT_ENGINE})")
    parser.add_argument("--label", help="the voice's label (default: Voice Slate: <speaker>)")
    parser.add_argument("--clip", action="append", default=[], metavar="ID", help="only this clip id; repeatable")
    parser.add_argument("--all", action="store_true", help="every valid clip in a speaker folder (with --register)")
    parser.add_argument("--accept-asr-draft", action="store_true",
                        help="accept a transcript the speech recognizer drafted and nobody corrected")
    parser.add_argument("--check-identity", action="store_true",
                        help="compare each clip with its master (--register always does, unless --no-identity-check)")
    parser.add_argument("--no-identity-check", action="store_true", help="with --register, skip the identity check")
    parser.add_argument("--against", metavar="WAV",
                        help="a clean recording of the same voice: the check says whether the clip moved toward or away")
    parser.add_argument("--min-similarity", type=float, default=DEFAULT_MIN_SIMILARITY,
                        help=f"flag a clip scoring below this (default {DEFAULT_MIN_SIMILARITY})")
    parser.add_argument("--store-root", help="the voice-lab folder whose data/cast is used (default: this one)")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        sidecars, is_folder = _clip_sidecars(Path(args.path).expanduser().resolve())
    except ClipRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.clip:
        sidecars = [path for path in sidecars if _sidecar_id(path) in args.clip]
        if not sidecars:
            print(f"ERROR: none of the clip ids {', '.join(args.clip)} is in {args.path}", file=sys.stderr)
            return 2
    if not 0.0 < args.min_similarity <= 1.0:
        print("ERROR: --min-similarity must be above 0 and at most 1", file=sys.stderr)
        return 2
    if args.register and not args.character:
        print("ERROR: --register needs --character (the Cast character this voice is for)", file=sys.stderr)
        return 2
    if args.register and is_folder and not (args.clip or args.all):
        print("ERROR: a folder holds several clips: choose them with --clip ID (repeatable), or pass --all",
              file=sys.stderr)
        return 2
    store = CastStore(args.store_root) if args.store_root else CastStore(LAB)
    do_check = args.check_identity or (args.register and not args.no_identity_check)
    results, failed = [], False
    for path in sidecars:
        entry = {"sidecar": str(path), "status": "valid", "problems": [], "clip": None, "plan": None,
                 "identity_check": None, "registered": None}
        try:
            clip = load_clip(path, accept_asr_draft=args.accept_asr_draft)
        except ClipRefused as exc:
            entry.update(status="refused", problems=exc.problems)
            failed = True
        else:
            entry["clip"] = clip.summary()
            try:
                if args.register:
                    report = import_clip(clip, character=args.character, engine_id=args.engine, label=args.label,
                                         create_character=args.create_character, store=store, identity_check=do_check,
                                         min_similarity=args.min_similarity, against=args.against)
                    entry.update(status=report["status"], registered=report, identity_check=report["identity_check"])
                else:
                    if args.character:
                        entry["plan"] = plan_import(clip, character=args.character, engine_id=args.engine, label=args.label,
                                                    create_character=args.create_character, store=store)
                    if do_check:
                        entry["identity_check"] = check_identity(clip, min_similarity=args.min_similarity,
                                                                 against=args.against)
            except ClipRefused as exc:
                entry.update(status="refused", problems=exc.problems)
                failed = True
        results.append(entry)
        if not args.json:
            _show_entry(entry, clip if entry["clip"] else None)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif not args.register:
        _say("\nNothing was imported (without --register nothing is written). Import a clip with:\n"
             "  recorder_import.py <clip>.json --register --character NAME")
    return 2 if failed and (args.register or not is_folder) else 0


def _show_entry(entry, clip):
    name = Path(entry["sidecar"]).name
    if entry["status"] == "refused":
        _say(f"[REFUSED] {name}")
        for problem in entry["problems"]:
            _say(f"    - {problem}")
        return
    _say(f"[{'ok' if entry['status'] == 'valid' else entry['status']}] {name}")
    _show_clip(clip)
    if entry["plan"]:
        plan = entry["plan"]
        _say(f"    would use: character {plan['character']['name']!r}"
             f"{'' if plan['character']['exists'] else ' (new)'}, voice {plan['identity']['label']!r}"
             f"{'' if plan['identity']['exists'] else ' (new)'} on {plan['identity']['engine_id']}"
             + ("; already registered" if plan["reference"]["already_registered"] else ""))
    if entry["registered"]:
        reg = entry["registered"]
        _say(f"    -> reference {reg['reference_id']} (version {reg['reference_version']}) under character "
             f"{reg['character_id']}, voice {reg['identity_id']} ({reg['engine_id']}, candidate)")
    if entry["identity_check"]:
        _show_check(entry["identity_check"])


if __name__ == "__main__":
    raise SystemExit(main())
