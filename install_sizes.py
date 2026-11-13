"""How much a model's Install downloads and how much disk it then takes, for the Models page and the first-run note.

The numbers are the ones each installer plans with (its packages' measured sizes and the files its manifest lists), for a first
install with nothing in place: the plan an Install shows first gives the exact figure for this computer, less what is already
here. Each installer is imported only when its model is sized, and the answer is kept, so the page can ask on every refresh.
"""

from __future__ import annotations

import threading
from pathlib import Path

_lock = threading.Lock()
_known = {}


def _files_bytes(manifest, files=None):
    return sum(item["size"] for item in (manifest["files"] if files is None else files))


def _breeze(root, _nvidia, card):
    from tools import install_breeze as ib

    files = _files_bytes(ib.base.load_manifest(root / ib.MANIFEST))
    download, disk = ib.packages_mb(card)
    return download * 10**6 + files, disk * 10**6 + files


def _drama3(_root, _nvidia, _card):
    from tools import install_drama3 as idr

    assets = sum(item["size"] for item in idr.ASSETS)
    # The runtime's archive is kept beside what it unpacks to, which the installer allows four times its size for.
    return idr.RUNTIME_SIZE + idr.MODEL_SIZE + assets, idr.RUNTIME_SIZE * 4 + idr.MODEL_SIZE + assets


def _sizers():
    import engine_adapters

    def adapted(adapter):
        return lambda root, nvidia, card: adapter.install_size(root, nvidia, card, _files_bytes)

    return {
        "breeze-tts-2": _breeze,
        "drama-3": _drama3,
        **{engine: adapted(adapter) for engine, adapter in engine_adapters.ADAPTERS.items() if adapter.sizes_install},
    }


def model_bytes(engine, root, *, nvidia=False, card=None):
    """(bytes to download, bytes on disk) for a first install of `engine`: its CPU build, NVIDIA's build (nvidia), or AMD's
    build for `card` (an amd_build card). None for an engine the app has no installer for (engine_install.INSTALLERS), or
    whose numbers cannot be read."""
    key = (engine, str(root), bool(nvidia), (card or {}).get("chip"))
    with _lock:
        if key in _known:
            return _known[key]
    sizer = _sizers().get(engine)
    try:
        answer = sizer(Path(root), bool(nvidia), card) if sizer else None
    except Exception:  # noqa: BLE001 - a missing or broken manifest leaves the size out; Install's plan says what is wrong
        answer = None
    with _lock:
        _known[key] = answer
    return answer


def size_fields(answer):
    """The engine status row's install_size: {download_bytes, disk_bytes}, or None."""
    return None if answer is None else {"download_bytes": int(answer[0]), "disk_bytes": int(answer[1])}
