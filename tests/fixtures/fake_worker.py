"""Small JSON-lines worker used to test broker scheduling and process ownership without a GPU."""

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from worker_protocol import PROTOCOL_VERSION, decode_message, encode_message, response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="kokoro")
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--started-marker", default="")
    args = parser.parse_args()
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    loaded = False
    prepared = None
    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        request_id = None
        try:
            message = decode_message(raw.rstrip("\r\n"))
            request_id = message.get("id")
            if message.get("protocol_version") != PROTOCOL_VERSION:
                raise ValueError("version mismatch")
            method = message.get("method")
            params = message.get("params") or {}
            if method == "health":
                result, stop = {"engine_id": args.engine, "pid": os.getpid(), "parent_pid": os.getppid(), "model_loaded": loaded,
                                "memory": {"allocated_bytes": 0}}, False
            elif method == "load":
                loaded = True
                result, stop = {"engine_id": args.engine, "pid": os.getpid(), "parent_pid": os.getppid(), "model_loaded": True,
                                "sample_rate": 24000, "memory": {"allocated_bytes": 0}}, False
            elif method == "prepare_reference":
                prepared = params.get("path")
                result, stop = {"prepared": True, "path": prepared}, False
            elif method == "generate":
                if not loaded:
                    raise RuntimeError("not loaded")
                if params.get("text") == "__crash__":
                    os._exit(23)
                if args.started_marker:
                    Path(args.started_marker).write_text(str(os.getpid()), encoding="ascii")
                if args.delay:
                    time.sleep(args.delay)
                path = Path(params["output_file"])
                path.parent.mkdir(parents=True, exist_ok=True)
                np.save(path, np.linspace(-0.25, 0.25, 4800, dtype=np.float32), allow_pickle=False)
                result, stop = {"audio_file": str(path), "sample_rate": 24000, "sample_count": 4800,
                                "info": {"total_s": 0.1, "first_chunk_s": 0.01},
                                "memory": {"allocated_bytes": 0}}, False
            elif method == "unload":
                loaded = False
                result, stop = {"model_loaded": False, "memory_before": {"allocated_bytes": 1234},
                                "memory_after": {"allocated_bytes": 0}, "allocated_bytes_released": 1234}, False
            elif method == "shutdown":
                loaded = False
                result, stop = {"stopped": True, "model_loaded": False}, True
            else:
                raise ValueError(f"unknown method {method!r}")
            protocol_stdout.write(encode_message(response(request_id, result=result)))
            protocol_stdout.flush()
            if stop:
                break
        except Exception as exc:
            protocol_stdout.write(encode_message(response(request_id, error={"type": type(exc).__name__, "message": str(exc)})))
            protocol_stdout.flush()


if __name__ == "__main__":
    main()
