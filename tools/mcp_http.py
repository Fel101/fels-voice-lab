"""Authenticated loopback Streamable HTTP for handshake-era MCP clients."""

import hmac
import io
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.mcp_server import LEGACY_VERSIONS, Player, Server, Tools

MAX_BODY = 65536
MAX_SESSIONS = 16
IDLE = 900


@dataclass
class Connection:
    tools: Tools
    version: str
    touched: float
    pending: set = field(default_factory=set)


class Host(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, lab, workspace, clips, token):
        self.lab, self.workspace, self.clips = lab, workspace, clips
        self.token = token.encode("utf-8")
        self.lock = threading.RLock()
        self.sessions = {}
        super().__init__(("127.0.0.1", port), Handler)

    def prune(self):
        now = time.monotonic()
        for key, value in list(self.sessions.items()):
            if not value.pending and now - value.touched > IDLE:
                del self.sessions[key]

    def end(self, key):
        with self.lock:
            row = self.sessions.pop(key, None)
        if row:
            row.tools.stop_all()
        return row is not None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def reply(self, code, data=None, headers=None):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else b""
        self.send_response(code)
        if raw:
            self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        for name, val in (headers or {}).items():
            self.send_header(name, val)
        self.end_headers()
        if raw:
            try:
                self.wfile.write(raw)
            except OSError:
                pass

    def allowed(self):
        if self.path != "/mcp":
            self.reply(404)
            return False
        port = self.server.server_port
        if self.headers.get("Host", "").lower() not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.reply(403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self.reply(403)
            return False
        given = self.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(given, b"Bearer " + self.server.token):
            self.reply(401, headers={"WWW-Authenticate": 'Bearer realm="voice-lab"'})
            return False
        return True

    def do_GET(self):
        if self.allowed():
            self.reply(405, headers={"Allow": "POST, DELETE"})

    def do_DELETE(self):
        if self.allowed():
            self.reply(204 if self.server.end(self.headers.get("Mcp-Session-Id")) else 404)

    def do_POST(self):
        if not self.allowed():
            return
        if self.headers.get_content_type() != "application/json" or "application/json" not in self.headers.get(
            "Accept", "application/json"
        ):
            return self.reply(406)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.reply(400)
        if not 0 < length <= MAX_BODY:
            return self.reply(413)
        self.connection.settimeout(15)
        try:
            raw = self.rfile.read(length)
            msg = json.loads(raw)
        except (OSError, ValueError):
            return self.reply(400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return self.reply(400)
        ident = self.headers.get("Mcp-Session-Id")
        proto = self.headers.get("MCP-Protocol-Version")
        if proto and proto not in LEGACY_VERSIONS:
            return self.reply(400)
        if msg["method"] == "initialize":
            return self.initialize(msg, ident)
        with self.server.lock:
            self.server.prune()
            row = self.server.sessions.get(ident)
            if row is None:
                return self.reply(404)
            if proto is not None and proto != row.version:
                return self.reply(400)
            row.touched = time.monotonic()
            message_id = json.dumps(msg["id"]) if "id" in msg else None
            if message_id in row.pending:
                return self.reply(409)
            if message_id is not None:
                if len(row.pending) >= 4:
                    return self.reply(429)
                row.pending.add(message_id)
        try:
            self.dispatch(row, raw, msg.get("id"), message_id)
        finally:
            if message_id is not None:
                with self.server.lock:
                    row.pending.discard(message_id)

    def initialize(self, msg, ident):
        if ident is not None or "id" not in msg or not isinstance(msg.get("params"), dict):
            return self.reply(400)
        with self.server.lock:
            self.server.prune()
            if len(self.server.sessions) >= MAX_SESSIONS:
                return self.reply(429)
            key = secrets.token_urlsafe(32)
            tools = Tools(self.server.lab, Player(), self.server.clips, workspace=self.server.workspace, scope=key[:8] + "-")
            protocol = Server(tools, io.BytesIO())
            reply = protocol.initialize(msg["params"])
            self.server.sessions[key] = Connection(tools, reply["protocolVersion"], time.monotonic())
        return self.reply(200, {"jsonrpc": "2.0", "id": msg["id"], "result": reply}, {"Mcp-Session-Id": key})

    def dispatch(self, row, raw, rpc_id, message_id):
        output = io.BytesIO()
        protocol = Server(row.tools, output)
        protocol.handle(raw)
        if message_id is None:
            return self.reply(202)
        for thread in protocol.threads:
            thread.join()
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        result = next((item for item in reversed(replies) if item.get("id") == rpc_id), None)
        return self.reply(200, result) if result else self.reply(202)


def _connection_token(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        with path.open("rb") as file:
            return file.read(515).decode("ascii").strip()
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as file:
        file.write(token + "\n")
    return token


def run(args, lab, parser):
    path = args.token_file
    if path is None:
        data = Path(os.environ["LOCALAPPDATA"]) if "LOCALAPPDATA" in os.environ else Path.home() / ".local" / "share"
        path = data / "FelsVoiceLabService" / "mcp-http.token"
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        parser.error("Choose an existing --workspace directory.")
    if not 1 <= args.http <= 65535:
        parser.error("Choose a loopback port from 1 to 65535.")
    try:
        token = os.environ.get("VOICE_LAB_MCP_TOKEN") or _connection_token(path)
    except (OSError, UnicodeError):
        parser.error("Choose a private, writable token file and try again.")
    if not 32 <= len(token) <= 512 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        parser.error("The connection token must have 32–512 printable ASCII characters. Choose a new token file.")
    # Each HTTP session has its own settings and file scope. The parent Lab must already be running.
    lab.can_start = False
    with Host(args.http, lab, workspace, args.clips, token) as host:
        source = "the configured override" if os.environ.get("VOICE_LAB_MCP_TOKEN") else str(path)
        print(f"Voice Lab MCP at http://127.0.0.1:{args.http}/mcp. Token from {source}.", file=sys.stderr, flush=True)
        try:
            host.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            for key in list(host.sessions):
                host.end(key)
    return 0
