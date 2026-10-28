"""Standalone (no ComfyUI) wrapper around the vendored Breeze TTS 2 code in breeze/.

Three ways to pick the voice, matching the official infer.py:
- design_speech: a text description of the voice, no recording ("voice design").
- clone_speech without an instruction: copy the voice (and its mood) of a reference clip ("voice cloning").
- clone_speech with an instruction: that voice, steered by a direction such as "whispering" ("voice direction").

Runs in its own venv (.venv-breeze), separate from the main service environment,
so nothing here can break what already works.
"""

from __future__ import annotations

import os
import time

import soundfile as sf
import torch

import common  # noqa: F401  (sets HIP_VISIBLE_DEVICES, disables cudnn, points HF_HOME at hf-cache)

from breeze import loader, native, runtime

MODEL_CHOICE = loader.BF16_LABEL  # full precision weights; skips the int8 dequant path entirely
DEVICE = "cuda"
ATTENTION = "sdpa"  # no flash-attn / sageattention on this GPU; sdpa is plain torch, no extra deps
# "eager" skips the CUDA-graph capture path, the least portable part of this code. "cuda_graphs" records the
# depth decoder's 15 small steps per audio frame once and replays them (HIP graphs on ROCm). Set BREEZE_DECODE.
DECODE_MODE = os.environ.get("BREEZE_DECODE", "eager")

_bundle = None
_last_generation_info = {}


def load_model():
    global _bundle
    if _bundle is not None:
        return _bundle
    t0 = time.perf_counter()
    _bundle = loader.load_breeze_bundle(
        repo_choice=MODEL_CHOICE,
        dtype_name="auto",
        device_name=DEVICE,
        attention_choice=ATTENTION,
        # Inference never installs or downloads weights. Model installation is a
        # separate, explicit operation; the service must fail clearly if the
        # already-installed local checkpoint is missing.
        download_if_missing=False,
        decode_mode=DECODE_MODE,
    )
    print(f"breeze: loaded {MODEL_CHOICE!r} in {time.perf_counter() - t0:.1f}s "
          f"(device={_bundle.device}, dtype={_bundle.dtype_name}, attention={_bundle.attention}, "
          f"decode={_bundle.decode_mode})", flush=True)
    return _bundle


def _generate(cond, negative, ref_codes, *, cfg_scale, seed, max_new_tokens=1500, temperature=0.9, top_k=50,
              top_p=1.0, repetition_penalty=1.1, depth_temperature=0.9, depth_top_k=50, depth_top_p=1.0):
    global _last_generation_info
    bundle = load_model()
    runtime.fix_seed(int(seed))
    inputs_embeds, attention_mask, base_positions, prefill_len = runtime.build_generation_batch(
        bundle.model,
        bundle.tokenizer,
        cond_segments=cond,
        negative_segments=negative if cfg_scale != 1.0 else None,
        ref_codes=ref_codes,
        cfg_scale=float(cfg_scale),
        device=bundle.device,
    )
    max_frames = min(int(max_new_tokens), runtime.MAX_SEQ_LEN - 1 - prefill_len)
    if max_frames < 64:
        raise ValueError(
            f"The prompt alone is {prefill_len} tokens of the {runtime.MAX_SEQ_LEN}-token context, "
            f"leaving only {max_frames} audio frames. Use shorter text."
        )
    params = runtime.GenerationParams(
        max_new_tokens=max_frames,
        temperature=float(temperature),
        top_k=int(top_k),
        top_p=float(top_p),
        repetition_penalty=float(repetition_penalty),
        depth_temperature=float(depth_temperature),
        depth_top_k=int(depth_top_k),
        depth_top_p=float(depth_top_p),
    )

    with torch.inference_mode(), native.attention_runtime(bundle.attention):
        codes = runtime.generate_codes(
            bundle.model,
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            base_positions=base_positions,
            prefill_len=prefill_len,
            cfg_scale=float(cfg_scale),
            params=params,
            progress_callback=None,
            decode_mode=bundle.decode_mode,
        )
        wav = runtime.decode_codes(bundle.codec, codes)

    if not bool(torch.isfinite(wav).all()):
        raise RuntimeError("Breeze TTS 2 generated non-finite audio samples.")
    if wav.numel() == 0:
        raise RuntimeError("Breeze TTS 2 produced no audio.")
    depth_runners = getattr(bundle.model, "_breeze_depth_runners", {})
    graph_used = any(getattr(runner, "use_graph", False) and
                     getattr(runner, "_graph_steps", None) is not None
                     for runner in depth_runners.values())
    _last_generation_info = {
        "profile_requested": bundle.decode_mode,
        "profile_effective": "cuda_graphs" if graph_used else "eager",
        "prefill_tokens": int(prefill_len),
        "max_new_tokens_requested": int(max_new_tokens),
        "max_new_tokens_effective": int(max_frames),
        "audio_frames": int(codes.shape[-2]),
        "fallback_to_eager": bool(bundle.decode_mode == "cuda_graphs" and not graph_used),
    }
    return wav, runtime.SAMPLE_RATE


def generation_info():
    """Metadata for the most recent serialized generation in this worker."""
    return dict(_last_generation_info)


def design_speech(text: str, instruction: str, *, cfg_scale: float = 4.0, seed: int = 42, **sampling):
    """Text -> speech in a voice described by `instruction`, no reference audio. Returns (wav, sample_rate)."""
    if not text or not text.strip():
        raise ValueError("Text cannot be empty.")
    cond = runtime.design_segments(text, instruction)
    negative = runtime.design_negative_segments(text)
    return _generate(cond, negative, None, cfg_scale=cfg_scale, seed=seed, **sampling)


def encode_reference(path: str):
    """Reference clip -> codec codes, done once per voice and reused for every line."""
    bundle = load_model()
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    wav = torch.from_numpy(data.mean(axis=1))
    if wav.shape[0] / sr > runtime.MAX_REFERENCE_SECONDS:
        raise ValueError(f"Reference clip is longer than {runtime.MAX_REFERENCE_SECONDS:.0f} s.")
    with torch.inference_mode():
        return runtime.encode_reference_audio(bundle.codec, wav, sr)


def clone_speech(text: str, ref_codes, ref_text: str, instruction: str | None = None, *,
                 cfg_scale: float | None = None, seed: int = 42, **sampling):
    """Speak `text` in the reference clip's voice. With `instruction`, steer it (voice direction):
    the unguided clone is the CFG negative, so cfg pushes the voice towards the instruction."""
    if not text or not text.strip():
        raise ValueError("Text cannot be empty.")
    if instruction:
        cond = runtime.ref_segments(ref_text, text, instruction, with_instruction=True)
        negative = runtime.ref_segments(ref_text, text, "", with_instruction=False)
        cfg = 4.0 if cfg_scale is None else cfg_scale
    else:
        cond = runtime.ref_segments(ref_text, text, "", with_instruction=False)
        negative = None
        cfg = 1.0 if cfg_scale is None else cfg_scale
    return _generate(cond, negative, ref_codes, cfg_scale=cfg, seed=seed, **sampling)
