"""Versioned JSON-lines messages shared by the broker, workers, and contract tests."""

import json

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
METHODS = frozenset({"health", "load", "prepare_reference", "phonemize", "generate", "generate_stream", "unload", "shutdown"})


class ProtocolError(RuntimeError):
    pass


def encode_message(message):
    line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"Worker protocol message exceeds {MAX_MESSAGE_BYTES} bytes.")
    return line


def decode_message(line):
    if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"Worker protocol message exceeds {MAX_MESSAGE_BYTES} bytes.")
    try:
        message = json.loads(line)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"Malformed JSON-lines message: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("Worker protocol messages must be JSON objects.")
    return message


def request(request_id, method, **params):
    if method not in METHODS:
        raise ProtocolError(f"Unknown worker method {method!r}.")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "id": str(request_id),
        "method": method,
        "params": params,
    }


def response(request_id, result=None, error=None):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "id": str(request_id),
        "ok": error is None,
        "result": result if error is None else None,
        "error": error,
    }


def validate_response(message, request_id):
    if message.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(f"Worker protocol version mismatch: {message.get('protocol_version')!r}.")
    if str(message.get("id")) != str(request_id):
        raise ProtocolError(f"Worker response id mismatch: expected {request_id!r}, got {message.get('id')!r}.")
    if not isinstance(message.get("ok"), bool):
        raise ProtocolError("Worker response is missing a boolean 'ok' field.")
    return message
