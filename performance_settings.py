"""Settings accepted by reusable Cast performances, with their service defaults."""

import math
import engine_adapters

DEFAULTS = {}

# Presentation metadata for the values already accepted by effective_settings(). The
# catalog remains authoritative for capability status; this table supplies readable
# labels and input bounds to the Perform and scene forms.
CONTROL_UI = {}
# The engines whose adapters say (engine_adapters/).
for _engine, _adapter in engine_adapters.ADAPTERS.items():
    if _adapter.performance_defaults is not None:
        DEFAULTS[_engine] = dict(_adapter.performance_defaults)
        CONTROL_UI[_engine] = dict(_adapter.control_ui or {})


def performance_controls(engine, capabilities=None):
    """Describe only accepted controls, enriched with their catalog verification status."""
    defaults = DEFAULTS.get(engine)
    if defaults is None:
        return {"engine_id": engine, "controls": [], "direction": {"status": "unsupported"}}
    capabilities = capabilities or {}
    sampling = capabilities.get("sampling_controls", {})
    sampling_ids = set(sampling.get("controls", [])) if isinstance(sampling, dict) else set()
    acting = capabilities.get("acting_controls", [])
    if isinstance(acting, dict):
        acting = acting.get("controls", [])
    acting_status = {item.get("id"): item.get("status", "supported") for item in acting if isinstance(item, dict)}
    runtime_profiles = capabilities.get("runtime_profiles", {})
    profiles_status = runtime_profiles.get("status", "unsupported") if isinstance(runtime_profiles, dict) else "unsupported"
    controls = []
    for key, default in defaults.items():
        metadata = CONTROL_UI.get(engine, {}).get(key)
        if metadata is None:
            continue
        if key == "profile":
            label, *choices = metadata
            status = profiles_status
            control = {"id": key, "label": label, "type": "select", "choices": choices}
        else:
            label, minimum, maximum, step = metadata
            status = acting_status.get(key) or (sampling.get("status", "unsupported") if key in sampling_ids else "unsupported")
            control = {"id": key, "label": label, "type": "number", "minimum": minimum,
                       "maximum": maximum, "step": step}
        if status not in {"supported", "worker_verified", "experimental"}:
            continue
        control.update({"default": default, "status": status})
        controls.append(control)
    direction = capabilities.get("voice_direction", {"status": "unsupported"})
    return {"engine_id": engine, "controls": controls,
            "direction": direction if isinstance(direction, dict) else {"status": "unsupported"},
            "native_vocal_events": capabilities.get("native_vocal_events", {"status": "unsupported"})}


def effective_settings(engine, settings):
    adapter = engine_adapters.get(engine)
    own = adapter.own_settings(settings) if adapter is not None else None
    if own is not None:  # a model that checks its settings itself (Drama 3)
        return own
    if not isinstance(settings, dict):
        raise ValueError("Performance settings must be an object.")
    defaults = DEFAULTS.get(engine)
    if defaults is None:
        raise ValueError(f"{engine} has no supported Cast performance settings.")
    unsupported = set(settings) - set(defaults)
    if unsupported:
        raise ValueError(f"{engine} does not support performance setting(s): {', '.join(sorted(unsupported))}.")
    result = {**defaults, **settings}
    for key, value in result.items():
        if key == "profile":
            if value not in {"eager", "cuda_graphs"}:
                raise ValueError("Breeze profile must be eager or cuda_graphs.")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Performance {key} must be a finite number.")
    return adapter.settle_settings(result) if adapter is not None else result
