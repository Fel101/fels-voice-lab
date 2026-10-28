"""Bounded, local reference discovery and preview validation.

``documented`` means provenance and allowed-use text were saved by the user. It
does not certify consent, copyright ownership, or a model's commercial terms.
An audio filename alone never establishes those rights.
"""

import json
import hashlib
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import stat
from urllib.parse import quote

import soundfile as sf

from output_license import license_profile


AUDIO_TYPES = {".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg"}
MAX_AUDIO_BYTES = 100 * 1024 * 1024
MAX_SIDECAR_BYTES = 64 * 1024
MAX_ITEMS = 500
MAX_CANDIDATES = 10_000


def _engine_names(lab):
    """Use recorded catalogue names, falling back to an engine ID, never a guess."""
    candidate = Path(lab) / "catalog/engines.json"
    if not candidate.is_file():
        candidate = Path(__file__).resolve().parent / "catalog/engines.json"
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
        return {row["id"]: row.get("name") or row["id"] for row in data.get("engines", [])
                if isinstance(row, dict) and isinstance(row.get("id"), str)}
    except (OSError, ValueError, UnicodeError):
        return {}


def _character_fields(character):
    tags = [tag for tag in character.get("tags", []) if isinstance(tag, str)]
    return {
        "character_id": character.get("id"), "character_name": str(character.get("name", "")),
        "character_description": str(character.get("description", "")), "character_tags": tags,
    }


def _take_generation_engine(take):
    model = take.get("model") or {}
    value = model.get("source_engine_id") or model.get("engine_id")
    return value if isinstance(value, str) and value else None


def _take_index(cast):
    by_identity, by_id = {}, {}
    # Bound metadata reads as well as the emitted catalogue.
    ids = cast._rows("SELECT id FROM takes ORDER BY created_at DESC,id DESC LIMIT ?", (MAX_CANDIDATES,))
    for take in cast.get_takes([item[0] for item in ids]):
        by_identity.setdefault(take["identity_id"], []).append(take)
        by_id[take["id"]] = take
    return by_identity, by_id


def _reference_generation(reference, identity_takes, takes_by_id):
    parent = takes_by_id.get(reference.get("parent_take_id"))
    if parent:
        return _take_generation_engine(parent), parent["id"]
    # A take conditioned on a reference is not necessarily the source of that
    # reference. Only claim a source when its audio checksum matches the source
    # upload or processed version, and the take records an actual engine.
    checksums = {value for value in (reference.get("source_sha256"), reference.get("audio_sha256")) if value}
    for take in sorted(identity_takes, key=lambda item: (item.get("created_at", ""), item["id"])):
        if take.get("audio_sha256") in checksums and _take_generation_engine(take):
            return _take_generation_engine(take), take["id"]
    return None, None


def save_designed_reference(path, audio, request, result, model):
    """Save one new design WAV and its provenance together, never overwriting.

    Only the server's validated design-save paths call this helper. No existing
    asset is relabeled, and model/output restrictions come from output_license.
    """
    path = Path(path)
    sidecar = path.with_suffix(".json")
    engine = request["engine"]
    profile = license_profile(engine, model, {})
    checkpoint = result.get("checkpoint") or model.get("checkpoint") or "unrecorded checkpoint"
    revision = result.get("checkpoint_revision") or model.get("local_revision") or "unrecorded revision"
    metadata = {
        "schema": "voice-lab/designed-reference-v1",
        "label": str(request["save_voice_as"]).rsplit("/", 1)[-1],
        "description": str(request.get("voice_description") or ""),
        "transcript": str(request.get("text") or ""),
        "origin": f"Original synthetic voice design via {engine}; {checkpoint} revision {revision}.",
        "allowed_use": f"{profile['weights_license']} {profile['output_use']}",
        "engine_id": engine, "checkpoint": checkpoint, "checkpoint_revision": revision,
        "license": profile, "audio_sha256": hashlib.sha256(audio).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    encoded = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    audio_created = sidecar_created = False
    try:
        with path.open("xb") as output:
            audio_created = True
            with sidecar.open("xb") as provenance:
                sidecar_created = True
                output.write(audio)
                provenance.write(encoded)
    except BaseException:
        # Roll back only files created by this call; preserve prior collisions.
        if audio_created:
            path.unlink(missing_ok=True)
        if sidecar_created:
            sidecar.unlink(missing_ok=True)
        raise
    return metadata


def _linked(path):
    """Reject symlinks and Windows reparse points, including directory junctions."""
    details = path.lstat()
    return (stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _unlinked_below(root, path):
    try:
        relative = path.relative_to(root)
        current = root
        if _linked(current):
            return False
        for part in relative.parts:
            current = current / part
            if _linked(current):
                return False
        return True
    except (OSError, ValueError):
        return False


def _roots(lab, cast):
    roots = []
    for base, relative, source, label in (
        (Path(lab), "reference", "reference", "Local reference folder"),
        (Path(lab), "data/voices", "voice_asset", "Saved designed voice"),
        (Path(cast.root), "data/cast/media/references", "cast_reference", "Cast reference"),
    ):
        try:
            base = base.resolve(strict=True)
            root = base / relative
            if root.is_dir() and _unlinked_below(base, root):
                roots.append((root, source, label))
        except (OSError, RuntimeError):
            continue
    return roots


def _cast_metadata(cast, names=None):
    names = names or {}
    identity_takes, takes_by_id = _take_index(cast)
    rows = {}
    for character in cast.list_cast():
        character_fields = _character_fields(character)
        for identity in character.get("identities", []):
            preferred = _preferred_reference(identity)
            preferred_id = preferred["id"] if preferred else None
            for reference in identity.get("references", []):
                if reference.get("retired"):
                    continue
                if len(rows) >= MAX_CANDIDATES:
                    return rows
                try:
                    # Use Cast's media resolver, including its own containment
                    # checks, rather than trusting a saved relative path.
                    path = Path(cast.get_media_path("reference", reference["id"]))
                except (OSError, RuntimeError, KeyError, ValueError):
                    continue
                source_engine, source_take = _reference_generation(
                    reference, identity_takes.get(identity["id"], []), takes_by_id)
                rows[path.absolute()] = {
                    **character_fields,
                    "reference_version_id": reference["id"],
                    "preferred_reference_id": preferred_id,
                    "is_preferred_reference": reference["id"] == preferred_id,
                    "identity_id": identity["id"], "identity_status": identity.get("status"),
                    "identity_label": str(identity.get("label", "")),
                    "engine_id": identity["engine_id"],
                    "engine_name": names.get(identity["engine_id"], identity["engine_id"]),
                    "generation_engine_id": source_engine,
                    "generation_engine_name": names.get(source_engine, source_engine),
                    "generation_source_take_id": source_take,
                    "version": reference.get("version"),
                    "description": character_fields["character_description"],
                    "source_file": str(reference.get("source_name", "")),
                    "transcript": str(reference.get("transcript", "")),
                    "origin": str(reference.get("origin", "")),
                    "allowed_use": str(reference.get("allowed_use", "")),
                }
    return rows


def _audio_details(path):
    size = path.stat().st_size
    if not path.is_file() or path.suffix.lower() not in AUDIO_TYPES or not 0 < size <= MAX_AUDIO_BYTES:
        raise ValueError("Reference preview must be WAV, FLAC, or OGG audio no larger than 100 MiB.")
    try:
        details = sf.info(path)
    except (OSError, RuntimeError, sf.LibsndfileError) as exc:
        raise ValueError("Reference audio could not be read.") from exc
    if (not math.isfinite(details.duration) or details.duration <= 0 or
            details.frames <= 0 or details.samplerate <= 0 or details.channels <= 0):
        raise ValueError("Reference audio is empty or invalid.")
    return size, details


def _sidecar(path, root):
    candidate = path.with_suffix(".json")
    try:
        if not _unlinked_below(root, candidate) or candidate.stat().st_size > MAX_SIDECAR_BYTES:
            return {}
        value = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
    except (OSError, ValueError, UnicodeError):
        return {}
    # Do not expose arbitrary adjacent JSON, internal paths, or unbounded text.
    result = {key: value[key].strip()[:8000] for key in (
        "label", "description", "transcript", "origin", "allowed_use", "character_name", "identity_label"
    ) if isinstance(value.get(key), str)}
    # Design-save sidecars record the generating engine, not a Cast clone identity.
    if isinstance(value.get("engine_id"), str):
        result["generation_engine_id"] = value["engine_id"][:120]
    return result


def _candidate_paths(root, budget):
    """Bound files *and* directories, without following links or globbing a tree."""
    import itertools

    visited = 0
    pending = [root]
    while pending and visited < budget:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(itertools.islice(iterator, budget - visited), key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        for entry in entries:
            visited += 1
            candidate = Path(entry.path)
            yield candidate
            try:
                if entry.is_dir(follow_symlinks=False) and _unlinked_below(root, candidate):
                    pending.append(candidate)
            except OSError:
                continue


def list_references(lab, cast, *, max_items=MAX_ITEMS, max_candidates=MAX_CANDIDATES):
    """List playable audio with honest provenance and stable Cast labels.

    Cast contributes only registered, processed reference versions. Its original
    uploads are kept intact but are not duplicated in the voice selector.
    """
    max_items = min(MAX_ITEMS, max(0, max_items))
    max_candidates = min(MAX_CANDIDATES, max(0, max_candidates))
    names = _engine_names(lab)
    metadata = _cast_metadata(cast, names)
    rows, seen = [], set()
    visited = 0
    for root, source, source_label in _roots(lab, cast):
        for candidate in _candidate_paths(root, max(0, max_candidates - visited)):
            visited += 1
            if len(rows) >= max_items:
                return rows
            if source == "cast_reference" and candidate.absolute() not in metadata:
                continue
            try:
                if not _unlinked_below(root, candidate):
                    continue
                resolved = candidate.resolve(strict=True)
                if root not in resolved.parents or resolved in seen:
                    continue
                size, details = _audio_details(resolved)
            except (OSError, RuntimeError, ValueError):
                continue
            saved = metadata.get(candidate.absolute()) or _sidecar(candidate, root)
            character = saved.get("character_name", "")
            identity = saved.get("identity_label", "")
            source_file = saved.get("source_file", resolved.name)
            if source == "cast_reference":
                display_name = f"{character} / {identity} / v{saved.get('version', '?')} / {source_file}"
            else:
                display_name = saved.get("label") or candidate.relative_to(root).as_posix()
            origin, allowed_use = saved.get("origin", ""), saved.get("allowed_use", "")
            row = {
                "name": resolved.name, "display_name": display_name,
                "path": str(resolved), "source": source, "source_label": source_label,
                "source_file": source_file, "size_bytes": size,
                "audio_url": "/api/reference-audio?path=" + quote(str(resolved), safe=""),
                "duration_seconds": round(float(details.duration), 3),
                "sample_rate_hz": details.samplerate,
                "transcript": saved.get("transcript", ""),
                "origin": origin, "allowed_use": allowed_use,
                "rights_status": "documented" if origin and allowed_use else "unknown",
                "character_name": character, "identity_label": identity,
                "reference_version_id": saved.get("reference_version_id"),
                "preferred_reference_id": saved.get("preferred_reference_id"),
                "is_preferred_reference": saved.get("is_preferred_reference", False),
                "character_id": saved.get("character_id"), "identity_id": saved.get("identity_id"),
                "character_tags": saved.get("character_tags", []),
                "identity_status": saved.get("identity_status"), "version": saved.get("version"),
                "engine_id": saved.get("engine_id"), "engine_name": saved.get("engine_name"),
                "generation_engine_id": saved.get("generation_engine_id"),
                "generation_engine_name": saved.get("generation_engine_name") or names.get(
                    saved.get("generation_engine_id"), saved.get("generation_engine_id")),
                "generation_source_take_id": saved.get("generation_source_take_id"),
            }
            for key in ("label", "description"):
                if saved.get(key):
                    row[key] = saved[key]
            rows.append(row)
            seen.add(resolved)
        if visited >= max_candidates:
            break
    return rows


def _playable_cast_media(cast, kind, item_id):
    try:
        path = Path(cast.get_media_path(kind, item_id))
        if not _unlinked_below(Path(cast.root), path):
            return None
        _, details = _audio_details(path)
        return details
    except (OSError, RuntimeError, KeyError, ValueError):
        return None


def _preferred_reference(identity):
    references = identity.get("references") or []
    approved = identity.get("approved_reference_id")
    match = next((reference for reference in references if reference["id"] == approved), None)
    return match or max(references, key=lambda reference: (
        reference.get("version", 0), reference.get("created_at", ""), reference["id"]), default=None)


def _choose_audition(takes, preferred_reference_id):
    if not takes:
        return None
    if preferred_reference_id:
        bound = [take for take in takes if take.get("reference_version_id") == preferred_reference_id]
        if bound:
            takes = bound
    non_rejected = [take for take in takes if take.get("status") != "rejected"]
    return max(non_rejected or takes, key=lambda take: (
        take.get("status") == "selected", take.get("created_at", ""), take["id"]))


def list_voice_library(lab, cast, *, max_items=MAX_ITEMS, include_retired=False):
    """Summarize saved identities, including fixed/preset voices without references.

    Technical-test labels come from saved tags. Audition audio is an existing
    readable take; this endpoint never synthesizes, approves, changes metadata, or
    invents a clone reference for a fixed voice.
    """
    max_items = min(MAX_ITEMS, max(0, max_items))
    names = _engine_names(lab)
    by_identity, _ = _take_index(cast)
    voices = []
    characters = [(_character_fields(character), character) for character in cast.list_cast()]
    characters.sort(key=lambda pair: (pair[0]["character_name"].casefold(), pair[0]["character_id"] or ""))
    for character_fields, character in characters:
        for identity in character.get("identities", []):
            if len(voices) >= max_items:
                break
            if identity.get("retired") and not include_retired:
                continue
            references = [reference for reference in identity.get("references") or []
                          if include_retired or not reference.get("retired")]
            preferred = _preferred_reference(identity)
            preferred_id = preferred["id"] if preferred else None
            available_references = {reference["id"] for reference in references
                                    if _playable_cast_media(cast, "reference", reference["id"]) is not None}
            take_details = {}
            playable_takes = []
            for take in by_identity.get(identity["id"], []):
                if take.get("retired") and not include_retired:
                    continue
                details = _playable_cast_media(cast, "take", take["id"])
                if details is not None:
                    take_details[take["id"]] = details
                    playable_takes.append(take)
            take = _choose_audition(playable_takes, preferred_id)
            generation_engine = _take_generation_engine(take) if take else None
            audition = None
            if take:
                details = take_details[take["id"]]
                model = take.get("model") or {}
                audition = {
                    "take_id": take["id"], "audio_url": take["audio_url"],
                    "duration_seconds": round(float(details.duration), 3), "sample_rate_hz": details.samplerate,
                    "text": take.get("spoken_text") or take.get("text", ""), "status": take.get("status"),
                    "language": take.get("language"), "settings": take.get("settings") or {},
                    "created_at": take.get("created_at"), "audio_sha256": take.get("audio_sha256"),
                    "generation_engine_id": generation_engine,
                    "generation_engine_name": names.get(generation_engine, generation_engine),
                    "checkpoint": model.get("checkpoint"), "checkpoint_revision": model.get("checkpoint_revision"),
                    "mode": model.get("mode"), "transcript_verified": model.get("transcript_verified"),
                    "human_listening": model.get("human_listening"), "license": model.get("license"),
                }
            provenance = preferred or ((take or {}).get("provenance") or {})
            origin, allowed_use = provenance.get("origin", ""), provenance.get("allowed_use", "")
            row = {
                **character_fields, "id": identity["id"], "identity_id": identity["id"],
                "identity_label": str(identity.get("label", "")), "identity_kind": identity.get("kind"),
                "identity_status": identity.get("status"), "engine_id": identity["engine_id"],
                "engine_name": names.get(identity["engine_id"], identity["engine_id"]),
                "generation_engine_id": generation_engine,
                "generation_engine_name": names.get(generation_engine, generation_engine),
                "engine_options": identity.get("engine_options") or {},
                "reference_count": len(references), "available_reference_count": len(available_references),
                "reference_version_ids": [reference["id"] for reference in references],
                "preferred_reference_id": preferred_id,
                "preferred_reference_available": preferred_id in available_references,
                "audition_count": len(playable_takes), "audition": audition,
                "origin": origin, "allowed_use": allowed_use,
                "rights_status": "documented" if origin and allowed_use else "unknown",
                "retired": bool(identity.get("retired")), "retirement": identity.get("retirement"),
            }
            voices.append(row)
        if len(voices) >= max_items:
            break
    voices.sort(key=lambda row: (row["character_name"].casefold(), row["identity_label"].casefold(), row["id"]))
    return {
        "schema_version": 1, "voices": voices,
        "counts": {"voices": len(voices), "references": sum(row["reference_count"] for row in voices),
                   "auditions": sum(row["audition_count"] for row in voices)},
    }


def resolve_reference_audio(value, lab, cast):
    """Resolve a preview path using the same roots and media policy as discovery."""
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("Choose an existing local reference audio file.")
    requested = Path(value)
    if not requested.is_absolute():
        raise ValueError("Reference audio must use an absolute local path.")
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Reference audio was not found.") from exc
    metadata = None
    for root, source, _ in _roots(lab, cast):
        if root not in resolved.parents or not _unlinked_below(root, requested):
            continue
        if source == "cast_reference":
            if metadata is None:
                metadata = _cast_metadata(cast)
            if resolved not in metadata:
                continue
        _audio_details(resolved)
        return resolved, AUDIO_TYPES[resolved.suffix.lower()]
    raise ValueError("Reference audio must belong to this Lab's reference folder, saved voices, or processed Cast references.")
