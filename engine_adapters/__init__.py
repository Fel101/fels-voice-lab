"""One module per engine family: what the Lab knows about each model, in one place.

The shared code (the pool, the service, the checks and the tables they read) asks an engine's adapter instead of testing the
engine's name. Each family module holds the adapters for its engines, and has two halves:

- the service's half: the adapter objects, whose attributes and methods the service reads. It imports in the core
  environment (.venv) with no model packages, as the service must never import a model.
- the worker's half: a Worker class that loads and speaks inside the model's own environment (engine_worker.py), with its
  heavy imports inside its methods.

Adding a model is a catalog entry, a family module listed in FAMILIES (or an adapter added to one), and an installer.
"""

import importlib

# The family modules, smallest first. Each has ADAPTERS: {engine id: adapter}.
FAMILIES = ("engine_adapters.phonon",)


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


def info():
    """The fields the adapters add to GET /api/info."""
    fields = {}
    for adapter in ADAPTERS.values():
        fields.update(adapter.info())
    return fields
