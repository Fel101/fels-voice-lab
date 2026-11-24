# SPDX-License-Identifier: Apache-2.0
# Adapted RoPE compatibility: The Qwen team, Alibaba Group and The HuggingFace Inc. team.
# Modified to read rope_parameters; see THIRD_PARTY_NOTICES.md.

"""Import before loading a model with faster-qwen3-tts (.venv-fqwen).

qwen-tts-hf registers a shim for the plain RoPE that transformers 5.x dropped, but it reads config.rope_theta,
which the codec's MimiConfig no longer has on transformers 5.15+ (it is in config.rope_parameters): loading
failed with AttributeError. This registers the same function reading either place. Library files untouched.
"""

import torch
import qwen_tts  # noqa: F401  (registers qwen-tts-hf's shim first, so ours replaces it)
from transformers.modeling_rope_utils import ROPE_INIT_FUNCTIONS


def _default_rope_parameters(config, device=None, seq_len=None, layer_type=None):
    base = getattr(config, "rope_theta", None)
    if base is None:
        base = (getattr(config, "rope_parameters", None) or {}).get("rope_theta", 10000.0)
    partial = getattr(config, "partial_rotary_factor", 1.0)
    head_dim = getattr(config, "head_dim", None) or config.hidden_size // config.num_attention_heads
    dim = int(head_dim * partial)
    inv_freq = 1.0 / (base ** (torch.arange(0, dim, 2, dtype=torch.int64).to(device=device, dtype=torch.float) / dim))
    return inv_freq, 1.0


ROPE_INIT_FUNCTIONS["default"] = _default_rope_parameters
