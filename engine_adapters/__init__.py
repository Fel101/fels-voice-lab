"""One module per engine family: what the Lab knows about each model, in one place.

The shared code (the pool, the service, the checks and the tables they read) asks an engine's adapter instead of testing the
engine's name. Each family module holds the adapters for its engines, and has two halves:

- the service's half: the adapter objects, whose attributes and methods the service reads. It imports in the core
  environment (.venv) with no model packages, as the service must never import a model.
- the worker's half: the adapter's worker_* methods, which load and speak inside the model's own environment
  (worker_runtime.py calls them in engine_worker.py's process), with their heavy imports inside them.

Adding a model is a catalog entry, a family module listed in FAMILIES (or an adapter added to one), and an installer.
"""

import importlib

# The family modules, smallest first. Each has ADAPTERS: {engine id: adapter}.
FAMILIES = ("engine_adapters.phonon", "engine_adapters.kokoro", "engine_adapters.chatterbox")
# The engine a speak or load request that names none gets.
DEFAULT_ENGINE = "kokoro"


def _load():
    found = {}
    for name in FAMILIES:
        module = importlib.import_module(name)
        for engine_id, adapter in module.ADAPTERS.items():
            if engine_id in found:
                raise RuntimeError(f"Two families claim the engine {engine_id!r}.")
            found[engine_id] = adapter
    return found


ADAPTERS = _load()


def get(engine_id):
    """The engine's adapter, or None for an engine no family module has taken yet."""
    return ADAPTERS.get(engine_id)


def table(name):
    """{engine id: the adapter's value} for every adapter that sets `name` to something other than None."""
    return {
        engine_id: getattr(adapter, name) for engine_id, adapter in ADAPTERS.items() if getattr(adapter, name, None) is not None
    }


def engines_where(name):
    """The engines whose adapter's `name` is true."""
    return {engine_id for engine_id, adapter in ADAPTERS.items() if getattr(adapter, name, False)}


def only(name):
    """The one engine whose adapter's `name` is true: the converter, or the engine that shows its phonemes."""
    (engine_id,) = engines_where(name)
    return engine_id


def info():
    """The fields the adapters add to GET /api/info."""
    fields = {}
    for adapter in ADAPTERS.values():
        fields.update(adapter.info())
    return fields
