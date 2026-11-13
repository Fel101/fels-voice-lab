"""Settings accepted by reusable Cast performances, with their service defaults."""

import math
from chatterbox_support import normalize_chatterbox_sampling
import engine_adapters

DEFAULTS = {
    'drama-3': {'seed':42,'temperature':1.,'top_k':30,'top_p':.9,'max_seconds':47.6},
    "kokoro": {"speed": 1.0},
    "turbo": normalize_chatterbox_sampling("turbo", {}),
    "orig": normalize_chatterbox_sampling("orig", {"exaggeration": 0.6, "cfg_weight": 0.4}),
    "breeze-tts-2": {"profile": "eager", "seed": 42, "cfg_scale": 1.0,
        "max_new_tokens": 1500, "temperature": 0.9, "top_k": 50, "top_p": 1.0,
        "repetition_penalty": 1.1, "depth_temperature": 0.9, "depth_top_k": 50, "depth_top_p": 1.0},
    "voxcpm2": {"seed": 42, "cfg_value": 2.0, "inference_timesteps": 10},
}
for _engine in ("qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base"):
    DEFAULTS[_engine] = {"seed": 42, "max_new_tokens": 512, "temperature": 0.9,
                        "top_k": 50, "top_p": 1.0, "repetition_penalty": 1.05}
DEFAULTS["qwen3-tts-1.7b-custom-voice"] = {
    "seed": 42, "max_new_tokens": 512, "temperature": 0.9,
    "top_k": 50, "top_p": 1.0, "repetition_penalty": 1.05,
}

# Presentation metadata for the values already accepted by effective_settings(). The
# catalog remains authoritative for capability status; this table supplies readable
# labels and input bounds to the Perform and scene forms.
CONTROL_UI = {
    'drama-3': {'seed':('Seed',0,2147483647,1),'temperature':('Temperature',.05,2.,.05),
                'top_k':('Top-k',1,4097,1),'top_p':('Top-p',.05,1.,.01),
                'max_seconds':('Maximum seconds per chunk',.5,95.1,.1)},
    "kokoro": {"speed": ("Speed", 0.5, 2.0, 0.05)},
    "turbo": {"temperature": ("Temperature", 0.05, 2.0, 0.05),
              "top_k": ("Top-k", 1, 2048, 1), "top_p": ("Top-p", 0.05, 1.0, 0.01),
              "repetition_penalty": ("Repetition penalty", 0.5, 2.0, 0.05)},
    "orig": {"exaggeration": ("Exaggeration", 0.0, 2.0, 0.05),
             "cfg_weight": ("CFG weight", 0.0, 1.0, 0.05),
             "temperature": ("Temperature", 0.05, 2.0, 0.05), "top_p": ("Top-p", 0.05, 1.0, 0.01),
             "repetition_penalty": ("Repetition penalty", 0.5, 2.0, 0.05),
             "min_p": ("Min-p", 0.0, 1.0, 0.01)},
    "breeze-tts-2": {"profile": ("Runtime profile", "eager", "cuda_graphs"),
                     "seed": ("Seed", 0, 2147483647, 1), "cfg_scale": ("CFG scale", 0.0, 20.0, 0.1),
                     "max_new_tokens": ("Maximum new tokens", 8, 2048, 1),
                     "temperature": ("Temperature", 0.05, 2.0, 0.05), "top_k": ("Top-k", 0, 200, 1),
                     "top_p": ("Top-p", 0.05, 1.0, 0.01),
                     "repetition_penalty": ("Repetition penalty", 0.5, 2.0, 0.05),
                     "depth_temperature": ("Depth temperature", 0.05, 2.0, 0.05),
                     "depth_top_k": ("Depth top-k", 0, 200, 1), "depth_top_p": ("Depth top-p", 0.05, 1.0, 0.01)},
    "voxcpm2": {"seed": ("Seed", 0, 2147483647, 1), "cfg_value": ("CFG value", 0.1, 10.0, 0.1),
                "inference_timesteps": ("Inference steps", 1, 100, 1)},
}
for _engine in ("qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base", "qwen3-tts-1.7b-custom-voice"):
    CONTROL_UI[_engine] = {
        "seed": ("Seed", 0, 2147483647, 1), "max_new_tokens": ("Maximum new tokens", 8, 2048, 1),
        "temperature": ("Temperature", 0.05, 2.0, 0.05), "top_k": ("Top-k", 0, 200, 1),
        "top_p": ("Top-p", 0.05, 1.0, 0.01),
        "repetition_penalty": ("Repetition penalty", 0.5, 2.0, 0.05),
    }
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
    if engine == 'drama-3':
        from drama3_runtime import controls
        return controls(settings)
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
    if engine in {"orig", "turbo"}:
        return normalize_chatterbox_sampling(engine, result)
    return result
