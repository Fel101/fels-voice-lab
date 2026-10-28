"""Small protocol worker used to verify streaming order and cancellation without a model."""

import argparse
import base64
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from worker_protocol import PROTOCOL_VERSION, decode_message, encode_message, response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="qwen3-tts-1.7b-voice-design")
    parser.add_argument("--delay", type=float, default=0.04)
    args = parser.parse_args()
    output = sys.stdout
    sys.stdout = sys.stderr
    for raw in sys.stdin:
        request = decode_message(raw.rstrip("\r\n"))
        request_id = request["id"]
        method = request["method"]
        params = request.get("params") or {}
        stop = False
        if method == "health":
            result = {"engine_id": args.engine, "pid": os.getpid(), "parent_pid": os.getppid(),
                      "model_loaded": False, "device": "cpu", "free_ram_gb": 99, "memory": {}}
        elif method == "load":
            result = {"engine_id": args.engine, "pid": os.getpid(), "parent_pid": os.getppid(),
                      "model_loaded": True, "sample_rate": 24000, "load_seconds": 0.01,
                      "backend": "stream-test-worker", "checkpoint": "fixture"}
        elif method == "generate_stream":
            sequence = 0
            sample_start = 0
            for base in (100, 200, 300):
                time.sleep(args.delay)
                pcm = struct.pack("<5h", base, base + 1, base + 2, base + 3, base + 4)
                message = {
                    "protocol_version": PROTOCOL_VERSION, "id": request_id, "event": "audio_chunk",
                    "sequence": sequence, "sample_start": sample_start, "sample_count": 5,
                    "sample_rate": 24000, "sample_format": "pcm_s16le",
                    "data": base64.b64encode(pcm).decode("ascii"),
                    **({"generator_first_chunk_s": 0.04, "backend_chunk_index": 0} if sequence == 0 else {}),
                }
                output.write(encode_message(message))
                output.flush()
                sequence += 1
                sample_start += 5
            time.sleep(args.delay)
            result = {"sample_count": sample_start, "chunk_count": sequence,
                      "sample_rate": 24000, "total_s": args.delay * 4,
                      "generator_first_chunk_s": 0.04, "backend_chunk_count": 3,
                      "engine": args.engine, "mode": params.get("mode", "design"),
                      "streaming_kind": "native_generator", "sampling": {"seed": params.get("seed", 42)}}
        elif method in {"unload", "shutdown"}:
            result = {"model_loaded": False, "allocated_bytes_released": 1}
            stop = method == "shutdown"
        else:
            result = {"error": "unsupported"}
            output.write(encode_message(response(request_id, error={"type": "ValueError", "message": "unsupported"})))
            output.flush()
            continue
        output.write(encode_message(response(request_id, result=result)))
        output.flush()
        if stop:
            break


if __name__ == "__main__":
    main()
