#!/usr/bin/env python3
"""Small authenticated gateway for the existing llama.cpp service.

Only chat completions, model listing, UI assets and read-only UI props cross
the boundary. No external package, prompt log or credential log is used.
"""
import argparse
import base64
import hashlib
import gzip
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import re
import secrets
import select
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit


COOKIE = "__Host-llm_session"
MAX_BODY = 128 * 1024
SESSION_SECONDS = 12 * 3600
REQUEST_SECONDS = 600
GENERATION = threading.Lock()
LOGIN_LOCK = threading.Lock()
LOGIN_ATTEMPTS = []
SESSION_INSTANCE_KEY = secrets.token_bytes(32)
REVOKED_SESSIONS = {}
SESSION_LOCK = threading.Lock()


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def issue_session(config):
    value = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(bytes.fromhex(config["session_secret"]) + SESSION_INSTANCE_KEY, value.encode(), "sha256").digest()
    return value + "." + b64(signature)


def valid_session(token, config):
    try:
        expiry, nonce, signature = token.split(".")
        if not (time.time() < int(expiry) <= time.time() + SESSION_SECONDS + 30):
            return False
        value = expiry + "." + nonce
        with SESSION_LOCK:
            if hashlib.sha256(token.encode()).hexdigest() in REVOKED_SESSIONS:
                return False
        expected = b64(hmac.new(bytes.fromhex(config["session_secret"]) + SESSION_INSTANCE_KEY, value.encode(), "sha256").digest())
        return hmac.compare_digest(expected, signature)
    except (ValueError, TypeError):
        return False


def valid_password(username, password, config):
    record = config["password"]
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(record["salt"]), record["iterations"]).hex()
    return hmac.compare_digest(username, config["username"]) & hmac.compare_digest(candidate, record["hash"])


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, config_path, backend_port=11434):
        self.config_path = Path(config_path)
        self.backend_port = backend_port
        self.capacity = threading.BoundedSemaphore(24)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.capacity.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.capacity.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.capacity.release()

    def handle_error(self, request, client_address):
        # Exceptions must never put a request, cookie or Authorization into logs.
        print("gateway: request failed", flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PersonalLLM"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_):
        pass

    def config(self):
        return json.loads(self.server.config_path.read_text())

    def headers_common(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def reply(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def error(self, status, message, code="invalid_request", headers=None):
        self.reply(status, {"error": {"message": message, "type": code, "code": code}}, headers=headers)

    def authenticated(self, config):
        authorization = self.headers.get("Authorization", "")
        if authorization:
            if not authorization.startswith("Bearer "):
                return False, False
            digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
            return any(hmac.compare_digest(digest, value) for value in config["api_keys"].values()), False
        cookie = self.headers.get("Cookie", "")
        values = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
        return valid_session(values.get(COOKIE, ""), config), True

    def same_origin(self, config):
        return self.headers.get("Origin") == config["origin"]

    def route(self, config):
        path = urlsplit(self.path).path
        prefix = config.get("prefix", "/llm")
        if path == prefix or path.startswith(prefix + "/"):
            return path[len(prefix):] or "/"
        return path

    def session_cookie(self):
        cookie = self.headers.get("Cookie", "")
        values = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
        return values.get(COOKIE, "")

    def body(self, maximum=MAX_BODY):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Chunked request bodies are not supported")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        length = int(raw_length)
        if not 0 <= length <= maximum:
            raise ValueError("Request body is too large")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Incomplete request")
        return body

    def upstream(self, method, path, body=None, headers=None, timeout=15):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.backend_port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def do_HEAD(self):
        self.do_GET()

    def do_DELETE(self):
        config = self.config()
        authenticated, session = self.authenticated(config)
        if not authenticated:
            return self.error(401, "Authentication required", "authentication_error")
        if session and not self.same_origin(config):
            return self.error(403, "Origin is not allowed")
        # Bundled UI may clean up its optional resumable-stream state on Stop.
        # This gateway uses connection cancellation and retains no such state.
        return self.error(404, "Resumable streams are not enabled")

    def do_GET(self):
        config = self.config()
        path = self.route(config)
        if path == "/login":
            page = (Path(__file__).parent / "web/login.html").read_text().replace("__PREFIX__", config.get("prefix", "/llm"))
            return self.reply(200, page, "text/html; charset=utf-8")
        authenticated, _ = self.authenticated(config)
        if not authenticated:
            if path.startswith(("/v1/", "/props", "/health", "/session")):
                return self.error(401, "Authentication required", "authentication_error", {"WWW-Authenticate": "Bearer"})
            return self.reply(303, headers={"Location": config.get("prefix", "/llm") + "/login"})
        if path == "/session":
            return self.reply(200, {"username": config["username"]})
        if path not in ("/", "/index.html", "/favicon.ico", "/favicon.svg", "/manifest.json", "/manifest.webmanifest", "/props", "/health", "/slots", "/v1/models") and not path.startswith(("/assets/", "/_app/", "/chat/")) and not re.fullmatch(r"/[a-zA-Z0-9_.-]+\.png", path):
            return self.error(404, "Not found")
        try:
            status, headers, body = self.upstream("GET", path, headers={"Accept-Encoding": "gzip"})
            extra = {key: value for key, value in headers.items() if key.lower() in ("content-encoding", "etag")}
            if path in ("/", "/index.html") and status == 200:
                compressed = headers.get("Content-Encoding") == "gzip"
                document = gzip.decompress(body) if compressed else body
                defaults = (Path(__file__).parent / "web/chat-defaults.js").read_bytes()
                document = document.replace(b"</head>", b"<script>" + defaults + b"</script></head>", 1)
                body = gzip.compress(document, mtime=0) if compressed else document
                extra.pop("ETag", None)
            if path == "/props" and status == 200:
                data = json.loads(body)
                data["model_path"] = "raspberry-llm"
                body = json.dumps(data).encode()
            self.reply(status, body, headers.get("Content-Type", "application/octet-stream"), extra)
        except (OSError, http.client.HTTPException):
            self.error(503, "Model service is temporarily unavailable", "service_unavailable")

    def do_POST(self):
        config = self.config()
        path = self.route(config)
        try:
            if path == "/login":
                if not self.same_origin(config):
                    return self.error(403, "Origin is not allowed")
                body = self.body(4096)
                with LOGIN_LOCK:
                    now = time.monotonic()
                    LOGIN_ATTEMPTS[:] = [stamp for stamp in LOGIN_ATTEMPTS if now - stamp < 600]
                    if len(LOGIN_ATTEMPTS) >= 12:
                        return self.error(429, "Too many login attempts; wait ten minutes", "rate_limit_error", {"Retry-After": "600"})
                    LOGIN_ATTEMPTS.append(now)
                fields = parse_qs(body.decode())
                username, password = fields.get("username", [""])[0], fields.get("password", [""])[0]
                if not valid_password(username, password, config):
                    return self.error(401, "Неверное имя пользователя или пароль", "authentication_error")
                cookie = f"{COOKIE}={issue_session(config)}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"
                return self.reply(200, {"ok": True}, headers={"Set-Cookie": cookie})
            authenticated, session = self.authenticated(config)
            if not authenticated:
                return self.error(401, "Authentication required", "authentication_error", {"WWW-Authenticate": "Bearer"})
            if session and not self.same_origin(config):
                return self.error(403, "Origin is not allowed")
            if path == "/logout":
                token = self.session_cookie()
                if token:
                    with SESSION_LOCK:
                        now = time.time()
                        expired = [key for key, expiry in REVOKED_SESSIONS.items() if expiry <= now]
                        for key in expired:
                            del REVOKED_SESSIONS[key]
                        REVOKED_SESSIONS[hashlib.sha256(token.encode()).hexdigest()] = int(token.split(".")[0])
                return self.reply(200, {"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"})
            if path != "/v1/chat/completions":
                return self.error(404, "Not found")
            if self.headers.get_content_type() != "application/json":
                return self.error(415, "Use application/json")
            payload = json.loads(self.body())
            self.validate_payload(payload)
            if not GENERATION.acquire(blocking=False):
                return self.error(429, "Модель занята другим запросом. Повторите позже.", "rate_limit_error", {"Retry-After": "10"})
            try:
                self.chat(payload, config)
            finally:
                GENERATION.release()
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.error(400, "Invalid request: text messages and max_tokens between 1 and 512 are required")
        except (OSError, http.client.HTTPException):
            self.error(503, "Model service is temporarily unavailable", "service_unavailable")

    @staticmethod
    def validate_payload(payload):
        if not isinstance(payload, dict) or payload.get("model", "raspberry-llm") != "raspberry-llm":
            raise ValueError("Unknown model")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
            raise ValueError("Invalid messages")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant", "tool"):
                raise ValueError("Invalid role")
            content = message.get("content")
            if isinstance(content, list):
                if not all(isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) for part in content):
                    raise ValueError("Only text is supported")
                message["content"] = "\n".join(part["text"] for part in content)
            if message.get("content") is None and isinstance(message.get("tool_calls"), list):
                message["content"] = ""
            if not isinstance(message.get("content"), str):
                raise ValueError("Only text is supported")
        limit = payload.pop("max_completion_tokens", payload.get("max_tokens", 256))
        if limit == -1:
            limit = 256
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 512:
            raise ValueError("Invalid output limit")
        payload["max_tokens"] = limit
        payload["model"] = "raspberry-llm"
        # UI hint: omitted sampling values already use the backend defaults.
        payload.pop("backend_sampling", None)
        if "sse_ping_interval" in payload:
            payload["sse_ping_interval"] = max(5, min(30, int(payload["sse_ping_interval"])))
        if payload.get("n", 1) != 1 or payload.get("stream", False) not in (True, False):
            raise ValueError("Only one completion is supported")
        # Block server-specific overrides that could expand context or invoke tools.
        allowed = {"model", "messages", "temperature", "top_p", "top_k", "min_p", "seed", "max_tokens", "stream", "stream_options", "stop", "presence_penalty", "frequency_penalty", "response_format", "tools", "tool_choice", "parallel_tool_calls", "n", "cache_prompt", "reasoning_control", "reasoning_format", "reasoning_effort", "return_progress", "sse_ping_interval", "timings_per_token", "repeat_penalty", "repeat_last_n", "typical_p", "dynatemp_range", "dynatemp_exponent", "xtc_probability", "xtc_threshold", "dry_multiplier", "dry_base", "dry_allowed_length", "dry_penalty_last_n", "dry_sequence_breakers", "samplers", "mirostat", "mirostat_tau", "mirostat_eta"}
        if set(payload) - allowed:
            raise ValueError("Unsupported parameter")

    def chunk(self, content):
        self.wfile.write(f"{len(content):x}\r\n".encode() + content + b"\r\n")
        self.wfile.flush()

    def chat(self, payload, config):
        streaming = bool(payload.get("stream", False))
        data = json.dumps(payload).encode()
        if not streaming:
            status, headers, body = self.upstream("POST", "/v1/chat/completions", data, {"Content-Type": "application/json"}, timeout=REQUEST_SECONDS)
            return self.reply(status, body, headers.get("Content-Type", "application/json"))
        events = queue.Queue(maxsize=32)
        stopped = threading.Event()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.backend_port, timeout=REQUEST_SECONDS)
        upstream_socket = [None]

        def put(event):
            while not stopped.is_set():
                try:
                    events.put(event, timeout=0.25)
                    return
                except queue.Full:
                    pass

        def read_upstream():
            response = None
            try:
                connection.connect()
                # HTTPConnection may drop its socket reference for a close-delimited
                # SSE response. Keep a duplicate so cancellation can interrupt read1.
                upstream_socket[0] = connection.sock.dup()
                if stopped.is_set():
                    return
                connection.request("POST", "/v1/chat/completions", data, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                if response.status != 200:
                    put(("error", response.status, response.read()))
                    return
                while not stopped.is_set():
                    block = response.read1(4096)
                    if not block:
                        break
                    put(("data", block))
            except (OSError, http.client.HTTPException):
                put(("error", 503, b""))
            finally:
                if response is not None:
                    response.close()
                connection.close()
                if upstream_socket[0] is not None:
                    upstream_socket[0].close()
                put(("done",))

        worker = threading.Thread(target=read_upstream, daemon=True)
        worker.start()
        try:
            # Wait for the first result briefly to preserve native HTTP errors.
            try:
                first = events.get(timeout=0.1)
            except queue.Empty:
                first = None
            if first and first[0] == "error":
                return self.reply(first[1], first[2] or {"error": {"message": "Model unavailable"}})
            self.send_response(200)
            self.headers_common()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.chunk(b": connected\n\n")
            deadline = time.monotonic() + REQUEST_SECONDS
            last_heartbeat = time.monotonic()
            event = first
            while time.monotonic() < deadline:
                if select.select([self.connection], [], [], 0)[0]:
                    if not self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                        break
                if event is None:
                    try:
                        event = events.get(timeout=0.25)
                    except queue.Empty:
                        if time.monotonic() - last_heartbeat >= 5:
                            self.chunk(b": keepalive\n\n")
                            last_heartbeat = time.monotonic()
                        continue
                if event[0] == "done":
                    break
                if event[0] == "error":
                    self.chunk(b'data: {"error":{"message":"Model request failed","type":"server_error"}}\n\ndata: [DONE]\n\n')
                    break
                self.chunk(event[1])
                event = None
            else:
                self.chunk(b'data: {"error":{"message":"Request timed out","type":"timeout"}}\n\ndata: [DONE]\n\n')
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (OSError, http.client.HTTPException):
            # A closed browser connection is cancellation, not a new error reply.
            pass
        finally:
            stopped.set()
            if upstream_socket[0] is not None:
                try:
                    upstream_socket[0].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
            worker.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18085)
    parser.add_argument("--backend-port", type=int, default=11434)
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/raspberry-llm/access.json")
    args = parser.parse_args()
    server = Gateway(("127.0.0.1", args.port), args.config, args.backend_port)
    print("gateway: listening on loopback", args.port, flush=True)
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
