"""Uninstall one model from the Models & features page: delete its files from the Lab's folder, and leave everything another
installed model still uses.

A model's files are its checkpoint in hf-cache (with the repository of its preset voices, if it has one), the files it keeps
elsewhere (Breeze's in breeze/models, Drama 3's in out/drama3), the source folders its worker imports (VoxCPM2's in
out/voxcpm2) and its Python environment (.venv-kokoro). Another installed model may share a checkpoint (Chatterbox 500M and
Voice Conversion), an environment (the Chatterbox and Qwen3-TTS families) or borrow an environment's packages (VoxCPM2 from
.venv-fqwen); what it uses stays. The service's own .venv, which Drama 3 runs in, is never removed. The library is never
touched.

A folder that is a link or a junction to another place is removed as a link: what it points at stays where it is."""

import os
import stat
from pathlib import Path

import engine_adapters
import engine_checks

# Environments whose packages another model may borrow (engine_checks.BORROWED_PACKAGES), by the model that borrows them.
BORROWED = engine_adapters.table("borrowed_packages")


def environment_folder(python):
    """The folder of a model's own Python environment, from its interpreter's path (.venv-kokoro/Scripts/python.exe), or
    None for the service's own .venv."""
    folder = Path(python).parts[0]
    return folder if folder.startswith(".venv-") else None


def model_folders(entry, root):
    """The folders, relative to the lab folder, that hold one model's own files (not its environment)."""
    root = Path(root)
    engine = entry["id"]
    adapter = engine_adapters.get(engine)
    own = adapter.checkpoint_missing(root) if adapter is not None else None
    folders = []
    if own is not None:
        folders.append(Path(*Path(own[0]).parts[:2]).as_posix())
    else:
        cache = "hf-cache" if engine in engine_checks.TOP_LEVEL_CACHE else "hf-cache/hub"
        folders.append(f"{cache}/models--{entry['checkpoint'].replace('/', '--')}")
        voices = entry.get("backend", {}).get("voices_checkpoint")
        if voices:
            folders.append(f"{cache}/models--{voices.replace('/', '--')}")
    for directory in engine_checks.EXTRA_DIRECTORIES.get(engine, ()):
        parts = Path(directory).parts
        folders.append(Path(*parts[:2]).as_posix() if parts[0] == "out" else Path(directory).as_posix())
    return folders


def environments_used(entry):
    """The environments a model runs in or borrows packages from."""
    used = {environment_folder(entry["runtime"]["python"])}
    if entry["id"] in BORROWED:
        used.add(environment_folder(BORROWED[entry["id"]]))
    return used - {None}


def is_link(path):
    isjunction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(isjunction and isjunction(path))


def size_of(path):
    """Bytes in a file or folder, not counting what a link inside it points at."""
    if is_link(path):
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for folder, folders, files in os.walk(path):
        here = Path(folder)
        folders[:] = [name for name in folders if not is_link(here / name)]
        for name in files:
            try:
                if not is_link(here / name):
                    total += (here / name).stat().st_size
            except OSError:
                pass
    return total


def remove(path):
    """Delete a file or a folder with what is in it. A link or junction is removed itself and never followed; a read-only
    file is made writable first."""
    if is_link(path):
        try:
            os.rmdir(path)
        except OSError:
            os.unlink(path)
        return
    if path.is_dir():
        for child in path.iterdir():
            remove(child)
        os.rmdir(path)
        return
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def inside(root, relative):
    """The path of a folder the plan names when it lies in the lab folder itself, or None when the way to it runs through
    a link to another place (hf-cache as a junction to another folder, say): that is not the Lab's to delete."""
    root = Path(root).resolve()
    path = root / relative
    parent = path.parent.resolve()
    return path if parent == root or root in parent.parents else None


def plan(entries, key, root, probe):
    """What uninstalling a model deletes and what stays, for the confirmation: a dict with name, remove (path, bytes, link),
    keep (path, why), bytes, ok, paragraphs and problems."""
    if key not in entries:
        raise ValueError(f"Unknown engine {key!r}.")
    root = Path(root)
    entry = entries[key]
    name = entry.get("name", key)
    others = [other for other_key, other in entries.items() if other_key != key and probe(other)["installed"]]
    candidates = [(folder, [other for other in others if folder in model_folders(other, root)])
                  for folder in model_folders(entry, root)]  # fmt: skip
    environment = environment_folder(entry["runtime"]["python"])
    if environment is not None:
        candidates.append((environment, [other for other in others if environment in environments_used(other)]))
    remove_list, keep = [], []
    for folder, users in candidates:
        path = inside(root, folder)
        if path is None:
            keep.append({"path": folder, "why": "it is reached through a link to a folder outside the Lab's folder"})
        elif not path.exists() and not is_link(path):
            continue
        elif users:
            keep.append({"path": folder, "why": ", ".join(other.get("name", other["id"]) for other in users) + " uses it too"})
        else:
            remove_list.append(folder)
    removing = []
    for folder in remove_list:
        path = root / folder
        link = is_link(path)
        removing.append({"path": folder, "bytes": 0 if link else size_of(path), "link": link})
    total = sum(item["bytes"] for item in removing)
    paragraphs, problems = [], []
    if removing:
        listed = ", ".join(item["path"] for item in removing)
        if all(item["link"] for item in removing):
            paragraphs.append(f"Uninstalling {name} removes {listed} from the Lab's folder.")
        else:
            paragraphs.append(f"Uninstalling {name} deletes {format_bytes(total)} from the Lab's folder: {listed}.")
        if any(item["link"] for item in removing):
            paragraphs.append("A folder that is a link to another place is removed as a link; what it points at stays.")
    else:
        problems.append(
            f"{name} has no files of its own to delete"
            + (": everything it has, another installed model uses too." if keep else ".")
        )
    for item in keep:
        paragraphs.append(f"{item['path']} stays, because {item['why']}.")
    if removing:
        paragraphs.append(
            "Your library is not touched: characters, voices, takes and scenes stay. A voice on this model cannot speak "
            "until the model is installed again; Install on its card puts it back, downloading its files again."
        )
    return {
        "engine": key,
        "name": name,
        "remove": removing,
        "keep": keep,
        "bytes": total,
        "ok": bool(removing),
        "paragraphs": paragraphs,
        "problems": problems,
    }


def run(entries, key, root, probe):
    """Delete what plan() names. Returns the plan with removed (paths deleted), failed (path and reason) and freed (bytes)."""
    result = plan(entries, key, root, probe)
    if not result["ok"]:
        raise ValueError(result["problems"][0])
    removed, failed, freed = [], [], 0
    for item in result["remove"]:
        try:
            remove(inside(root, item["path"]))
        except OSError as exc:
            failed.append({"path": item["path"], "reason": exc.strerror or str(exc)})
        else:
            removed.append(item["path"])
            freed += item["bytes"]
    return {**result, "removed": removed, "failed": failed, "freed": freed, "freed_text": format_bytes(freed)}


def format_bytes(count):
    for unit, size in (("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if count >= size:
            return f"{count / size:.1f} {unit}"
    return f"{count} bytes"
